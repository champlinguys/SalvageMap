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

"""Registry of unlocked (decrypt-on-read) views of images.

An encrypted volume is a *read transform*, not a filesystem: once a BitLocker
volume is unlocked, every parser should see plaintext and nothing else should
change. Rather than thread a reader object through every parse function (they
all take an image path), an unlocked image is registered here and the two places
that touch raw offsets consult it:

* :func:`app.core.recovery.read_image` — all parsing reads go through it, so
  registering a source is what makes the NTFS/ext/HFS+ parsers see plaintext;
* :meth:`app.core.recovery.TargetedRecovery._run_domain` — translates the
  filesystem's (plaintext) ranges into the physical sectors ddrescue images.

Sources are keyed by absolute image path and live only for the session; an
unlock is never written to disk, so reopening the image asks for the key again.
"""

from __future__ import annotations

import os
from typing import Any

_sources: dict[str, Any] = {}


def _key(image: str) -> str:
    return os.path.abspath(image)


def register(image: str, source: Any) -> None:
    """Make ``source`` answer all future reads of ``image``."""
    _sources[_key(image)] = source


def unregister(image: str) -> None:
    """Forget any unlocked view of ``image`` (e.g. a new session opened it)."""
    _sources.pop(_key(image), None)


def clear() -> None:
    _sources.clear()


def source_for(image: str) -> Any | None:
    """The decrypting source for ``image``, or None if it isn't unlocked."""
    return _sources.get(_key(image))


def physical_ranges(image: str, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Map filesystem (plaintext) ranges to the physical ranges to image."""
    source = source_for(image)
    return source.physical_ranges(ranges) if source is not None else ranges
