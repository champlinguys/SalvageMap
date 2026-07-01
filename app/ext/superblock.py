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

"""Parse the ext2/3/4 superblock.

The superblock lives 1024 bytes into the volume and holds the geometry needed to
locate everything else: block size, how many inodes/blocks per group, the inode
size, and which features are in use (notably 64-bit, which widens the group
descriptors). It is the ext analogue of the NTFS boot sector.

Field offsets are within the 1024-byte superblock and were verified against a
``mke2fs -t ext4`` image.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SUPERBLOCK_OFFSET = 1024
SUPERBLOCK_SIZE = 1024
EXT_MAGIC = 0xEF53

INCOMPAT_64BIT = 0x80
EXTENTS_FL = 0x80000          # i_flags: inode uses an extent tree
INLINE_DATA_FL = 0x10000000  # i_flags: file content is inline in the inode

# i_mode top nibble.
S_IFMT = 0xF000
S_IFDIR = 0x4000
S_IFREG = 0x8000
S_IFLNK = 0xA000


@dataclass
class Superblock:
    volume_offset: int       # byte offset of the volume within the whole disk
    block_size: int
    inode_size: int
    inodes_count: int
    blocks_count: int
    blocks_per_group: int
    inodes_per_group: int
    first_data_block: int
    desc_size: int           # group-descriptor size: 32, or 64 with INCOMPAT_64BIT
    incompat_64bit: bool
    first_ino: int           # first non-reserved inode (user files start here)

    @property
    def n_groups(self) -> int:
        return (self.inodes_count + self.inodes_per_group - 1) // self.inodes_per_group

    @property
    def gdt_block(self) -> int:
        """Block holding the start of the group descriptor table."""
        return self.first_data_block + 1

    def block_offset(self, block: int) -> int:
        """Absolute byte offset of a filesystem block (incl. the volume offset)."""
        return self.volume_offset + block * self.block_size


def parse(data: bytes, volume_offset: int = 0) -> Superblock | None:
    """Parse a superblock from its 1024 bytes. None if it isn't a valid ext sb.

    On a half-recovered image this region may be zeros/garbage, so we validate
    the magic and geometry and return None rather than trusting junk.
    """
    # Guard the full span we read: the highest field is blocks_hi at 0x150 (u32).
    # On a half-imaged volume this region can be short; return None (skip) rather
    # than letting struct.unpack_from raise.
    if len(data) < 0x154:
        return None
    if struct.unpack_from("<H", data, 0x38)[0] != EXT_MAGIC:
        return None

    inodes_count = struct.unpack_from("<I", data, 0x00)[0]
    blocks_lo = struct.unpack_from("<I", data, 0x04)[0]
    blocks_hi = struct.unpack_from("<I", data, 0x150)[0]
    first_data_block = struct.unpack_from("<I", data, 0x14)[0]
    log_block_size = struct.unpack_from("<I", data, 0x18)[0]
    blocks_per_group = struct.unpack_from("<I", data, 0x20)[0]
    inodes_per_group = struct.unpack_from("<I", data, 0x28)[0]
    rev_level = struct.unpack_from("<I", data, 0x4C)[0]
    first_ino = struct.unpack_from("<I", data, 0x54)[0]
    incompat = struct.unpack_from("<I", data, 0x60)[0]

    if log_block_size > 6:  # block size 1KiB..64KiB
        return None
    block_size = 1024 << log_block_size
    if not inodes_per_group or not blocks_per_group:
        return None

    # Old (rev 0) filesystems have no s_first_ino field; the fixed value is 11.
    if rev_level < 1 or first_ino == 0:
        first_ino = 11

    # Inode size is 128 on old (rev 0) filesystems; dynamic ones store it at 0x58.
    inode_size = 128
    if rev_level >= 1:
        inode_size = struct.unpack_from("<H", data, 0x58)[0] or 128

    incompat_64bit = bool(incompat & INCOMPAT_64BIT)
    desc_size = 32
    if incompat_64bit:
        desc_size = struct.unpack_from("<H", data, 0xFE)[0] or 64

    return Superblock(
        volume_offset=volume_offset,
        block_size=block_size,
        inode_size=inode_size,
        inodes_count=inodes_count,
        blocks_count=blocks_lo | (blocks_hi << 32),
        blocks_per_group=blocks_per_group,
        inodes_per_group=inodes_per_group,
        first_data_block=first_data_block,
        desc_size=desc_size,
        incompat_64bit=incompat_64bit,
        first_ino=first_ino,
    )
