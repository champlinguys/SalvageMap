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

"""A decrypt-on-read view of a BitLocker volume inside a disk image.

The image always holds the ciphertext ddrescue read — nothing is ever decrypted
onto disk. This class sits between the image and the filesystem parsers and
answers reads in plaintext, so NTFS/ext/HFS+ parsing, the file tree and the
prioritised-imaging ranges all work unchanged on an encrypted volume.

Two coordinate systems meet here, and the difference matters:

* **Plaintext offsets** are what the filesystem talks in — what the NTFS boot
  sector, its data runs and therefore the file tree all use.
* **Physical offsets** are where those bytes actually live on the platter, which
  is what ddrescue must be told to image.

They are the same everywhere except the volume's first ``header_block_size``
bytes: BitLocker overwrites the volume start with its own boot record and FVE
metadata, relocating the original NTFS boot region to ``header_block_offset``.
:meth:`physical_ranges` applies that shift, which is why "image this folder
first" on an unlocked volume targets the right sectors.
"""

from __future__ import annotations

from typing import Callable

from app.bitlocker import fve
from app.bitlocker.keys import SECTOR, SectorDecryptor, VolumeKeys

# Cap on the per-call decrypt buffer: bounds memory while keeping reads large.
MAX_BATCH = 1 << 20

RawRead = Callable[[int, int], bytes]
Range = tuple[int, int]


class BitLockerSource:
    """Plaintext view of the BitLocker volume at ``volume_offset`` in an image.

    All offsets in the public API are absolute *disk* offsets (the same
    coordinates the rest of the app uses), not volume-relative ones.
    """

    def __init__(self, volume_offset: int, volume_size: int, keys: VolumeKeys,
                 header_block_offset: int = 0, header_block_size: int = 0,
                 description: str = ""):
        self.volume_offset = volume_offset
        self.volume_size = volume_size
        self.keys = keys
        self.header_block_offset = header_block_offset
        self.header_block_size = header_block_size
        self.description = description
        self._decryptor = SectorDecryptor(keys)

    @classmethod
    def from_metadata(cls, volume_offset: int, volume_size: int,
                      md: fve.FveMetadata, keys: VolumeKeys) -> "BitLockerSource":
        # The FVE metadata's own idea of the volume size beats a partition
        # table's: it lives inside the volume, so it can't be a generation stale.
        size = volume_size
        if md.encrypted_volume_size:
            size = md.encrypted_volume_size
        return cls(volume_offset, size, keys,
                   header_block_offset=md.header_block_offset,
                   header_block_size=md.header_block_size,
                   description=md.description)

    # Names the encryption scheme for the UI; its CoreStorage counterpart
    # answers "FileVault 2", so status text can stay scheme-agnostic.
    scheme_name = "BitLocker"

    @property
    def method_name(self) -> str:
        return self.keys.method_name

    @property
    def volume_end(self) -> int:
        return self.volume_offset + self.volume_size

    # --- offset mapping ---------------------------------------------------
    def _segments(self, start: int, length: int) -> list[tuple[int, int, bool]]:
        """Split a plaintext disk range into ``(physical_start, n, encrypted)``.

        Anything outside the volume passes through untouched, so an image
        holding a partition table plus other volumes still reads normally.
        """
        segments: list[tuple[int, int, bool]] = []
        pos, end = start, start + length
        # The relocated header block, in plaintext disk coordinates.
        hdr_end = self.volume_offset + self.header_block_size
        for bound_lo, bound_hi, shift in (
            (0, self.volume_offset, None),                       # before volume
            (self.volume_offset, hdr_end, self.header_block_offset),
            (hdr_end, self.volume_end, 0),                       # body
        ):
            lo, hi = max(pos, bound_lo), min(end, bound_hi)
            if hi > lo:
                if shift is None:
                    segments.append((lo, hi - lo, False))
                else:
                    segments.append((lo + shift, hi - lo, True))
                pos = hi
        if end > pos:  # past the volume
            segments.append((pos, end - pos, False))
        return segments

    def physical_ranges(self, ranges: list[Range]) -> list[Range]:
        """Translate plaintext ``(start, length)`` ranges to physical ones.

        This is what turns filesystem-derived ranges into something ddrescue can
        image. Only the relocated boot region actually moves; everything else
        maps 1:1, which is why BitLocker costs nothing in imaging accuracy.
        """
        out: list[Range] = []
        for start, length in ranges:
            if length <= 0:
                continue
            out += [(phys, n) for phys, n, _enc in self._segments(start, length)]
        return out

    # --- reading ----------------------------------------------------------
    def read(self, raw_read: RawRead, offset: int, length: int) -> bytes:
        """Read ``length`` plaintext bytes at disk ``offset``.

        ``raw_read(offset, length)`` reads the underlying (encrypted) image. A
        short read — a hole in a partial rescue — is zero-filled, so callers get
        the length they asked for and the parsers see (invalid) data rather than
        a truncated buffer.
        """
        if length <= 0:
            return b""
        chunks: list[bytes] = []
        for phys, n, encrypted in self._segments(offset, length):
            if not encrypted:
                chunks.append(_padded(raw_read(phys, n), n))
            else:
                chunks.append(self._read_encrypted(raw_read, phys, n))
        return b"".join(chunks)

    def _read_encrypted(self, raw_read: RawRead, phys: int, length: int) -> bytes:
        """Decrypt ``length`` bytes of ciphertext at physical offset ``phys``.

        Reads are widened to whole sectors (the crypto unit) and batched, so a
        big sequential read is a handful of large reads, not one per sector.
        """
        out: list[bytes] = []
        done = 0
        while done < length:
            pos = phys + done
            sector_base = pos - (pos % SECTOR)
            in_sector = pos - sector_base
            want = min(length - done + in_sector, MAX_BATCH)
            n_sectors = -(-want // SECTOR)   # round up
            span = n_sectors * SECTOR
            data = _padded(raw_read(sector_base, span), span)
            # The data unit is the *volume-relative* sector number.
            plain = self._decryptor.decrypt(
                data, (sector_base - self.volume_offset) // SECTOR)
            take = min(span - in_sector, length - done)
            out.append(plain[in_sector:in_sector + take])
            done += take
        return b"".join(out)


def _padded(data: bytes, length: int) -> bytes:
    """``data`` zero-filled up to ``length`` (a hole in the image reads as zeros)."""
    if len(data) >= length:
        return data[:length]
    return data + b"\x00" * (length - len(data))
