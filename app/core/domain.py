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

"""Build ddrescue *domain* mapfiles from byte ranges.

A domain mapfile tells ddrescue (via ``-m``/``--domain-mapfile``) to restrict
the rescue to the blocks marked finished (``+``); everything else is left
non-tried (``?``). We use this to target exactly the regions we care about
(the ``$MFT``, then every directory ``$INDEX_ALLOCATION`` block, then later
individual files' ``$DATA``).

The ranges we feed in come from NTFS data runs and are arbitrary/overlapping,
so we align them outward to sector boundaries, sort, coalesce, and gap-fill
with ``?`` to produce a clean, ordered, gapless mapfile (no need for ``-L``).
"""

from __future__ import annotations

from app.core.mapfile import Block, FINISHED, Mapfile, NON_TRIED


def align_and_merge(
    ranges: list[tuple[int, int]],
    sector_size: int = 512,
    clamp_end: int | None = None,
) -> list[tuple[int, int]]:
    """Align ``(start, length)`` ranges to sectors, sort and coalesce.

    Returns a sorted list of non-overlapping ``(start, end)`` tuples. Adjacent
    and overlapping ranges are merged. Each range is expanded outward so it
    covers whole sectors.
    """
    aligned: list[tuple[int, int]] = []
    for start, length in ranges:
        if length <= 0:
            continue
        end = start + length
        a_start = (start // sector_size) * sector_size
        a_end = ((end + sector_size - 1) // sector_size) * sector_size
        if a_start < 0:
            a_start = 0
        if clamp_end is not None:
            a_end = min(a_end, clamp_end)
        if a_end > a_start:
            aligned.append((a_start, a_end))

    if not aligned:
        return []

    aligned.sort()
    merged = [aligned[0]]
    for start, end in aligned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # overlapping or adjacent
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def build_domain_mapfile(
    ranges: list[tuple[int, int]],
    total_size: int,
    sector_size: int = 512,
) -> Mapfile:
    """Create a gapless domain Mapfile marking ``ranges`` as finished.

    ``total_size`` is the size of the whole domain (e.g. the device/volume).
    Regions in ``ranges`` become ``+``; everything else becomes ``?``.
    """
    merged = align_and_merge(ranges, sector_size, clamp_end=total_size)

    blocks: list[Block] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            blocks.append(Block(cursor, start - cursor, NON_TRIED))
        blocks.append(Block(start, end - start, FINISHED))
        cursor = end
    if cursor < total_size:
        blocks.append(Block(cursor, total_size - cursor, NON_TRIED))
    if not blocks and total_size > 0:
        blocks.append(Block(0, total_size, NON_TRIED))

    return Mapfile(
        blocks=blocks,
        current_pos=0,
        current_status=NON_TRIED,
        current_pass=1,
        comments=["# Domain mapfile. Created by salvagemap"],
    )


def covered_bytes(mf: Mapfile) -> int:
    """Total bytes marked finished (the rescue domain size)."""
    return sum(b.size for b in mf.blocks if b.status == FINISHED)
