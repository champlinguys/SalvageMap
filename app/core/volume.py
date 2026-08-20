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


def locked_volume_message(image: str, volume_offset: int) -> str | None:
    """A "this volume is encrypted" message, or None if it's readable.

    Reads through :func:`read_image`, so an already-unlocked volume shows its
    plaintext filesystem here and this returns None — the check is "still
    locked?", not "was ever encrypted?".
    """
    from app.bitlocker.fve import looks_like_bitlocker
    from app.corestorage.cs import looks_like_corestorage
    try:
        head = read_image(image, volume_offset, 512)
    except OSError:
        return None
    if looks_like_bitlocker(head):
        return (
            f"The volume at offset 0x{volume_offset:X} is BitLocker-encrypted, so "
            "its filesystem can't be read yet.\n\nUnlock it first with its "
            "recovery key: Tools ▸ Unlock BitLocker volume…, then run this step "
            "again."
        )
    if looks_like_corestorage(head):
        return (
            f"The volume at offset 0x{volume_offset:X} is a CoreStorage volume "
            "encrypted with FileVault 2, so its filesystem can't be read yet."
            "\n\nUnlock it first with the Mac's password: Tools ▸ Unlock "
            "CoreStorage volume…, then run this step again."
        )
    return None


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
    if not tag or tag in partition.LOCKED:
        return None   # encrypted: no plan applies until it's unlocked
    return plan_for_fs(tag)
