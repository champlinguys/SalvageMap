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

"""Where the logical volume's bytes physically live, measured rather than assumed.

This is the piece that lets targeted recovery work on a CoreStorage volume, and
it exists because of a real hazard. The filesystem's ranges are *logical* — HFS+
extents inside the logical volume — but ddrescue has to be pointed at the
*physical* sectors holding their ciphertext. CoreStorage allocates a logical
volume out of segments of the physical volume, and libfvde does not expose that
segment map. Guessing a single fixed shift would work on a freshly-encrypted
disk and silently image the wrong sectors on a resized one: the recovery would
appear to succeed and quietly hand back the wrong bytes. That failure is
invisible, which is what makes it unacceptable.

So the mapping is not derived from the format at all — it is *observed*. libfvde
is asked to read a logical offset while the file object underneath records which
physical offsets it touched (see :class:`app.corestorage.keys.ImageSlice`). That
gives the true shift at that point, whatever CoreStorage did. Probing evenly
across the volume and bisecting wherever two neighbours disagree recovers the
segment boundaries exactly, in a few hundred tiny reads.

If the mapping cannot be established, :attr:`Mapping.trusted` is False and the
caller must fall back to imaging the whole partition. Imaging too much is slow;
imaging the wrong place is a lost recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Evenly-spaced probes taken before any bisection. Enough to catch fragmentation
# on a normal volume without making unlock feel slow; boundaries found between
# neighbours are then pinned exactly, so this is a sampling density, not a limit
# on accuracy.
DEFAULT_PROBES = 256

# Bisection stops here. CoreStorage allocates in whole blocks far larger than
# this, so a boundary pinned to a sector is exact in practice.
BISECT_GRANULARITY = 512

Range = tuple[int, int]


@dataclass(frozen=True)
class Segment:
    """A run of the logical volume that maps to one run of the physical volume."""
    logical_start: int
    physical_start: int          # partition-relative
    size: int

    @property
    def logical_end(self) -> int:
        return self.logical_start + self.size

    @property
    def shift(self) -> int:
        return self.physical_start - self.logical_start


@dataclass
class Mapping:
    """Logical-volume → physical-disk mapping for one CoreStorage volume."""
    volume_offset: int                       # CS partition start, on disk
    logical_size: int
    segments: list[Segment] = field(default_factory=list)
    trusted: bool = False                    # False => do not target; image whole
    readable_limit: int = 0                  # how far libfvde can decrypt
    evidence: str = ""                       # why the mapping is (not) trusted

    @property
    def is_contiguous(self) -> bool:
        return len(self.segments) == 1

    @property
    def summary(self) -> str:
        if not self.trusted:
            return ("mapping not established — the whole partition will be "
                    f"imaged ({self.evidence})")
        if self.is_contiguous:
            shift = self.segments[0].shift
            return (f"one contiguous segment, shifted {shift:,} bytes "
                    f"({_mib(shift)}) — {self.evidence}")
        return f"{len(self.segments)} segments — {self.evidence}"

    def _segment_for(self, logical: int) -> Segment | None:
        for seg in self.segments:
            if seg.logical_start <= logical < seg.logical_end:
                return seg
        return None

    def physical_ranges(self, ranges: list[Range]) -> list[Range]:
        """Translate logical ``(start, length)`` ranges to absolute disk ranges.

        A range is split wherever it crosses a segment boundary. When the
        mapping is untrusted the whole partition is returned instead, so the
        caller images a superset rather than the wrong place.
        """
        if not self.trusted or not self.segments:
            return [(self.volume_offset, self.logical_size)]
        out: list[Range] = []
        for start, length in ranges:
            pos, end = start, start + length
            while pos < end:
                seg = self._segment_for(pos)
                if seg is None:      # outside every segment: image it as-is
                    out.append((self.volume_offset + pos, end - pos))
                    break
                take = min(end, seg.logical_end) - pos
                out.append((self.volume_offset + seg.physical_start
                            + (pos - seg.logical_start), take))
                pos += take
        return out


def _mib(n: int) -> str:
    return f"{n / (1 << 20):,.0f} MiB"


def _shift_at(unlocked, logical: int) -> int | None:
    """The physical shift libfvde uses at ``logical``, or None if unreadable.

    Serving one small read can touch metadata as well as data, so the recorded
    reads are filtered to the one that plausibly *is* the data read: it must
    cover the requested offset once the shift is applied, and the shift must
    keep the read inside the partition.
    """
    reads = unlocked.observe_physical(logical, BISECT_GRANULARITY)
    for physical_start, length in reversed(reads):
        if length <= 0:
            continue
        shift = physical_start - (logical - logical % BISECT_GRANULARITY)
        if shift < 0:
            continue
        if physical_start + length > unlocked.physical_size:
            continue
        return shift
    return None


def _find_boundary(unlocked, low: int, low_shift: int, high: int) -> int:
    """First logical offset > ``low`` whose shift differs from ``low_shift``.

    ``low`` and ``high`` are known to disagree, so bisection converges on the
    segment edge between them.
    """
    while high - low > BISECT_GRANULARITY:
        mid = (low + high) // 2
        mid -= mid % BISECT_GRANULARITY
        if mid <= low:
            break
        shift = _shift_at(unlocked, mid)
        if shift == low_shift:
            low = mid
        else:
            high = mid
    return high


def _first_metadata_after(header, physical_size: int, shift: int) -> int:
    """Start of the first container-metadata region at or after ``shift``.

    The logical volume has to live in the gap between CoreStorage's front
    metadata and its end-of-disk copies, so this is the hard ceiling on how far
    a single contiguous segment could possibly run.
    """
    if header is None:
        return physical_size
    block = header.metadata_block_size or header.block_size or 4096
    span = (header.metadata_size or 0) + (4 << 20)
    limits = [physical_size]
    for number in header.metadata_blocks:
        start = number * block
        if start + span > shift:
            limits.append(start)
    return min(limits)


def measure(unlocked, probes: int = DEFAULT_PROBES, header=None) -> Mapping:
    """Measure the mapping of an unlocked volume by watching libfvde read.

    Probing is confined to what libfvde can actually decrypt — see
    :meth:`app.corestorage.keys.UnlockedVolume.readable_limit`. On a volume over
    1 TiB that is a small fraction of it, which is why a uniform shift across the
    readable part is then *proved* to extend over the rest rather than assumed:
    the logical volume must fit between CoreStorage's front metadata and its
    end-of-disk copies, and on a full-disk volume that gap is barely larger than
    the volume itself, leaving no room for a second segment. The proof is
    recorded in :attr:`Mapping.evidence`, and when it does not hold the mapping
    stays untrusted rather than extrapolating.

    Returns an untrusted :class:`Mapping` rather than raising if the volume
    cannot be probed — a recovery that images too much still recovers the data.
    """
    size = unlocked.size
    mapping = Mapping(volume_offset=unlocked.volume_offset, logical_size=size)
    if size <= 0:
        return mapping

    ceiling = unlocked.readable_limit()
    mapping.readable_limit = ceiling
    if ceiling <= 0:
        mapping.evidence = "libfvde could not decrypt anything in this volume"
        return mapping

    step = max(ceiling // max(probes, 1), BISECT_GRANULARITY)
    points = list(range(0, ceiling, step))
    last = (ceiling - BISECT_GRANULARITY) & ~(BISECT_GRANULARITY - 1)
    if points[-1] != last:
        points.append(last)

    samples: list[tuple[int, int]] = []
    for point in points:
        shift = _shift_at(unlocked, point)
        if shift is None:
            mapping.evidence = (
                f"no physical read could be observed at logical offset "
                f"{point:,}")
            return mapping
        samples.append((point, shift))

    segments: list[Segment] = []
    seg_start, seg_shift = samples[0]
    for (prev_point, prev_shift), (point, shift) in zip(samples, samples[1:]):
        if shift == seg_shift:
            continue
        boundary = _find_boundary(unlocked, prev_point, prev_shift, point)
        segments.append(Segment(seg_start, seg_start + seg_shift,
                                boundary - seg_start))
        seg_start, seg_shift = boundary, shift

    if ceiling >= size:
        # The whole volume was probed directly; nothing needs proving.
        segments.append(Segment(seg_start, seg_start + seg_shift, size - seg_start))
        mapping.segments = segments
        mapping.trusted = True
        mapping.evidence = "whole volume probed directly"
        return mapping

    if segments:
        # Fragmented *and* only partly readable: the tail cannot be established,
        # and guessing which segment it belongs to is exactly the mistake this
        # module exists to avoid.
        mapping.segments = segments
        mapping.evidence = (
            f"{len(segments) + 1} segments found below libfvde's "
            f"{ceiling:,}-byte ceiling, so the rest cannot be resolved")
        return mapping

    # One uniform shift across everything readable. It extends over the whole
    # volume only if a single contiguous segment is the only thing that fits.
    limit = _first_metadata_after(header, unlocked.physical_size, seg_shift)
    spare = limit - seg_shift - size
    if spare < 0:
        mapping.evidence = (
            f"a contiguous volume at shift {seg_shift:,} would overrun the "
            f"container metadata at {limit:,}")
        return mapping

    mapping.segments = [Segment(0, seg_shift, size)]
    mapping.trusted = True
    mapping.evidence = (
        f"uniform shift over {len(samples)} probes up to {ceiling / (1 << 40):.2f} "
        f"TiB; the volume fits the {limit - seg_shift:,}-byte gap between the "
        f"container's front and end-of-disk metadata with {spare:,} bytes to "
        f"spare, leaving no room for a second segment")
    return mapping
