# SalvageMap — GUI wrapper over GNU ddrescue.
# Copyright (C) 2026 Champlin Guys Data Recovery
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""NTFS filesystem plan for the targeted-recovery engine.

Maps NTFS onto the engine's phase model:

  Phase 1  GET_BOOT      image the volume boot record -> locate $MFT
  Phase 2  GET_MFT0      image MFT record 0 -> read $MFT's own $DATA runs
  Phase 3  GET_MFT       image the full $MFT
  Phase 4  GET_INDEX     parse $MFT, image every $INDEX_ALLOCATION region
  Phase 5  GET_FILEDATA  image all allocated file $DATA (skips free space)

The parse helpers (``load_boot`` … ``build_filedata_domain``) read only from the
output image and are shared by the handlers below and by the File ▸ Export
file-data Domain action in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core import domain, mapfile
from app.core.recovery import (
    FilesystemPlan,
    Image,
    Next,
    Phase,
    PhaseHandler,
    RecoveryState,
    Terminal,
    read_image,
)
from app.ntfs import boot_sector, mft
from app.ntfs.filetree import build_file_tree
from app.ntfs.runlist import runs_to_byte_ranges

BOOT_SCAN_BYTES = 64 * 1024

# Canonical ordered steps for the workflow checklist (Phase -> short label).
PHASE_STEPS: list[tuple[Phase, str]] = [
    (Phase.GET_TABLE, "Partition table"),
    (Phase.GET_VBRS, "Volume boot records"),
    (Phase.GET_BOOT, "Boot sector"),
    (Phase.GET_MFT0, "$MFT record 0"),
    (Phase.GET_MFT, "Full $MFT"),
    (Phase.GET_INDEX, "Directory indexes"),
    (Phase.GET_FILEDATA, "File data"),
]


# --- image parsing (all reads from the image, never the device) -----------
def load_boot(outfile: str, volume_offset: int) -> boot_sector.BootSector | None:
    """Parse the NTFS boot sector from the image, or None if not recovered."""
    data = read_image(outfile, volume_offset, 512)
    try:
        return boot_sector.parse(data, volume_offset=volume_offset)
    except ValueError:
        return None


def load_mft_ranges(outfile: str, boot: boot_sector.BootSector) -> list[tuple[int, int]]:
    """Byte ranges of the full $MFT, from MFT record 0's own $DATA runs."""
    raw = read_image(outfile, boot.mft_offset, boot.mft_record_size)
    rec0 = mft.parse_record(raw, 0, boot.bytes_per_sector)
    if rec0 is None:
        return []
    return runs_to_byte_ranges(rec0.data_runs(), boot.bytes_per_cluster, boot.volume_offset)


def assemble_mft(outfile: str, mft_ranges: list[tuple[int, int]]) -> bytes:
    return b"".join(read_image(outfile, s, length) for s, length in mft_ranges)


def collect_index_ranges(boot, mft_bytes: bytes) -> tuple[list[tuple[int, int]], int]:
    """All directory $INDEX_ALLOCATION regions and the count of directories."""
    ranges: list[tuple[int, int]] = []
    n_dirs = 0
    for rec in mft.iter_records(mft_bytes, boot.mft_record_size, boot.bytes_per_sector):
        ia = rec.index_allocation_runs()
        if ia:
            n_dirs += 1
            ranges += runs_to_byte_ranges(ia, boot.bytes_per_cluster, boot.volume_offset)
    return ranges, n_dirs


def collect_filedata_ranges(boot, mft_bytes: bytes) -> tuple[list[tuple[int, int]], int]:
    """All allocated file $DATA regions and the count of files with data."""
    ranges: list[tuple[int, int]] = []
    n_files = 0
    for rec in mft.iter_records(mft_bytes, boot.mft_record_size, boot.bytes_per_sector):
        if not rec.in_use or rec.is_directory:
            continue
        runs = rec.data_runs()
        if runs:
            byte_ranges = runs_to_byte_ranges(runs, boot.bytes_per_cluster, boot.volume_offset)
            if byte_ranges:
                n_files += 1
                ranges += byte_ranges
    return ranges, n_files


def collect_attribute_list_ranges(boot, mft_bytes: bytes) -> list[tuple[int, int]]:
    """On-disk ranges of every *non-resident* $ATTRIBUTE_LIST.

    These hold the run maps of the most heavily-fragmented files; imaging them
    lets a later tree rebuild follow those lists instead of flagging the files
    incomplete."""
    ranges: list[tuple[int, int]] = []
    for rec in mft.iter_records(mft_bytes, boot.mft_record_size, boot.bytes_per_sector):
        attr = rec.attribute_list()
        if attr is not None and attr.non_resident:
            ranges += runs_to_byte_ranges(
                attr.runs, boot.bytes_per_cluster, boot.volume_offset)
    return ranges


def build_filedata_domain(outfile: str, volume_offset: int, total_size: int,
                          sector_size: int) -> mapfile.Mapfile | None:
    """Build the file-data domain file from an image's $MFT (for export)."""
    boot = load_boot(outfile, volume_offset)
    if boot is None:
        return None
    mft_ranges = load_mft_ranges(outfile, boot)
    if not mft_ranges:
        return None
    mft_bytes = assemble_mft(outfile, mft_ranges)
    ranges, _ = collect_filedata_ranges(boot, mft_bytes)
    if not ranges:
        return None
    return domain.build_domain_mapfile(ranges, total_size, sector_size)


@dataclass
class NtfsCatalog:
    """Plan-owned parsed state carried in ``RecoveryState.fs``."""
    boot: boot_sector.BootSector
    mft_ranges: list[tuple[int, int]]
    index_ranges: list[tuple[int, int]] = field(default_factory=list)
    filedata_ranges: list[tuple[int, int]] = field(default_factory=list)


class NtfsPlan(FilesystemPlan):
    name = "NTFS"
    first_phase = Phase.GET_BOOT
    catalog_phase = Phase.GET_MFT
    index_phase = Phase.GET_INDEX

    def steps(self):
        return PHASE_STEPS

    def handler(self, phase: Phase) -> PhaseHandler:
        return self._handlers()[phase]

    def _handlers(self) -> dict[Phase, PhaseHandler]:
        return {
            Phase.GET_BOOT: PhaseHandler(
                Phase.GET_BOOT, "Recovering boot sector",
                self._build_boot, self._parse_boot),
            Phase.GET_MFT0: PhaseHandler(
                Phase.GET_MFT0, "Recovering $MFT record 0",
                self._build_mft0, self._parse_mft0),
            Phase.GET_MFT: PhaseHandler(
                Phase.GET_MFT, "Recovering full $MFT",
                self._build_mft, self._parse_mft),
            Phase.GET_INDEX: PhaseHandler(
                Phase.GET_INDEX, "Recovering directory index regions",
                self._build_index, self._parse_index),
            Phase.GET_FILEDATA: PhaseHandler(
                Phase.GET_FILEDATA, "Imaging allocated file data",
                self._build_filedata, self._parse_filedata),
        }

    # --- phase 1: boot ----------------------------------------------------
    def _build_boot(self, st: RecoveryState):
        return Image([(st.volume_offset, BOOT_SCAN_BYTES)])

    def _parse_boot(self, st: RecoveryState):
        boot = load_boot(st.outfile, st.volume_offset)
        if boot is None:
            return Terminal(False, "Could not read the NTFS boot sector.")
        st.fs = NtfsCatalog(boot=boot, mft_ranges=[])
        st.log(
            f"NTFS: {boot.bytes_per_cluster} B/cluster, MFT record "
            f"{boot.mft_record_size} B, $MFT at offset 0x{boot.mft_offset:X}"
        )
        return Next(Phase.GET_MFT0)

    # --- phase 2: MFT record 0 -------------------------------------------
    def _build_mft0(self, st: RecoveryState):
        boot = st.fs.boot
        return Image([(boot.mft_offset, boot.mft_record_size)])

    def _parse_mft0(self, st: RecoveryState):
        ranges = load_mft_ranges(st.outfile, st.fs.boot)
        if not ranges:
            return Terminal(False, "Could not read $MFT record 0.")
        st.fs.mft_ranges = ranges
        total = sum(length for _, length in ranges)
        st.log(
            f"$MFT spans {len(ranges)} run(s), {total} bytes "
            f"(~{total // st.fs.boot.mft_record_size} records)."
        )
        return Next(Phase.GET_MFT)

    # --- phase 3: full MFT ------------------------------------------------
    def _build_mft(self, st: RecoveryState):
        return Image(st.fs.mft_ranges)

    def _parse_mft(self, st: RecoveryState):
        if st.stop_after_catalog:
            return Terminal(True, "Boot sector and full $MFT recovered.")
        return Next(Phase.GET_INDEX)

    # --- phase 4: index regions ------------------------------------------
    def _build_index(self, st: RecoveryState):
        mft_bytes = assemble_mft(st.outfile, st.fs.mft_ranges)
        ranges, n_dirs = collect_index_ranges(st.fs.boot, mft_bytes)
        st.fs.index_ranges = ranges
        st.log(f"Found {n_dirs} directories with index allocations -> "
               f"{len(ranges)} region(s).")
        if not ranges:
            if st.include_filedata:
                return Next(Phase.GET_FILEDATA)
            return Terminal(True, "No non-resident directory indexes found.")
        return Image(ranges)

    def _parse_index(self, st: RecoveryState):
        if st.include_filedata:
            return Next(Phase.GET_FILEDATA)
        n = len(getattr(st.fs, "index_ranges", []))
        return Terminal(True, f"Recovered {n} directory index region(s). "
                              "Directory tree metadata should now be available.")

    # --- phase 5: file data ----------------------------------------------
    def _build_filedata(self, st: RecoveryState):
        mft_bytes = assemble_mft(st.outfile, st.fs.mft_ranges)
        ranges, n_files = collect_filedata_ranges(st.fs.boot, mft_bytes)
        st.fs.filedata_ranges = ranges
        total = sum(length for _, length in ranges)
        st.log(f"Found {n_files} files with allocated data -> "
               f"{len(ranges)} region(s), {total:,} bytes to image.")
        if not ranges:
            return Terminal(True, "No allocated file data found to image.")
        return Image(ranges)

    def _parse_filedata(self, st: RecoveryState):
        n = len(getattr(st.fs, "filedata_ranges", []))
        return Terminal(True, f"Imaged {n} file-data region(s). "
                              "Allocated file content recovered (free space skipped).")

    # --- entry from an already-imaged volume ------------------------------
    def prepare_existing(self, st: RecoveryState) -> str | None:
        boot = load_boot(st.outfile, st.volume_offset)
        if boot is None:
            return "Boot sector not yet recovered — run Step 1 first."
        ranges = load_mft_ranges(st.outfile, boot)
        if not ranges:
            return "$MFT not yet recovered — run Step 1 first."
        st.fs = NtfsCatalog(boot=boot, mft_ranges=ranges)
        return None

    # --- UI helpers -------------------------------------------------------
    def build_tree(self, image: str, volume_offset: int):
        boot = load_boot(image, volume_offset)
        if boot is None:
            return None
        ranges = load_mft_ranges(image, boot)
        if not ranges:
            return None

        def read_volume(offset: int, length: int) -> bytes:
            return read_image(image, offset, length)

        return build_file_tree(boot, assemble_mft(image, ranges), read_volume)

    def filedata_domain(self, image: str, volume_offset: int, total: int,
                        sector_size: int):
        return build_filedata_domain(image, volume_offset, total, sector_size)

    def metadata_ranges(self, image: str, volume_offset: int):
        """The $MFT plus any non-resident $ATTRIBUTE_LIST clusters.

        Re-imaging the $MFT completes the extension records that hold a
        fragmented file's overflow run list; the attribute-list clusters (for
        the most fragmented files, whose list is itself non-resident) live out on
        the volume, so we add them too."""
        boot = load_boot(image, volume_offset)
        if boot is None:
            return []
        mft_ranges = load_mft_ranges(image, boot)
        ranges = list(mft_ranges)
        if mft_ranges:
            ranges += collect_attribute_list_ranges(
                boot, assemble_mft(image, mft_ranges))
        return ranges
