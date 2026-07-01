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

"""Walk an ext4 extent tree into absolute byte ranges.

ext4 stores a file's block map as an extent tree (the equivalent of an NTFS run
list). The tree starts in the inode's 60-byte ``i_block`` area:

    extent header (12 bytes): magic 0xF30A, entry count, depth
    depth 0 (leaf):  ext4_extent     entries -> contiguous on-disk runs
    depth > 0:       ext4_extent_idx entries -> child nodes stored in blocks

Interior nodes live in their own blocks, which must be read back from the image
(``read_block``). On a partial image an unreadable child reads as zeros and is
skipped, so we recover whatever extents survive.
"""

from __future__ import annotations

import struct
from typing import Callable

from app.ext.superblock import Superblock

EXTENT_MAGIC = 0xF30A
_HEADER_SIZE = 12
_ENTRY_SIZE = 12
_UNINIT_LEN = 32768   # ee_len > this marks an uninitialized (still-allocated) extent
_MAX_DEPTH = 8        # extent trees are at most ~5 deep; guard against loops/garbage


def collect_ranges(
    node: bytes,
    sb: Superblock,
    read_block: Callable[[int], bytes],
) -> list[tuple[int, int]]:
    """Return absolute ``(start, length)`` byte ranges for an extent tree node."""
    return collect_ranges_coverage(node, sb, read_block)[0]


def collect_ranges_coverage(
    node: bytes,
    sb: Superblock,
    read_block: Callable[[int], bytes],
) -> tuple[list[tuple[int, int]], bool]:
    """Ranges plus whether the extent tree was walked *completely*.

    ``complete`` is False when an interior (index) node couldn't be read back
    from the image — its child extents can't be located, so a fragmented file
    resolved from it is known-incomplete (the ext analog of an unrecovered HFS+
    Extents Overflow File or a missing NTFS extension record).
    """
    out: list[tuple[int, int]] = []
    # ``visited`` bounds breadth as ``_MAX_DEPTH`` bounds depth: on a garbage image
    # many interior entries could each point at blocks that also carry the magic,
    # so we never descend into the same physical block twice (also breaks cycles).
    state = {"complete": True}
    _walk(node, sb, read_block, out, _MAX_DEPTH, set(), state)
    return out, state["complete"]


def _walk(node, sb, read_block, out, budget, visited, state) -> None:
    if len(node) < _HEADER_SIZE:
        state["complete"] = False
        return
    if budget < 0:
        state["complete"] = False
        return
    magic, entries, _max, depth = struct.unpack_from("<HHHH", node, 0)
    if magic != EXTENT_MAGIC:
        state["complete"] = False
        return
    for i in range(entries):
        base = _HEADER_SIZE + i * _ENTRY_SIZE
        if base + _ENTRY_SIZE > len(node):
            break
        if depth == 0:
            ee_len = struct.unpack_from("<H", node, base + 4)[0]
            length = ee_len - _UNINIT_LEN if ee_len > _UNINIT_LEN else ee_len
            if length <= 0:
                continue
            start_hi = struct.unpack_from("<H", node, base + 6)[0]
            start_lo = struct.unpack_from("<I", node, base + 8)[0]
            phys = (start_hi << 32) | start_lo
            out.append((sb.block_offset(phys), length * sb.block_size))
        else:
            leaf_lo = struct.unpack_from("<I", node, base + 4)[0]
            leaf_hi = struct.unpack_from("<H", node, base + 8)[0]
            child = (leaf_hi << 32) | leaf_lo
            if child in visited:
                continue
            visited.add(child)
            block = read_block(child)
            if block and any(block):
                _walk(block, sb, read_block, out, budget - 1, visited, state)
            else:
                state["complete"] = False   # interior node not imaged -> tail lost
