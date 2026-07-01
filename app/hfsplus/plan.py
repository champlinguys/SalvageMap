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

"""HFS+ filesystem plan for the targeted-recovery engine (data-fork extents).

Maps HFS+ onto the engine's phase model. Because the Catalog B-tree holds both
the directory hierarchy and each file's extents, there is no separate
directory-blocks phase (unlike ext):

  GET_VOLHEADER  image the volume header (+1024) -> geometry + special-file forks
  GET_CATALOG    image the Catalog File -> the name tree and file records
  GET_EXTENTS    image the Extents Overflow File -> extra extents for big files
  GET_FILEDATA   image every file's data-fork extents (inline 8 + overflow)

Compressed files (data in the resource fork / decmpfs attribute) are counted and
reported but not imaged — data-fork extents only, for now.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core import domain
from app.core.recovery import (
    FilesystemPlan,
    Image,
    Next,
    Phase,
    PhaseHandler,
    RecoveryState,
    Terminal,
)
from app.hfsplus import catalog, extents
from app.hfsplus.btree import BTree
from app.hfsplus.volume_header import VolumeHeader

VOLHEADER_SCAN_BYTES = 1024   # covers the 512-byte header at +1024 and a margin
_INCOMPLETE_LOG_LIMIT = 20    # cap the per-file incomplete list in the log

# The Extents Overflow File is imaged *before* the Catalog: a catalog fragmented
# past 8 extents keeps the rest in the overflow file, so we need that in hand to
# image (and later parse) the whole catalog.
HFSPLUS_STEPS: list[tuple[Phase, str]] = [
    (Phase.GET_TABLE, "Partition table"),
    (Phase.GET_VBRS, "Volume boot records"),
    (Phase.GET_VOLHEADER, "Volume header"),
    (Phase.GET_EXTENTS, "Extents overflow"),
    (Phase.GET_CATALOG, "Catalog B-tree"),
    (Phase.GET_FILEDATA, "File data"),
]


@dataclass
class HfsState:
    """Plan-owned parsed state carried in ``RecoveryState.fs``."""
    vh: VolumeHeader
    filedata_ranges: list[tuple[int, int]] = None


class HfsPlusPlan(FilesystemPlan):
    name = "HFS+"
    first_phase = Phase.GET_VOLHEADER
    catalog_phase = Phase.GET_CATALOG
    index_phase = Phase.GET_EXTENTS   # resume re-images overflow, then catalog

    def steps(self):
        return HFSPLUS_STEPS

    def handler(self, phase: Phase) -> PhaseHandler:
        return self._handlers()[phase]

    def _handlers(self) -> dict[Phase, PhaseHandler]:
        return {
            Phase.GET_VOLHEADER: PhaseHandler(
                Phase.GET_VOLHEADER, "Recovering volume header",
                self._build_volheader, self._parse_volheader),
            Phase.GET_CATALOG: PhaseHandler(
                Phase.GET_CATALOG, "Recovering catalog B-tree",
                self._build_catalog, self._parse_catalog),
            Phase.GET_EXTENTS: PhaseHandler(
                Phase.GET_EXTENTS, "Recovering extents overflow",
                self._build_extents, self._parse_extents),
            Phase.GET_FILEDATA: PhaseHandler(
                Phase.GET_FILEDATA, "Imaging allocated file data",
                self._build_filedata, self._parse_filedata),
        }

    # --- volume header ----------------------------------------------------
    def _build_volheader(self, st: RecoveryState):
        return Image([(st.volume_offset + 1024, VOLHEADER_SCAN_BYTES)])

    def _parse_volheader(self, st: RecoveryState):
        vh = catalog.load_volume(st.outfile, st.volume_offset)
        if vh is None:
            return Terminal(False, "Could not read the HFS+ volume header.")
        st.fs = HfsState(vh=vh)
        st.log(
            f"HFS+: {vh.block_size} B/block, {vh.total_blocks} blocks, "
            f"catalog {vh.catalog.total_blocks} block(s)."
        )
        return Next(Phase.GET_EXTENTS)

    # --- extents overflow (imaged first: it resolves the catalog's own tail) --
    def _build_extents(self, st: RecoveryState):
        # Bootstrap the overflow file's own extents so a fragmented overflow file
        # is imaged in full, not just its inline 8.
        ranges, _ov = catalog.resolve_overflow_file(st.outfile, st.fs.vh)
        if not ranges:
            # No overflow file content: nothing to image here — go straight to the
            # catalog (its extents must be inline-only, which we handle there).
            return Next(Phase.GET_CATALOG)
        return Image(ranges)

    def _parse_extents(self, st: RecoveryState):
        return Next(Phase.GET_CATALOG)

    # --- catalog (needs the overflow file above to resolve its tail extents) --
    def _build_catalog(self, st: RecoveryState):
        ranges = catalog.catalog_ranges(st.outfile, st.fs.vh)
        if not ranges:
            return Terminal(False, "HFS+ volume header has no catalog extents.")
        res = extents.resolve_fork_coverage(
            st.fs.vh.catalog, st.fs.vh, extents.HFS_CATALOG_FILE_ID,
            extents.FORK_TYPE_DATA, catalog.load_overflow(st.outfile, st.fs.vh))
        if not res.fully_mapped:
            st.log(f"WARNING: the catalog is fragmented into more extents than "
                   f"could be mapped ({res.mapped_blocks}/{res.total_blocks} "
                   "blocks) — the Extents Overflow File is incomplete, so parts "
                   "of the directory tree may be missing until it is recovered.")
        return Image(ranges)

    def _parse_catalog(self, st: RecoveryState):
        if st.stop_after_catalog:
            return Terminal(True, "Volume header, extents overflow and catalog "
                                  "B-tree recovered.")
        if st.include_filedata:
            return Next(Phase.GET_FILEDATA)
        return Terminal(True, "Catalog recovered. Directory tree should now be "
                              "available.")

    # --- file data --------------------------------------------------------
    def _build_filedata(self, st: RecoveryState):
        scan = catalog.scan_filedata(st.outfile, st.fs.vh)
        st.fs.filedata_ranges = scan.ranges
        total = sum(length for _, length in scan.ranges)
        st.log(f"Found {scan.n_files} files with data -> {len(scan.ranges)} "
               f"region(s), {total:,} bytes to image.")
        if scan.n_skipped:
            st.log(f"Skipped {scan.n_skipped} compressed file(s) — data-fork "
                   "extents only for now.")
        if scan.incomplete:
            st.log(f"WARNING: {len(scan.incomplete)} file(s) are too fragmented "
                   "to map fully — the Extents Overflow File is incomplete, so "
                   "their tail extents can't be located. These can be imaged only "
                   "in part until more of the volume is recovered:")
            for name, size in scan.incomplete[:_INCOMPLETE_LOG_LIMIT]:
                st.log(f"    incomplete: {name} ({size:,} bytes)")
            if len(scan.incomplete) > _INCOMPLETE_LOG_LIMIT:
                st.log(f"    …and {len(scan.incomplete) - _INCOMPLETE_LOG_LIMIT} "
                       "more.")
        if not scan.ranges:
            return Terminal(True, "No allocated file data found to image.")
        return Image(scan.ranges)

    def _parse_filedata(self, st: RecoveryState):
        n = len(st.fs.filedata_ranges or [])
        return Terminal(True, f"Imaged {n} file-data region(s). "
                              "Allocated file content recovered (free space skipped).")

    # --- entry from an already-imaged volume ------------------------------
    def prepare_existing(self, st: RecoveryState) -> str | None:
        vh = catalog.load_volume(st.outfile, st.volume_offset)
        if vh is None:
            return "HFS+ volume header not yet recovered — run Step 1 first."
        bt = BTree(st.outfile, catalog.catalog_ranges(st.outfile, vh))
        if not bt.ok:
            return "HFS+ catalog not yet recovered — run Step 1 first."
        st.fs = HfsState(vh=vh)
        return None

    # --- UI helpers -------------------------------------------------------
    def build_tree(self, image: str, volume_offset: int):
        return catalog.build_tree(image, volume_offset)

    def metadata_ranges(self, image: str, volume_offset: int):
        """Catalog + Extents Overflow ranges — re-imaging them can complete the
        extent maps of heavily-fragmented files."""
        vh = catalog.load_volume(image, volume_offset)
        if vh is None:
            return []
        overflow_ranges, overflow = catalog.resolve_overflow_file(image, vh)
        return overflow_ranges + catalog.catalog_ranges(image, vh, overflow)

    def filedata_domain(self, image: str, volume_offset: int, total: int,
                        sector_size: int):
        vh = catalog.load_volume(image, volume_offset)
        if vh is None:
            return None
        ranges, _n, _skipped = catalog.collect_filedata_ranges(image, vh)
        if not ranges:
            return None
        return domain.build_domain_mapfile(ranges, total, sector_size)
