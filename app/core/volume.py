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

"""Map a volume (by filesystem tag) to the FilesystemPlan that recovers it.

Kept separate from the engine so that ``app/core/recovery.py`` need not import
any concrete plan at module load (avoids an import cycle: plans import the engine
for ``Phase``/``PhaseHandler``/``FilesystemPlan``).
"""

from __future__ import annotations

from app.core import partition
from app.core.recovery import FilesystemPlan, read_image


def plan_for_fs(fs_type: str) -> FilesystemPlan:
    """Return the plan for a filesystem tag, defaulting to NTFS.

    ext support is optional at import time (it arrives with ``app/ext/``); if the
    package isn't present we fall back to NTFS so the engine still runs.
    """
    if fs_type == "ext":
        try:
            from app.ext.plan import ExtPlan
            return ExtPlan()
        except ImportError:
            pass
    if fs_type == "hfsplus":
        try:
            from app.hfsplus.plan import HfsPlusPlan
            return HfsPlusPlan()
        except ImportError:
            pass
    from app.ntfs.plan import NtfsPlan
    return NtfsPlan()


def detect_filesystem(image: str, volume_offset: int) -> FilesystemPlan | None:
    """Identify the filesystem at ``volume_offset`` in the image and pick a plan.

    Returns ``None`` if the region isn't imaged yet or holds no recognised
    filesystem (the caller then images more, or falls back to a default plan).
    """
    try:
        head = read_image(image, volume_offset, partition._PROBE_BYTES)
    except OSError:
        return None
    tag = partition.identify_filesystem(head)
    if not tag:
        return None
    return plan_for_fs(tag)
