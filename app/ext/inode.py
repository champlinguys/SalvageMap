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

"""Parse ext4 inodes and iterate the inode tables from a recovered image.

An inode describes one file/directory: its type, size, flags and its 60-byte
``i_block`` area, which on ext4 normally holds an extent tree (the equivalent of
an NTFS data run list). We read inodes straight out of the imaged inode tables,
which the group descriptors located for us.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

from app.core.recovery import read_image
from app.ext import group_desc
from app.ext.superblock import (
    EXTENTS_FL,
    INLINE_DATA_FL,
    S_IFDIR,
    S_IFLNK,
    S_IFMT,
    S_IFREG,
    Superblock,
)

I_BLOCK_OFFSET = 0x28
I_BLOCK_SIZE = 60


@dataclass
class Inode:
    number: int
    mode: int
    size: int
    flags: int
    links_count: int
    i_block: bytes           # 60-byte block area (extent tree or block pointers)

    @property
    def file_type(self) -> int:
        return self.mode & S_IFMT

    @property
    def is_dir(self) -> bool:
        return self.file_type == S_IFDIR

    @property
    def is_regular(self) -> bool:
        return self.file_type == S_IFREG

    @property
    def is_symlink(self) -> bool:
        return self.file_type == S_IFLNK

    @property
    def uses_extents(self) -> bool:
        return bool(self.flags & EXTENTS_FL)

    @property
    def inline_data(self) -> bool:
        return bool(self.flags & INLINE_DATA_FL)


def parse(raw: bytes, number: int) -> Inode | None:
    """Parse one inode's bytes. None if it is unused (mode 0) or too short."""
    # Guard the full span we read: the highest field is size_hi at 0x6C (u32).
    # A short slice (e.g. the final partial inode of a truncated table read on a
    # half-imaged volume) returns None rather than raising struct.error.
    if len(raw) < 0x70:
        return None
    mode = struct.unpack_from("<H", raw, 0x00)[0]
    if mode == 0:
        return None
    size_lo = struct.unpack_from("<I", raw, 0x04)[0]
    size_hi = struct.unpack_from("<I", raw, 0x6C)[0]
    links_count = struct.unpack_from("<H", raw, 0x1A)[0]
    flags = struct.unpack_from("<I", raw, 0x20)[0]
    i_block = raw[I_BLOCK_OFFSET:I_BLOCK_OFFSET + I_BLOCK_SIZE]
    return Inode(
        number=number, mode=mode, size=size_lo | (size_hi << 32),
        flags=flags, links_count=links_count, i_block=i_block,
    )


def inode_location(sb: Superblock, gdt: list[int], number: int) -> int:
    """Absolute byte offset of inode ``number`` in the image."""
    group, index = divmod(number - 1, sb.inodes_per_group)
    return sb.block_offset(gdt[group]) + index * sb.inode_size


def read_inode(image: str, sb: Superblock, gdt: list[int], number: int) -> Inode | None:
    """Read and parse a single inode by number from the image."""
    if number < 1:
        return None
    group = (number - 1) // sb.inodes_per_group
    if group >= len(gdt):
        return None
    raw = read_image(image, inode_location(sb, gdt, number), sb.inode_size)
    return parse(raw, number)


def iter_inodes(image: str, sb: Superblock, gdt: list[int]) -> Iterator[Inode]:
    """Yield every in-use inode by scanning all imaged inode tables.

    Missing/corrupt regions on a partial image parse as unused and are skipped —
    the same tolerance the NTFS $MFT scan relies on.
    """
    span = sb.inodes_per_group * sb.inode_size
    for group, itab in enumerate(gdt):
        table = read_image(image, sb.block_offset(itab), span)
        for idx in range(sb.inodes_per_group):
            raw = table[idx * sb.inode_size:(idx + 1) * sb.inode_size]
            node = parse(raw, group * sb.inodes_per_group + idx + 1)
            if node is not None and node.links_count > 0:
                yield node


def read_gdt(image: str, sb: Superblock) -> list[int]:
    """Read the group descriptor table from the image and return inode tables."""
    length = group_desc.gdt_byte_length(sb)
    # Round up to whole blocks (the GDT occupies complete blocks on disk).
    blocks = (length + sb.block_size - 1) // sb.block_size
    data = read_image(image, sb.block_offset(sb.gdt_block), blocks * sb.block_size)
    return group_desc.parse(data, sb)
