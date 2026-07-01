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

"""Parse ext directory blocks (linear ``ext4_dir_entry_2`` records).

A directory's data blocks are a sequence of variable-length entries:

    0x00 u32 inode      (0 = unused slot)
    0x04 u16 rec_len    (distance to the next entry)
    0x06 u8  name_len
    0x07 u8  file_type  (1 = regular, 2 = directory, ...)
    0x08     name[name_len]

HTree-indexed directories keep their leaf entries in ordinary data blocks too,
so reading every directory data block linearly recovers all names; the index
root block only contributes ``.`` and ``..``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

FT_DIR = 2


@dataclass
class DirEntry:
    inode: int
    file_type: int
    name: str

    @property
    def is_dir(self) -> bool:
        return self.file_type == FT_DIR


def iter_entries(block: bytes) -> Iterator[DirEntry]:
    """Yield directory entries from one directory data block."""
    pos = 0
    n = len(block)
    while pos + 8 <= n:
        inode = struct.unpack_from("<I", block, pos)[0]
        rec_len = struct.unpack_from("<H", block, pos + 4)[0]
        if rec_len < 8 or pos + rec_len > n:
            break
        name_len = block[pos + 6]
        file_type = block[pos + 7]
        if inode != 0 and name_len and pos + 8 + name_len <= n:
            name = block[pos + 8:pos + 8 + name_len].decode("utf-8", errors="replace")
            if name not in (".", ".."):
                yield DirEntry(inode, file_type, name)
        pos += rec_len
