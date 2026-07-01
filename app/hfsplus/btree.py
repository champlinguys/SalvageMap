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

"""Read an HFS+ B-tree (Catalog or Extents Overflow) from a recovered image.

Both the Catalog File and the Extents Overflow File are B-trees stored in a
special file's fork. The fork may be fragmented, so a logical B-tree node offset
must be mapped through the fork's resolved byte ranges back to absolute image
offsets — that mapping is what :class:`BTree` provides.

Node 0 is the *header node*: a 14-byte ``BTNodeDescriptor`` followed by a
``BTHeaderRec`` carrying ``nodeSize`` and the index of the first leaf node. Leaf
records are then read by walking the leaves via each node's forward link
(``fLink``); within a node, a trailing array of big-endian ``u16`` offsets (one
per record, plus a free-space sentinel) delimits the records.

We tolerate a partial image: an unreadable node (zeros) ends the walk rather
than raising, recovering whatever leaves precede the damage.
"""

from __future__ import annotations

import struct
from typing import Iterator

from app.core.recovery import read_image

# BTNodeDescriptor.kind values.
KIND_LEAF = -1
KIND_INDEX = 0
KIND_HEADER = 1

_DESCRIPTOR_SIZE = 14
_HEADER_REC_OFFSET = _DESCRIPTOR_SIZE   # BTHeaderRec follows the node descriptor
_NODE_SIZE_OFFSET = _HEADER_REC_OFFSET + 18   # nodeSize u16 within the header node
_FIRST_LEAF_OFFSET = _HEADER_REC_OFFSET + 10  # firstLeafNode u32
_MAX_NODES = 1 << 22   # guard against a looping/garbage leaf chain


class BTree:
    """A B-tree backed by a fork's resolved byte ranges within the image."""

    def __init__(self, image: str, ranges: list[tuple[int, int]]):
        self._image = image
        self._ranges = ranges
        self._span = sum(length for _start, length in ranges)
        self.node_size = 0
        self.first_leaf = 0
        self._ok = self._read_header()

    @property
    def ok(self) -> bool:
        return self._ok

    # --- logical fork I/O -------------------------------------------------
    def _read_logical(self, pos: int, length: int) -> bytes:
        """Read ``length`` bytes at logical fork offset ``pos`` across extents."""
        if pos < 0 or length <= 0:
            return b""
        out = bytearray()
        cur = 0   # logical position of the start of the current extent
        for start, ext_len in self._ranges:
            if len(out) >= length:
                break
            ext_end = cur + ext_len
            if pos < ext_end and pos + length > cur:
                lo = max(pos, cur)
                hi = min(pos + length, ext_end)
                out += read_image(self._image, start + (lo - cur), hi - lo)
            cur = ext_end
        return bytes(out)

    def read_node(self, index: int) -> bytes:
        if self.node_size <= 0:
            return b""
        return self._read_logical(index * self.node_size, self.node_size)

    # --- header -----------------------------------------------------------
    def _read_header(self) -> bool:
        head = self._read_logical(0, 0x100)
        if len(head) < _NODE_SIZE_OFFSET + 2:
            return False
        node_size = struct.unpack_from(">H", head, _NODE_SIZE_OFFSET)[0]
        if node_size < 512 or (node_size & (node_size - 1)) != 0:
            return False
        if node_size > self._span:
            return False
        self.node_size = node_size
        self.first_leaf = struct.unpack_from(">I", head, _FIRST_LEAF_OFFSET)[0]
        return True

    # --- records ----------------------------------------------------------
    @staticmethod
    def _node_records(node: bytes, node_size: int) -> Iterator[bytes]:
        if len(node) < _DESCRIPTOR_SIZE:
            return
        num_records = struct.unpack_from(">H", node, 10)[0]
        # The offset array sits at the end of the node: num_records+1 big-endian
        # u16 offsets, the last marking the start of free space.
        offsets = []
        for i in range(num_records + 1):
            pos = node_size - 2 * (i + 1)
            if pos < 0 or pos + 2 > len(node):
                return
            offsets.append(struct.unpack_from(">H", node, pos)[0])
        for i in range(num_records):
            start, end = offsets[i], offsets[i + 1]
            if start < _DESCRIPTOR_SIZE or end > node_size or end <= start:
                continue
            yield node[start:end]

    def iter_leaf_records(self) -> Iterator[bytes]:
        """Yield every leaf record (raw bytes) by walking the leaf-node chain."""
        if not self._ok:
            return
        index = self.first_leaf
        seen = 0
        visited: set[int] = set()
        while index and seen < _MAX_NODES and index not in visited:
            visited.add(index)
            seen += 1
            node = self.read_node(index)
            if len(node) < _DESCRIPTOR_SIZE:
                break
            kind = struct.unpack_from(">b", node, 8)[0]
            f_link = struct.unpack_from(">I", node, 0)[0]
            if kind == KIND_LEAF:
                yield from self._node_records(node, self.node_size)
            index = f_link


def split_key_data(record: bytes) -> tuple[bytes, bytes]:
    """Split a B-tree leaf record into ``(key_body, data)``.

    The record begins with a ``u16`` key length; the data follows the key,
    2-byte aligned (HFS+ records use big keys with a leading length word).
    """
    if len(record) < 2:
        return b"", b""
    key_len = struct.unpack_from(">H", record, 0)[0]
    data_off = 2 + key_len
    if data_off & 1:        # align the data to an even offset
        data_off += 1
    key_body = record[2:2 + key_len]
    data = record[data_off:] if data_off <= len(record) else b""
    return key_body, data
