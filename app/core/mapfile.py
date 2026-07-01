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

"""Parse, write and aggregate GNU ddrescue mapfiles.

ddrescue mapfile layout (version >= 1.20)::

    # Mapfile. Created by GNU ddrescue version 1.30
    # Command line: ddrescue ...
    # ... more comment lines ...
    0x00000000     ?               1        <- status line: pos status pass
    0x00000000  0x00001000  +               <- block lines: pos size status
    0x00001000  0x000F0000  ?
    ...

Status characters (see the ddrescue manual):

    ?  non-tried        (not read yet)
    *  non-trimmed      (failed block, not yet trimmed)
    /  non-scraped      (failed block, trimmed but not scraped)
    -  bad-sector       (read error, retried)
    +  finished         (successfully read)

Block lines are ordered and gapless across the rescue domain, so a cell of the
sector map can be coloured by the *worst* status of the bytes it spans: a cell
is "finished" (green) only when every byte under it is finished.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Iterable

# All valid status characters, ordered best -> worst. The index doubles as the
# aggregation priority: when a sector-map cell spans bytes of several statuses,
# the worst (highest priority) one wins.
STATUS_CHARS = ("+", "?", "*", "/", "-")
_PRIORITY = {ch: i for i, ch in enumerate(STATUS_CHARS)}

NON_TRIED = "?"
FINISHED = "+"


def status_priority(status: str) -> int:
    """Worst-wins priority for a status char (higher == worse)."""
    return _PRIORITY.get(status, _PRIORITY[NON_TRIED])


@dataclass(frozen=True)
class Block:
    """A contiguous region of the domain with a single status."""

    pos: int
    size: int
    status: str

    @property
    def end(self) -> int:
        return self.pos + self.size


@dataclass
class Mapfile:
    """A parsed ddrescue mapfile."""

    blocks: list[Block] = field(default_factory=list)
    current_pos: int = 0
    current_status: str = NON_TRIED
    current_pass: int = 1
    comments: list[str] = field(default_factory=list)

    # --- derived geometry -------------------------------------------------
    @property
    def domain_start(self) -> int:
        return self.blocks[0].pos if self.blocks else 0

    @property
    def domain_end(self) -> int:
        return self.blocks[-1].end if self.blocks else 0

    @property
    def domain_size(self) -> int:
        return self.domain_end - self.domain_start

    def status_totals(self) -> dict[str, int]:
        """Bytes per status char across the whole domain."""
        totals = {ch: 0 for ch in STATUS_CHARS}
        for b in self.blocks:
            totals[b.status] = totals.get(b.status, 0) + b.size
        return totals

    def rescued_bytes(self) -> int:
        return self.status_totals().get(FINISHED, 0)


def _parse_int(token: str) -> int:
    """ddrescue writes hex (0x...) but accept decimal too."""
    return int(token, 0)


def parse_text(text: str) -> Mapfile:
    """Parse mapfile contents from a string.

    Tolerant of partially-written files (ddrescue rewrites the mapfile
    periodically while running): malformed trailing lines are ignored.
    """
    mf = Mapfile()
    seen_status_line = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            mf.comments.append(raw)
            continue
        parts = line.split()
        if not seen_status_line:
            # First non-comment line: current_pos current_status current_pass
            try:
                mf.current_pos = _parse_int(parts[0])
                mf.current_status = parts[1] if len(parts) > 1 else NON_TRIED
                mf.current_pass = int(parts[2]) if len(parts) > 2 else 1
            except (ValueError, IndexError):
                pass
            seen_status_line = True
            continue
        # Block line: pos size status
        if len(parts) < 3:
            continue
        try:
            pos = _parse_int(parts[0])
            size = _parse_int(parts[1])
        except ValueError:
            continue
        status = parts[2]
        if size <= 0:
            continue
        mf.blocks.append(Block(pos, size, status))
    return mf


def parse(path: str) -> Mapfile:
    """Parse a mapfile from disk."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_text(fh.read())


def to_text(mf: Mapfile, header_comment: str | None = None) -> str:
    """Serialise a Mapfile back to ddrescue mapfile text."""
    lines: list[str] = []
    if header_comment is not None:
        for cl in header_comment.splitlines():
            lines.append(cl if cl.startswith("#") else f"# {cl}")
    elif mf.comments:
        lines.extend(mf.comments)
    else:
        lines.append("# Mapfile. Created by salvagemap")
    lines.append("#      current_pos  current_status  current_pass")
    lines.append(f"0x{mf.current_pos:08X}     {mf.current_status}               {mf.current_pass}")
    lines.append("#      pos        size  status")
    for b in mf.blocks:
        lines.append(f"0x{b.pos:08X}  0x{b.size:08X}  {b.status}")
    return "\n".join(lines) + "\n"


def write(path: str, mf: Mapfile, header_comment: str | None = None) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_text(mf, header_comment))


# Statuses that mean "tried and could not be (fully) read" — the opposite of a
# non-tried hole. Used to tell unrecoverable file data apart from not-yet-imaged.
BAD_STATUSES = ("*", "/", "-")


def _merge_intervals(pairs: Iterable[tuple[int, int]]) -> tuple[list[int], list[int]]:
    merged: list[list[int]] = []
    for s, e in sorted(pairs):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [m[0] for m in merged], [m[1] for m in merged]


def _overlap_bytes(starts: list[int], ends: list[int], start: int, length: int) -> int:
    end = start + length
    if length <= 0 or not starts:
        return 0
    i = bisect.bisect_right(ends, start)  # first interval ending past start
    n = len(starts)
    total = 0
    while i < n and starts[i] < end:
        ov = min(end, ends[i]) - max(start, starts[i])
        if ov > 0:
            total += ov
        i += 1
    return total


class FinishedIndex:
    """Fast 'how much of these byte ranges is finished / bad?' over a mapfile.

    Built once per mapfile snapshot: FINISHED blocks (and, separately, the bad
    blocks — non-trimmed/non-scraped/bad-sector) are merged into sorted,
    non-overlapping intervals so a range query is a bisect plus a short walk.
    Used to colour file-tree nodes: finished = green, tried-but-bad = red,
    untouched = clear.
    """

    def __init__(self, starts, ends, bad_starts=None, bad_ends=None):
        self._starts = starts
        self._ends = ends
        self._bad_starts = bad_starts or []
        self._bad_ends = bad_ends or []

    @classmethod
    def from_mapfile(cls, mf: "Mapfile") -> "FinishedIndex":
        fin_s, fin_e = _merge_intervals(
            (b.pos, b.end) for b in mf.blocks if b.status == FINISHED)
        bad_s, bad_e = _merge_intervals(
            (b.pos, b.end) for b in mf.blocks if b.status in BAD_STATUSES)
        return cls(fin_s, fin_e, bad_s, bad_e)

    def finished_bytes(self, start: int, length: int) -> int:
        """Bytes of ``[start, start+length)`` that are finished."""
        return _overlap_bytes(self._starts, self._ends, start, length)

    def bad_bytes(self, start: int, length: int) -> int:
        """Bytes of ``[start, start+length)`` that were tried but are bad."""
        return _overlap_bytes(self._bad_starts, self._bad_ends, start, length)

    def fraction(self, ranges: Iterable[tuple[int, int]]) -> float | None:
        """Finished fraction across ``(start, length)`` ranges.

        Returns None when there are no on-disk bytes (e.g. resident content), so
        the caller can treat that as 'complete once the $MFT is captured'.
        """
        total = 0
        got = 0
        for start, length in ranges:
            if length <= 0:
                continue
            total += length
            got += self.finished_bytes(start, length)
        if total <= 0:
            return None
        return got / total


def range_finished_fraction(
    mf: "Mapfile", ranges: Iterable[tuple[int, int]]
) -> float | None:
    """Convenience one-shot wrapper around :class:`FinishedIndex`."""
    return FinishedIndex.from_mapfile(mf).fraction(ranges)


def aggregate(
    blocks: Iterable[Block],
    domain_start: int,
    domain_size: int,
    n_cells: int,
) -> list[str]:
    """Reduce blocks to ``n_cells`` status chars for the sector map.

    Each cell spans ``domain_size / n_cells`` bytes; its status is the worst
    status of any block byte it overlaps (worst-wins, see module docstring).
    Cells with no covering block default to non-tried (``?``).
    """
    if n_cells <= 0 or domain_size <= 0:
        return []

    cell_size = domain_size / n_cells
    # Track best-known (lowest) priority seen so far; None == uncovered.
    prio: list[int | None] = [None] * n_cells

    domain_end = domain_start + domain_size
    for b in blocks:
        b0 = max(b.pos, domain_start)
        b1 = min(b.end, domain_end)
        if b1 <= b0:
            continue
        # Cell c spans byte-range [c*cell_size, (c+1)*cell_size); it overlaps the
        # block [b0, b1) iff that interval intersects. This holds for both down-
        # sampling (cell_size > 1) and upsampling (cell_size < 1).
        start_cell = int((b0 - domain_start) / cell_size)
        end_cell = math.ceil((b1 - domain_start) / cell_size) - 1
        start_cell = max(0, min(start_cell, n_cells - 1))
        end_cell = max(0, min(end_cell, n_cells - 1))
        p = status_priority(b.status)
        for c in range(start_cell, end_cell + 1):
            cur = prio[c]
            prio[c] = p if cur is None else max(cur, p)

    default = status_priority(NON_TRIED)
    return [STATUS_CHARS[p if p is not None else default] for p in prio]


def aggregate_progress(
    blocks: Iterable[Block],
    domain_start: int,
    domain_size: int,
    n_cells: int,
) -> list[tuple[str, float]]:
    """Like :func:`aggregate`, but also reports each cell's finished fraction.

    Returns ``(display_status, finished_fraction)`` per cell:
      * if any byte in the cell has a *bad* status, the worst bad char (so a
        single bad sector stays visible) with fraction 1.0;
      * otherwise ``+`` with the fraction of the cell that is finished (lets the
        UI render partial progress as a dim green, which matters on huge disks
        where a fully-finished cell is rare), or ``?`` when nothing is finished.

    Non-tried blocks are skipped (they contribute nothing), which also keeps this
    cheap when most of the domain is a single huge non-tried region.
    """
    if n_cells <= 0 or domain_size <= 0:
        return []

    cell_size = domain_size / n_cells
    finished = [0.0] * n_cells
    worst_bad: list[int | None] = [None] * n_cells
    bad_floor = status_priority("*")  # statuses >= this are "bad"
    domain_end = domain_start + domain_size

    for b in blocks:
        if b.status == NON_TRIED:
            continue
        p = status_priority(b.status)
        is_bad = p >= bad_floor
        if b.status != FINISHED and not is_bad:
            continue
        b0 = max(b.pos, domain_start)
        b1 = min(b.end, domain_end)
        if b1 <= b0:
            continue
        start_cell = int((b0 - domain_start) / cell_size)
        end_cell = math.ceil((b1 - domain_start) / cell_size) - 1
        start_cell = max(0, min(start_cell, n_cells - 1))
        end_cell = max(0, min(end_cell, n_cells - 1))
        for c in range(start_cell, end_cell + 1):
            cell_lo = domain_start + c * cell_size
            cell_hi = cell_lo + cell_size
            overlap = min(b1, cell_hi) - max(b0, cell_lo)
            if overlap <= 0:
                continue
            if is_bad:
                cur = worst_bad[c]
                worst_bad[c] = p if cur is None else max(cur, p)
            else:  # finished
                finished[c] += overlap

    out: list[tuple[str, float]] = []
    for c in range(n_cells):
        if worst_bad[c] is not None:
            out.append((STATUS_CHARS[worst_bad[c]], 1.0))
        else:
            frac = min(1.0, finished[c] / cell_size) if cell_size else 0.0
            out.append((FINISHED, frac) if frac > 0 else (NON_TRIED, 0.0))
    return out
