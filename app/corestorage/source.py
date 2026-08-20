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

"""A decrypt-on-read view of a CoreStorage volume inside a disk image.

The counterpart to :class:`app.bitlocker.source.BitLockerSource`, satisfying the
same contract for :mod:`app.core.decrypt`: ``read`` and ``physical_ranges``. The
image always holds the ciphertext ddrescue read — nothing is ever decrypted onto
disk.

Two coordinate systems meet here, as with BitLocker, but they diverge much more:

* **Plaintext offsets** are what the HFS+ parser talks in. The volume is
  presented as though the logical volume began at the CoreStorage partition
  start, so a volume offset picked from the partition table just works and the
  HFS+ plan needs no changes at all.
* **Physical offsets** are where those bytes actually live, which is what
  ddrescue must be told to image. For BitLocker only the boot region moves; for
  CoreStorage the whole logical volume sits behind the CoreStorage metadata (64
  MiB on the reference drive) and may be split into segments.

:meth:`physical_ranges` therefore goes through the *measured* segment map (see
:mod:`app.corestorage.segments`) rather than a fixed shift.
"""

from __future__ import annotations

from typing import Callable

from app.corestorage import cs
from app.corestorage.keys import UnlockedVolume
from app.corestorage.segments import Mapping

RawRead = Callable[[int, int], bytes]
Range = tuple[int, int]


class CoreStorageSource:
    """Plaintext view of the CoreStorage volume at ``volume_offset`` in an image.

    All offsets in the public API are absolute *disk* offsets, the same
    coordinates the rest of the app uses.
    """

    def __init__(self, unlocked: UnlockedVolume, mapping: Mapping,
                 header: cs.VolumeHeader | None = None):
        self._unlocked = unlocked
        self.mapping = mapping
        self.header = header
        self.volume_offset = unlocked.volume_offset
        self.volume_size = unlocked.size

    # Counterpart to BitLockerSource.scheme_name; see there.
    scheme_name = "FileVault 2"

    @property
    def method_name(self) -> str:
        return self.header.method_name if self.header else "AES-128-XTS"

    @property
    def description(self) -> str:
        name = self._unlocked.name
        base = self.header.description if self.header else "CoreStorage (FileVault 2)"
        return f"{base} — {name}" if name else base

    @property
    def name(self) -> str:
        return self._unlocked.name

    @property
    def volume_end(self) -> int:
        return self.volume_offset + self.volume_size

    @property
    def mapping_summary(self) -> str:
        return self.mapping.summary

    # --- reading ----------------------------------------------------------
    def read(self, raw_read: RawRead, offset: int, length: int) -> bytes:
        """Read ``length`` plaintext bytes at disk ``offset``.

        Reads inside the volume are served by libfvde from the image; anything
        outside it (the partition table, the EFI and Booter partitions) falls
        through to ``raw_read`` untouched, so an image holding a whole Mac disk
        still reads normally everywhere else.
        """
        if length <= 0:
            return b""
        chunks: list[bytes] = []
        pos, end = offset, offset + length
        for lo, hi, inside in ((offset, min(end, self.volume_offset), False),
                               (max(offset, self.volume_offset),
                                min(end, self.volume_end), True),
                               (max(offset, self.volume_end), end, False)):
            lo, hi = max(lo, pos), max(hi, pos)
            if hi <= lo:
                continue
            if inside:
                chunks.append(self._unlocked.read(lo - self.volume_offset, hi - lo))
            else:
                chunks.append(_padded(raw_read(lo, hi - lo), hi - lo))
            pos = hi
        if end > pos:
            chunks.append(_padded(raw_read(pos, end - pos), end - pos))
        return b"".join(chunks)

    # --- offset mapping ---------------------------------------------------
    def physical_ranges(self, ranges: list[Range]) -> list[Range]:
        """Translate plaintext ``(start, length)`` ranges to physical ones.

        Ranges outside the volume pass through; ranges inside go through the
        measured segment map. When the mapping could not be measured the map
        returns the whole partition, so ddrescue images a superset — never the
        wrong sectors.
        """
        out: list[Range] = []
        inside: list[Range] = []
        for start, length in ranges:
            if length <= 0:
                continue
            end = start + length
            lo, hi = max(start, self.volume_offset), min(end, self.volume_end)
            if hi > lo:
                inside.append((lo - self.volume_offset, hi - lo))
            if start < self.volume_offset:
                out.append((start, min(end, self.volume_offset) - start))
            if end > self.volume_end:
                out.append((max(start, self.volume_end),
                            end - max(start, self.volume_end)))
        if inside:
            out += self.mapping.physical_ranges(inside)
        return out

    def close(self) -> None:
        self._unlocked.close()


def _padded(data: bytes, length: int) -> bytes:
    """``data`` zero-filled up to ``length`` (a hole in the image reads as zeros)."""
    if len(data) >= length:
        return data[:length]
    return data + b"\x00" * (length - len(data))
