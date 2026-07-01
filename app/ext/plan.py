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

"""ext4 filesystem plan for the targeted-recovery engine (extents only).

Maps ext4 onto the engine's phase model, mirroring the NTFS plan:

  GET_SUPERBLOCK  image the superblock -> geometry (block size, groups, …)
  GET_GDT         image the group descriptor table -> inode-table locations
  GET_INODES      image every inode table (ext's distributed "$MFT")
  GET_DIRS        image every directory's data blocks -> the name tree
  GET_FILEDATA    image every regular file's extents (allocated data only)

ext3/ext2 files that use indirect blocks (no extent tree) are detected and
counted but not imaged — extent support only, for now.
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
from app.ext import catalog
from app.ext.superblock import Superblock, parse as parse_superblock

SUPERBLOCK_SCAN_BYTES = 2048   # covers the superblock at +1024 and a margin

EXT_STEPS: list[tuple[Phase, str]] = [
    (Phase.GET_TABLE, "Partition table"),
    (Phase.GET_VBRS, "Volume boot records"),
    (Phase.GET_SUPERBLOCK, "Superblock"),
    (Phase.GET_GDT, "Group descriptors"),
    (Phase.GET_INODES, "Inode tables"),
    (Phase.GET_DIRS, "Directory blocks"),
    (Phase.GET_FILEDATA, "File data"),
]


@dataclass
class ExtCatalog:
    """Plan-owned parsed state carried in ``RecoveryState.fs``."""
    sb: Superblock
    gdt: list[int]
    dir_ranges: list[tuple[int, int]] = field(default_factory=list)
    filedata_ranges: list[tuple[int, int]] = field(default_factory=list)


class ExtPlan(FilesystemPlan):
    name = "ext4"
    first_phase = Phase.GET_SUPERBLOCK
    catalog_phase = Phase.GET_INODES
    index_phase = Phase.GET_DIRS

    def steps(self):
        return EXT_STEPS

    def handler(self, phase: Phase) -> PhaseHandler:
        return self._handlers()[phase]

    def _handlers(self) -> dict[Phase, PhaseHandler]:
        return {
            Phase.GET_SUPERBLOCK: PhaseHandler(
                Phase.GET_SUPERBLOCK, "Recovering superblock",
                self._build_superblock, self._parse_superblock),
            Phase.GET_GDT: PhaseHandler(
                Phase.GET_GDT, "Recovering group descriptors",
                self._build_gdt, self._parse_gdt),
            Phase.GET_INODES: PhaseHandler(
                Phase.GET_INODES, "Recovering inode tables",
                self._build_inodes, self._parse_inodes),
            Phase.GET_DIRS: PhaseHandler(
                Phase.GET_DIRS, "Recovering directory blocks",
                self._build_dirs, self._parse_dirs),
            Phase.GET_FILEDATA: PhaseHandler(
                Phase.GET_FILEDATA, "Imaging allocated file data",
                self._build_filedata, self._parse_filedata),
        }

    # --- superblock -------------------------------------------------------
    def _build_superblock(self, st: RecoveryState):
        return Image([(st.volume_offset + 1024, SUPERBLOCK_SCAN_BYTES)])

    def _parse_superblock(self, st: RecoveryState):
        sb = parse_superblock(
            read_image(st.outfile, st.volume_offset + 1024, 1024), st.volume_offset)
        if sb is None:
            return Terminal(False, "Could not read the ext superblock.")
        st.fs = ExtCatalog(sb=sb, gdt=[])
        st.log(
            f"ext: {sb.block_size} B/block, inode {sb.inode_size} B, "
            f"{sb.n_groups} group(s), {sb.inodes_per_group} inodes/group."
        )
        return Next(Phase.GET_GDT)

    # --- group descriptors ------------------------------------------------
    def _build_gdt(self, st: RecoveryState):
        sb = st.fs.sb
        from app.ext.group_desc import gdt_byte_length
        length = gdt_byte_length(sb)
        blocks = (length + sb.block_size - 1) // sb.block_size
        return Image([(sb.block_offset(sb.gdt_block), blocks * sb.block_size)])

    def _parse_gdt(self, st: RecoveryState):
        from app.ext.inode import read_gdt
        gdt = read_gdt(st.outfile, st.fs.sb)
        if not gdt:
            return Terminal(False, "Could not read the group descriptor table.")
        st.fs.gdt = gdt
        st.log(f"Located {len(gdt)} inode table(s).")
        return Next(Phase.GET_INODES)

    # --- inode tables -----------------------------------------------------
    def _build_inodes(self, st: RecoveryState):
        sb = st.fs.sb
        span = sb.inodes_per_group * sb.inode_size
        return Image([(sb.block_offset(itab), span) for itab in st.fs.gdt])

    def _parse_inodes(self, st: RecoveryState):
        if st.stop_after_catalog:
            return Terminal(True, "Superblock, group descriptors and inode tables "
                                  "recovered.")
        return Next(Phase.GET_DIRS)

    # --- directory blocks -------------------------------------------------
    def _build_dirs(self, st: RecoveryState):
        ranges, n_dirs = catalog.collect_dir_ranges(st.outfile, st.fs.sb, st.fs.gdt)
        st.fs.dir_ranges = ranges
        st.log(f"Found {n_dirs} directories -> {len(ranges)} region(s).")
        if not ranges:
            if st.include_filedata:
                return Next(Phase.GET_FILEDATA)
            return Terminal(True, "No directory data blocks found.")
        return Image(ranges)

    def _parse_dirs(self, st: RecoveryState):
        if st.include_filedata:
            return Next(Phase.GET_FILEDATA)
        n = len(st.fs.dir_ranges or [])
        return Terminal(True, f"Recovered {n} directory region(s). "
                              "Directory tree should now be available.")

    # --- file data --------------------------------------------------------
    def _build_filedata(self, st: RecoveryState):
        ranges, n_files, n_skipped = catalog.collect_filedata_ranges(
            st.outfile, st.fs.sb, st.fs.gdt)
        st.fs.filedata_ranges = ranges
        total = sum(length for _, length in ranges)
        st.log(f"Found {n_files} files with extents -> {len(ranges)} region(s), "
               f"{total:,} bytes to image.")
        if n_skipped:
            st.log(f"Skipped {n_skipped} indirect-block (ext3/ext2) file(s) — "
                   "extent support only for now.")
        if not ranges:
            return Terminal(True, "No allocated file data found to image.")
        return Image(ranges)

    def _parse_filedata(self, st: RecoveryState):
        n = len(st.fs.filedata_ranges or [])
        return Terminal(True, f"Imaged {n} file-data region(s). "
                              "Allocated file content recovered (free space skipped).")

    # --- entry from an already-imaged volume ------------------------------
    def prepare_existing(self, st: RecoveryState) -> str | None:
        geom = catalog.load_geometry(st.outfile, st.volume_offset)
        if geom is None:
            return "ext superblock / inode tables not yet recovered — run Step 1 first."
        sb, gdt = geom
        st.fs = ExtCatalog(sb=sb, gdt=gdt)
        return None

    # --- UI helpers -------------------------------------------------------
    def build_tree(self, image: str, volume_offset: int):
        return catalog.build_tree(image, volume_offset)

    def filedata_domain(self, image: str, volume_offset: int, total: int,
                        sector_size: int):
        geom = catalog.load_geometry(image, volume_offset)
        if geom is None:
            return None
        sb, gdt = geom
        ranges, _n, _skipped = catalog.collect_filedata_ranges(image, sb, gdt)
        if not ranges:
            return None
        return domain.build_domain_mapfile(ranges, total, sector_size)
