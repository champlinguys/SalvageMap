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

"""Parse the HFS+ volume header.

The volume header lives 1024 bytes into the volume (it is the HFS+ analogue of
the NTFS boot sector / ext superblock) and holds the geometry needed to locate
everything else: the allocation block size and, crucially, the on-disk extents
of the *special files* — chiefly the Catalog File (the B-tree of files and
folders) and the Extents Overflow File (extra extents for fragmented forks).

A special file's location is an :class:`HFSPlusForkData`: its logical size and
the first 8 extents inline; any further extents live in the Extents Overflow
File. Allocation blocks are counted from the start of the volume, so the byte
offset of block ``n`` is ``volume_offset + n * block_size``.

Field offsets are within the 512-byte volume header and were verified against a
real ``mkfs.hfsplus`` volume. All fields are big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

VOLUME_HEADER_OFFSET = 1024
SIGNATURE_HFSPLUS = 0x482B   # 'H+'  HFS Plus
SIGNATURE_HFSX = 0x4858      # 'HX'  HFSX (case-sensitive)

# HFSPlusForkData byte offsets of the special files within the volume header.
FORK_ALLOCATION = 0x70
FORK_EXTENTS = 0xC0
FORK_CATALOG = 0x110
FORK_ATTRIBUTES = 0x150
FORK_STARTUP = 0x190

_FORK_SIZE = 80              # logicalSize(8) clumpSize(4) totalBlocks(4) + 8 extents(64)
_HIGHEST_FIELD = FORK_CATALOG + _FORK_SIZE   # last fork we actually use (0x190)


@dataclass
class ForkData:
    """An HFS+ fork: its logical size and inline extents (start_block, count)."""
    logical_size: int
    total_blocks: int
    extents: list[tuple[int, int]]


@dataclass
class VolumeHeader:
    volume_offset: int       # byte offset of the volume within the whole disk
    signature: int
    block_size: int
    total_blocks: int
    catalog: ForkData
    extents_overflow: ForkData
    allocation: ForkData

    def block_offset(self, block: int) -> int:
        """Absolute byte offset of an allocation block (incl. the volume offset)."""
        return self.volume_offset + block * self.block_size


def _parse_fork(data: bytes, off: int) -> ForkData:
    logical = struct.unpack_from(">Q", data, off)[0]
    total = struct.unpack_from(">I", data, off + 12)[0]
    extents: list[tuple[int, int]] = []
    for i in range(8):
        start = struct.unpack_from(">I", data, off + 16 + i * 8)[0]
        count = struct.unpack_from(">I", data, off + 16 + i * 8 + 4)[0]
        if count:
            extents.append((start, count))
    return ForkData(logical_size=logical, total_blocks=total, extents=extents)


def parse(data: bytes, volume_offset: int = 0) -> VolumeHeader | None:
    """Parse a volume header from its bytes. None if it isn't a valid HFS+ header.

    On a half-recovered image this region may be zeros/garbage, so we validate
    the signature and block size and return None rather than trusting junk.
    """
    # Guard the full span we read: the highest field is the catalog fork's last
    # extent at 0x190. A short slice (truncated read on a half-imaged volume)
    # returns None rather than letting struct.unpack_from raise.
    if len(data) < _HIGHEST_FIELD:
        return None
    signature = struct.unpack_from(">H", data, 0x00)[0]
    if signature not in (SIGNATURE_HFSPLUS, SIGNATURE_HFSX):
        return None
    block_size = struct.unpack_from(">I", data, 0x28)[0]
    total_blocks = struct.unpack_from(">I", data, 0x2C)[0]
    # Allocation block size is a power of two, at least 512 bytes.
    if block_size < 512 or (block_size & (block_size - 1)) != 0:
        return None

    return VolumeHeader(
        volume_offset=volume_offset,
        signature=signature,
        block_size=block_size,
        total_blocks=total_blocks,
        catalog=_parse_fork(data, FORK_CATALOG),
        extents_overflow=_parse_fork(data, FORK_EXTENTS),
        allocation=_parse_fork(data, FORK_ALLOCATION),
    )
