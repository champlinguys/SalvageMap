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

"""Find the BitLocker volumes in a (possibly partial) disk image.

The partition table is the obvious way to locate a volume, and we try it first —
but on a failing drive sector 0 is often exactly what didn't come back. This
image is the case in point: its first sector is a hole, so no table can be read,
yet the BitLocker volume sits intact at the usual 1 MiB mark. So we also sweep
the low part of the disk for the ``-FVE-FS-`` signature on MiB boundaries, which
finds a volume whose table is gone without a full-disk scan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.bitlocker import fve

# Sweep every MiB boundary up to here when the partition table is unreadable.
# Partitions start MiB-aligned on anything modern, and the first data volume is
# within the first few hundred MiB even behind a recovery + EFI + MSR prefix.
SWEEP_STEP = 1 << 20
SWEEP_LIMIT = 512 << 20


@dataclass(frozen=True)
class LockedVolume:
    """A BitLocker volume found in an image, with its metadata parsed."""
    offset: int              # absolute byte offset of the volume in the image
    size: int                # bytes (FVE's own figure where available)
    metadata: fve.FveMetadata
    source: str = ""         # how we found it ("partition table" / "signature scan")

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def method_name(self) -> str:
        return self.metadata.method_name


def _read(path: str, offset: int, length: int) -> bytes:
    """Raw (never decrypted) read from the image; b"" past the end or on error."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)
    except OSError:
        return b""


def volume_reader(path: str, volume_offset: int):
    """A volume-relative raw reader, as :func:`app.bitlocker.fve.parse` wants."""
    return lambda offset, length: _read(path, volume_offset + offset, length)


def parse_at(path: str, offset: int) -> fve.FveMetadata | None:
    """Parse the FVE metadata of a volume at ``offset``, or None if not BitLocker."""
    return fve.parse(volume_reader(path, offset))


def is_bitlocker_at(path: str, offset: int) -> bool:
    return fve.looks_like_bitlocker(_read(path, offset, 512))


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
                 sweep_limit: int = SWEEP_LIMIT) -> list[LockedVolume]:
    """All BitLocker volumes discoverable in ``path``, nearest offset first.

    Looks at ``extra_offsets`` (e.g. the session's current volume offset), then
    the partition table, then MiB boundaries up to ``sweep_limit``.
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
        if not is_bitlocker_at(path, offset):
            continue
        md = parse_at(path, offset)
        if md is None:
            continue   # signature but no readable metadata copy — not usable
        size = md.encrypted_volume_size or table.get(offset) or max(
            image_size - offset, 0)
        found.append(LockedVolume(offset, size, md, sources[offset]))
    return found
