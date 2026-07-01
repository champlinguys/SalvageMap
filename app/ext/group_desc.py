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

"""Parse the ext block-group descriptor table.

One descriptor per block group; each tells us where that group's *inode table*
lives. The inode tables together are the ext analogue of the NTFS ``$MFT`` —
the catalog of every file — except distributed across groups instead of one
contiguous run. We only need the inode-table block here (block/inode bitmaps are
not required for extent-based recovery).

A descriptor is 32 bytes, or 64 with the 64-bit feature (then the high halves of
the block pointers are present).
"""

from __future__ import annotations

import struct

from app.ext.superblock import Superblock


def gdt_byte_length(sb: Superblock) -> int:
    """Bytes spanned by the whole descriptor table (unrounded)."""
    return sb.n_groups * sb.desc_size


def parse(table: bytes, sb: Superblock) -> list[int]:
    """Return the inode-table starting block for each block group."""
    blocks: list[int] = []
    for g in range(sb.n_groups):
        entry = table[g * sb.desc_size:(g + 1) * sb.desc_size]
        if len(entry) < 32:
            break
        lo = struct.unpack_from("<I", entry, 0x08)[0]
        hi = 0
        # The 64-bit high half is at 0x28 (u32); only read it if the descriptor
        # is actually that long (a truncated trailing entry on a partial image
        # may be 32–43 bytes — use just the low half rather than raising).
        if sb.desc_size >= 64 and sb.incompat_64bit and len(entry) >= 0x2C:
            hi = struct.unpack_from("<I", entry, 0x28)[0]
        blocks.append(lo | (hi << 32))
    return blocks
