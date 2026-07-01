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

"""Decode NTFS data runs (run lists).

A non-resident attribute stores its content as a *run list*: a sequence of
runs, each describing a number of contiguous clusters and where they live on
disk. Each run is encoded as:

    1 header byte:  low nibble  = number of bytes in the *length* field
                    high nibble = number of bytes in the *offset* field
    <length bytes>  cluster count (unsigned, little-endian)
    <offset bytes>  LCN delta from the previous run's start (SIGNED, LE)

A run with a zero offset field length is *sparse* (a hole) and has no on-disk
location. The list terminates at a zero header byte.

We decode runs into absolute byte ranges on the volume given the cluster size,
which is exactly what we need to build ddrescue domain mapfiles.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    """One run: ``length`` clusters starting at ``lcn`` (None if sparse)."""

    length: int        # cluster count
    lcn: int | None    # starting logical cluster number, or None for sparse


def _read_signed(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=True)


def _read_unsigned(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=False)


def decode_runlist(data: bytes) -> list[Run]:
    """Decode a run list into :class:`Run` objects.

    Stops at the terminating zero byte or the end of ``data``. Tolerant of a
    truncated buffer (returns what was decoded so far).
    """
    runs: list[Run] = []
    pos = 0
    prev_lcn = 0
    n = len(data)
    while pos < n:
        header = data[pos]
        pos += 1
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0:
            break  # malformed
        if pos + len_size + off_size > n:
            break  # truncated
        length = _read_unsigned(data[pos:pos + len_size])
        pos += len_size
        if off_size == 0:
            # Sparse run (hole) — no on-disk location.
            runs.append(Run(length, None))
            continue
        delta = _read_signed(data[pos:pos + off_size])
        pos += off_size
        prev_lcn += delta
        runs.append(Run(length, prev_lcn))
    return runs


def runs_to_byte_ranges(
    runs: list[Run],
    cluster_size: int,
    volume_offset: int = 0,
) -> list[tuple[int, int]]:
    """Convert runs to absolute ``(start, length)`` byte ranges.

    Sparse runs are skipped (no on-disk data). ``volume_offset`` is the byte
    offset of the NTFS volume within the whole disk (0 if operating on the
    volume directly).
    """
    ranges: list[tuple[int, int]] = []
    for run in runs:
        if run.lcn is None or run.length <= 0:
            continue
        start = volume_offset + run.lcn * cluster_size
        length = run.length * cluster_size
        ranges.append((start, length))
    return ranges


def runlist_length_clusters(runs: list[Run]) -> int:
    """Total clusters described by the runs (including sparse)."""
    return sum(r.length for r in runs)
