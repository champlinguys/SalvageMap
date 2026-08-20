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

"""Find the CoreStorage volumes in a (possibly partial) disk image.

Mirrors :mod:`app.bitlocker.detect`, and for the same reason: the partition
table is the obvious way to locate a volume, but on a failing drive sector 0 is
often exactly what didn't come back. So the table is tried first, then the low
part of the disk is swept on MiB boundaries for the CoreStorage signature, which
finds a volume whose table is gone without scanning terabytes.

A CoreStorage volume group can hold several logical volumes (that is how Fusion
Drives work). This module finds the *physical* volume — the container; the
logical volumes inside it only become visible once libfvde has opened it, which
is :mod:`app.corestorage.keys`' job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.corestorage import cs

# Sweep every MiB boundary up to here when the partition table is unreadable.
# On a Mac disk the CoreStorage volume sits behind a 200 MB EFI partition, so it
# is well inside this window; sweeping further would cost real time on a 14 TB
# image for no gain.
SWEEP_STEP = 1 << 20
SWEEP_LIMIT = 512 << 20


@dataclass(frozen=True)
class LockedVolume:
    """A CoreStorage volume found in an image, with its header parsed."""
    offset: int              # absolute byte offset of the volume in the image
    size: int                # bytes (the header's own figure where available)
    header: cs.VolumeHeader
    source: str = ""         # how we found it ("partition table" / "signature scan")

    @property
    def description(self) -> str:
        return self.header.description

    @property
    def method_name(self) -> str:
        return self.header.method_name

    @property
    def identifier(self) -> str:
        return self.header.identifier

    @property
    def is_encrypted(self) -> bool:
        return self.header.is_encrypted


def _read(path: str, offset: int, length: int) -> bytes:
    """Raw (never decrypted) read from the image; b"" past the end or on error."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)
    except OSError:
        return b""


def volume_reader(path: str, volume_offset: int):
    """A volume-relative raw reader, as :func:`app.corestorage.cs.parse_with` wants."""
    return lambda offset, length: _read(path, volume_offset + offset, length)


def parse_at(path: str, offset: int) -> cs.VolumeHeader | None:
    """Parse the CoreStorage header at ``offset``, or None if there isn't one."""
    return cs.parse(_read(path, offset, cs.HEADER_SIZE))


def is_corestorage_at(path: str, offset: int) -> bool:
    return cs.looks_like_corestorage(_read(path, offset, cs.HEADER_SIZE))


def _table_offsets(path: str) -> dict[int, int]:
    """``{start: size}`` from the image's partition table (empty if unreadable)."""
    from app.core import partition
    try:
        return {p.start: p.size for p in partition.scan_device(path)}
    except OSError:
        return {}


def _image_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def find_volumes(path: str, extra_offsets: tuple[int, ...] = (),
                 sweep_limit: int = SWEEP_LIMIT,
                 encrypted_only: bool = True) -> list[LockedVolume]:
    """All CoreStorage volumes discoverable in ``path``, nearest offset first.

    Looks at ``extra_offsets`` (e.g. the session's current volume offset), then
    the partition table, then MiB boundaries up to ``sweep_limit``.

    ``encrypted_only`` drops unencrypted CoreStorage containers: they are real
    CoreStorage but there is nothing to unlock, and offering them would send the
    tech hunting for a password that was never set.
    """
    table = _table_offsets(path)
    image_size = _image_size(path)

    sources: dict[int, str] = {}
    for offset in extra_offsets:
        sources.setdefault(offset, "current volume offset")
    for offset in sorted(table):
        sources.setdefault(offset, "partition table")
    for offset in range(0, min(sweep_limit, image_size or sweep_limit), SWEEP_STEP):
        sources.setdefault(offset, "signature scan")

    found: list[LockedVolume] = []
    for offset in sorted(sources):
        header = parse_at(path, offset)
        if header is None:
            continue
        if encrypted_only and not header.is_encrypted:
            continue
        # The header's size is the physical volume's own figure and matched the
        # GPT entry byte-for-byte on the reference drive; prefer it, but fall
        # back to the table (and then the image) when the header is truncated.
        size = header.physical_volume_size or table.get(offset) or max(
            image_size - offset, 0)
        found.append(LockedVolume(offset, size, header, sources[offset]))
    return found
