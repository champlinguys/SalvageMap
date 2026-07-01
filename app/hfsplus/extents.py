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

"""Resolve an HFS+ fork into absolute byte ranges.

A fork's first 8 extents are stored inline (in the catalog record or the volume
header). A heavily fragmented fork has more than 8 extents; the rest live in the
Extents Overflow File, a B-tree keyed by ``(forkType, fileID, startBlock)``.

:class:`ExtentsOverflow` indexes those overflow records (built once from the
imaged Extents Overflow B-tree); :func:`resolve_fork` then stitches a fork's
inline extents together with any overflow extents into ``(start, length)`` byte
ranges. Everything is relative to the volume start: byte = ``volume_offset +
block * block_size``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from app.hfsplus.volume_header import ForkData, VolumeHeader

FORK_TYPE_DATA = 0x00
FORK_TYPE_RESOURCE = 0xFF

# Reserved catalog node IDs of the special files. Their forks (in the volume
# header) hold only the first 8 extents inline; if a special file is itself
# fragmented past 8 extents, the rest live in the Extents Overflow File keyed by
# these IDs — just like an ordinary file.
HFS_EXTENTS_FILE_ID = 3       # the Extents Overflow File itself (self-describing)
HFS_CATALOG_FILE_ID = 4       # the Catalog File
HFS_ALLOCATION_FILE_ID = 6
HFS_ATTRIBUTES_FILE_ID = 8

_EXTENT_KEY_LENGTH = 10       # forkType(1) pad(1) fileID(4) startBlock(4)
_MAX_OVERFLOW_RECORDS = 1 << 20   # guard against a garbage/looping index


def extent_descriptors(data: bytes, off: int = 0) -> list[tuple[int, int]]:
    """Parse 8 ``HFSPlusExtentDescriptor`` (startBlock u32, blockCount u32) pairs."""
    out: list[tuple[int, int]] = []
    for i in range(8):
        base = off + i * 8
        if base + 8 > len(data):
            break
        start, count = struct.unpack_from(">II", data, base)
        if count:
            out.append((start, count))
    return out


def blocks_to_ranges(blocks: list[tuple[int, int]],
                     vh: VolumeHeader) -> list[tuple[int, int]]:
    """Absolute ``(start, length)`` byte ranges for ``(start_block, count)`` blocks."""
    return [(vh.block_offset(start), count * vh.block_size)
            for start, count in blocks if count]


# Kept as a private alias for callers within the package.
_blocks_to_ranges = blocks_to_ranges


def stitch_blocks(inline: list[tuple[int, int]], total_blocks: int,
                  extra: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], int]:
    """Append overflow ``extra`` extents to ``inline`` until ``total_blocks`` covered.

    Returns ``(blocks, covered)``. When the inline extents already cover the fork
    nothing is appended; a short/empty ``extra`` simply leaves the fork partly
    mapped (``covered < total_blocks``).
    """
    blocks = list(inline)
    covered = sum(count for _start, count in blocks)
    for start_block, count in extra:
        if covered >= total_blocks:
            break
        blocks.append((start_block, count))
        covered += count
    return blocks, covered


@dataclass
class ExtentsOverflow:
    """Index of overflow extents keyed by ``(fork_type, file_id)``.

    Each value is the file-relative-ordered list of extra ``(start_block, count)``
    extents beyond a fork's inline 8. Empty when the Extents Overflow File holds
    nothing (the common case — most files fit in 8 extents).
    """
    by_fork: dict[tuple[int, int], list[tuple[int, int]]] = field(default_factory=dict)

    def extra(self, fork_type: int, file_id: int) -> list[tuple[int, int]]:
        return self.by_fork.get((fork_type, file_id), [])

    @classmethod
    def from_records(cls, records) -> "ExtentsOverflow":
        """Build from ``(key_bytes, data_bytes)`` Extents Overflow leaf records.

        Records are keyed by the file-relative start block, so we sort by it and
        concatenate the extent descriptors in file order.
        """
        staged: dict[tuple[int, int], list[tuple[int, list[tuple[int, int]]]]] = {}
        for n, (key, data) in enumerate(records):
            if n > _MAX_OVERFLOW_RECORDS:
                break
            if len(key) < _EXTENT_KEY_LENGTH:
                continue
            fork_type = key[0]
            file_id, start_block = struct.unpack_from(">II", key, 2)
            descs = extent_descriptors(data)
            if descs:
                staged.setdefault((fork_type, file_id), []).append((start_block, descs))
        by_fork: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for fork_key, parts in staged.items():
            parts.sort(key=lambda p: p[0])
            merged: list[tuple[int, int]] = []
            for _start_block, descs in parts:
                merged += descs
            by_fork[fork_key] = merged
        return cls(by_fork=by_fork)


@dataclass
class ForkResolution:
    """A fork resolved to byte ranges, plus how completely we could map it.

    ``mapped_blocks`` is how many allocation blocks the resolved ranges cover;
    ``total_blocks`` is how many the fork claims. When ``mapped_blocks <
    total_blocks`` the extent map is *incomplete* — the tail extents live in the
    Extents Overflow File, and we couldn't recover enough of it to place them.
    Such a file can never be imaged in full from the current metadata, so it must
    not be reported (or coloured) as complete.
    """
    ranges: list[tuple[int, int]]
    mapped_blocks: int
    total_blocks: int

    @property
    def fully_mapped(self) -> bool:
        return self.mapped_blocks >= self.total_blocks


def resolve_fork_coverage(fork: ForkData, vh: VolumeHeader, file_id: int,
                          fork_type: int,
                          overflow: ExtentsOverflow | None) -> ForkResolution:
    """Resolve a fork to byte ranges and record how completely it was mapped."""
    extra = overflow.extra(fork_type, file_id) if overflow is not None else []
    blocks, covered = stitch_blocks(fork.extents, fork.total_blocks, extra)
    return ForkResolution(blocks_to_ranges(blocks, vh), covered, fork.total_blocks)


def resolve_fork(fork: ForkData, vh: VolumeHeader, file_id: int, fork_type: int,
                 overflow: ExtentsOverflow | None) -> list[tuple[int, int]]:
    """Absolute ``(start, length)`` byte ranges for a fork (inline + overflow)."""
    return resolve_fork_coverage(fork, vh, file_id, fork_type, overflow).ranges


def fork_byte_ranges(fork: ForkData, vh: VolumeHeader) -> list[tuple[int, int]]:
    """Byte ranges for a fork's inline extents only (used for the B-tree forks)."""
    return _blocks_to_ranges(fork.extents, vh)
