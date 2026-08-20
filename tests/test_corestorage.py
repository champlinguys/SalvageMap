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

"""CoreStorage header parsing, volume detection and the physical mapping.

The header fixture is built to the same layout as a real FileVault 2 volume —
the field offsets were read off a 14 TB Mac disk and cross-checked against its
GPT entry — so these tests pin the parser to the format rather than to itself.

The mapping tests use a fake unlocked volume that reports a shift chosen by the
test, which is exactly the shape :mod:`app.corestorage.segments` observes from
libfvde. That lets the segment-boundary logic — the part that decides which
sectors ddrescue is pointed at — be tested without libfvde or a real disk.
"""

import struct
import uuid

import pytest

from app.core import partition
from app.corestorage import cs, detect, segments
from app.corestorage.segments import Mapping, Segment

# Synthetic, not from any real disk: the parser only cares about byte order at
# offset 0x130, and a fixture must never carry a customer volume's identity.
VOLUME_UUID = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
PARTITION_SIZE = 14_000_142_114_816


def make_header(encryption_method: int = cs.ENCRYPTION_AES_128_XTS,
                block_type: int = cs.VOLUME_HEADER_BLOCK_TYPE,
                signature: bytes = cs.SIGNATURE,
                size: int = PARTITION_SIZE,
                metadata_blocks: tuple = (1025, 3418001397, 3418002421)) -> bytes:
    """A CoreStorage volume header laid out like the real one."""
    head = bytearray(cs.HEADER_SIZE)
    struct.pack_into("<I", head, 0x00, 0x4E1AD758)        # checksum
    struct.pack_into("<I", head, 0x04, 0xFFFFFFFF)
    struct.pack_into("<H", head, 0x08, 1)                 # version
    struct.pack_into("<H", head, 0x0A, block_type)
    struct.pack_into("<Q", head, 0x30, 512)               # block size
    struct.pack_into("<Q", head, 0x40, size)
    head[0x58:0x58 + len(signature)] = signature
    struct.pack_into("<H", head, 0x5A, 1)                 # checksum algorithm
    struct.pack_into("<I", head, 0x60, 4096)              # metadata block size
    struct.pack_into("<I", head, 0x64, 4 << 20)           # metadata size
    for i, number in enumerate(metadata_blocks[:cs._METADATA_BLOCK_SLOTS]):
        struct.pack_into("<Q", head, 0x70 + i * 8, number)
    struct.pack_into("<I", head, 0xA8, 16)                # key data size
    struct.pack_into("<I", head, 0xAC, encryption_method)
    head[0x130:0x140] = VOLUME_UUID.bytes
    return bytes(head)


# --- header parsing -------------------------------------------------------
def test_parses_the_real_field_layout():
    header = cs.parse(make_header())
    assert header is not None
    assert header.version == 1
    assert header.block_size == 512
    # The size field matched the GPT entry byte-for-byte on the reference drive.
    assert header.physical_volume_size == PARTITION_SIZE
    assert header.identifier == str(VOLUME_UUID)
    assert header.metadata_blocks == (1025, 3418001397, 3418002421)
    assert header.is_encrypted
    assert header.method_name == "AES-128-XTS"


def test_unencrypted_corestorage_is_recognised_but_not_encrypted():
    header = cs.parse(make_header(encryption_method=cs.ENCRYPTION_NONE))
    assert header is not None and not header.is_encrypted


def test_signature_alone_is_not_enough():
    """A stray "CS" without the volume-header block type must not pass."""
    assert not cs.looks_like_corestorage(make_header(block_type=0x0011))
    assert not cs.looks_like_corestorage(make_header(signature=b"XX"))
    assert cs.parse(b"\x00" * cs.HEADER_SIZE) is None


def test_truncated_header_is_rejected():
    assert not cs.looks_like_corestorage(make_header()[:256])


# --- partition scanner integration ---------------------------------------
def test_partition_scanner_labels_it_locked():
    fs = partition.identify_filesystem(make_header())
    assert fs == "corestorage"
    assert fs in partition.LOCKED
    assert fs not in partition.RECOVERABLE
    assert partition.FS_LABELS[fs] == "FileVault 2 (locked)"


# --- detection in an image ------------------------------------------------
def _image(tmp_path, offset: int):
    path = tmp_path / "disk.img"
    data = bytearray(offset + cs.HEADER_SIZE)
    data[offset:offset + cs.HEADER_SIZE] = make_header()
    path.write_bytes(bytes(data))
    return str(path)


def test_finds_volume_by_signature_when_table_is_unreadable(tmp_path):
    """Sector 0 is often exactly what a failing drive didn't give back."""
    found = detect.find_volumes(_image(tmp_path, 1 << 20))
    assert [v.offset for v in found] == [1 << 20]
    assert found[0].source == "signature scan"
    assert found[0].identifier == str(VOLUME_UUID)


def test_unencrypted_containers_are_not_offered_for_unlocking(tmp_path):
    path = tmp_path / "plain.img"
    data = bytearray((1 << 20) + cs.HEADER_SIZE)
    data[1 << 20:] = make_header(encryption_method=cs.ENCRYPTION_NONE)
    path.write_bytes(bytes(data))
    assert detect.find_volumes(str(path)) == []
    assert len(detect.find_volumes(str(path), encrypted_only=False)) == 1


# --- the physical mapping -------------------------------------------------
BASE = 1 << 20          # CS partition start on disk
SHIFT = 64 << 20        # metadata ahead of the logical volume


def test_contiguous_mapping_shifts_ranges():
    m = Mapping(BASE, 1 << 30, [Segment(0, SHIFT, 1 << 30)], trusted=True)
    assert m.is_contiguous
    assert m.physical_ranges([(0, 4096)]) == [(BASE + SHIFT, 4096)]
    assert m.physical_ranges([(8192, 4096)]) == [(BASE + SHIFT + 8192, 4096)]


def test_range_crossing_a_segment_boundary_is_split():
    m = Mapping(BASE, 800, [Segment(0, 64, 400), Segment(400, 900, 400)],
                trusted=True)
    assert m.physical_ranges([(350, 100)]) == [(BASE + 414, 50), (BASE + 900, 50)]


def test_untrusted_mapping_images_the_whole_partition():
    """Imaging a superset is slow; imaging the wrong place loses the recovery."""
    m = Mapping(BASE, 1 << 30)
    assert not m.trusted
    assert m.physical_ranges([(0, 4096)]) == [(BASE, 1 << 30)]


class FakeUnlocked:
    """Stands in for libfvde: reports a shift decided by ``shift_for``."""

    def __init__(self, size, shift_for, physical_size=None, ceiling=None):
        self.size = size
        self.volume_offset = BASE
        self.physical_size = physical_size or (size + (1 << 30))
        self._shift_for = shift_for
        self._ceiling = size if ceiling is None else ceiling
        self.probes = 0

    def readable_limit(self):
        return self._ceiling

    def observe_physical(self, offset, length=512):
        self.probes += 1
        shift = self._shift_for(offset)
        if shift is None:
            return []
        aligned = offset - offset % segments.BISECT_GRANULARITY
        return [(aligned + shift, length)]


def test_measure_finds_a_single_contiguous_segment():
    fake = FakeUnlocked(1 << 30, lambda off: SHIFT)
    m = segments.measure(fake, probes=16)
    assert m.trusted and m.is_contiguous
    assert m.segments[0].shift == SHIFT
    assert m.physical_ranges([(0, 512)]) == [(BASE + SHIFT, 512)]


def test_measure_pins_a_fragmented_boundary_exactly():
    """Two segments: the boundary must be found, not approximated."""
    size = 1 << 20
    boundary = 512 * 700          # not on a probe point
    fake = FakeUnlocked(size, lambda off: SHIFT if off < boundary else SHIFT * 2)
    m = segments.measure(fake, probes=8)
    assert m.trusted and len(m.segments) == 2
    assert m.segments[0].size == boundary
    assert m.segments[1].logical_start == boundary
    assert m.segments[1].shift == SHIFT * 2
    # A read either side lands in the right place.
    assert m.physical_ranges([(0, 512)]) == [(BASE + SHIFT, 512)]
    assert m.physical_ranges([(boundary, 512)]) == [
        (BASE + boundary + SHIFT * 2, 512)]


def test_measure_gives_up_rather_than_guess_when_a_probe_fails():
    fake = FakeUnlocked(1 << 20, lambda off: None)
    m = segments.measure(fake, probes=8)
    assert not m.trusted
    assert m.physical_ranges([(0, 512)]) == [(BASE, 1 << 20)]


def test_measure_rejects_a_shift_that_runs_past_the_partition():
    fake = FakeUnlocked(1 << 20, lambda off: 1 << 40, physical_size=1 << 20)
    assert not segments.measure(fake, probes=4).trusted


# --- the decrypt-on-read source ------------------------------------------
class FakeVolume:
    """An unlocked logical volume whose plaintext is a recognisable pattern."""

    def __init__(self, size):
        self.size = size
        self.volume_offset = BASE
        self.name = "TEST VOLUME"
        self.closed = False

    def read(self, offset, length):
        return bytes(((offset + i) % 251 for i in range(length)))

    def close(self):
        self.closed = True


def _source(volume_size=1 << 20, trusted=True):
    from app.corestorage.source import CoreStorageSource
    volume = FakeVolume(volume_size)
    mapping = Mapping(BASE, volume_size,
                      [Segment(0, SHIFT, volume_size)] if trusted else [],
                      trusted=trusted)
    return CoreStorageSource(volume, mapping), volume


def test_reads_inside_the_volume_come_back_as_plaintext():
    src, volume = _source()
    raw = lambda off, n: b"\xAA" * n      # noqa: E731 — the ciphertext, untouched
    assert src.read(raw, BASE, 16) == volume.read(0, 16)
    assert src.read(raw, BASE + 4096, 16) == volume.read(4096, 16)


def test_reads_outside_the_volume_fall_through_untouched():
    """The partition table and the EFI/Booter partitions must read normally."""
    src, _ = _source()
    raw = lambda off, n: b"\xAA" * n      # noqa: E731
    assert src.read(raw, 0, 512) == b"\xAA" * 512
    assert src.read(raw, src.volume_end, 512) == b"\xAA" * 512


def test_a_read_straddling_the_volume_start_is_stitched():
    src, volume = _source()
    raw = lambda off, n: b"\xAA" * n      # noqa: E731
    got = src.read(raw, BASE - 8, 24)
    assert got == b"\xAA" * 8 + volume.read(0, 16)


def test_a_short_raw_read_is_zero_filled():
    """A hole in a partial rescue must not shorten the buffer the parser gets."""
    src, _ = _source()
    assert src.read(lambda off, n: b"", 0, 64) == b"\x00" * 64


def test_physical_ranges_go_through_the_measured_map():
    src, _ = _source()
    assert src.physical_ranges([(BASE, 4096)]) == [(BASE + SHIFT, 4096)]


def test_physical_ranges_outside_the_volume_pass_through():
    src, _ = _source()
    assert src.physical_ranges([(0, 512)]) == [(0, 512)]


def test_untrusted_mapping_makes_the_source_image_the_whole_partition():
    src, _ = _source(trusted=False)
    assert src.physical_ranges([(BASE, 4096)]) == [(BASE, 1 << 20)]


def test_closing_the_source_releases_the_volume():
    src, volume = _source()
    src.close()
    assert volume.closed


# --- the targeted workflow, end to end -----------------------------------
# The parts above test pieces; these two drive the real state machine, because
# the way CoreStorage breaks the targeted workflow is a *sequencing* bug, not a
# parsing one, and only the whole flow shows it.

WORKFLOW_OFFSET = 1 << 20           # CS partition start on the synthetic disk
TAIL_BLOCK = 100_000                # stands in for the real drive's end-of-disk
TAIL_BLOCK_2 = 120_000              # metadata copies, scaled down to fit a test
WORKFLOW_SHIFT = 64 << 20           # logical volume sits behind the metadata


class _FakeRunner:
    """Records the domain mapfile each phase asks ddrescue to image."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Runner(QObject):
            finished = Signal(int)

            def __init__(self, outer):
                super().__init__()
                self._outer = outer

            def start(self, infile, outfile, logfile, settings):
                self._outer.domains.append(settings.domain_mapfile)
                self.finished.emit(0)

            def take_unaligned_error(self):
                return False

        self.domains = []
        self.qt = _Runner(self)

    def targeted(self):
        """Every ``(pos, size)`` block ddrescue was pointed at, all phases."""
        from app.core import mapfile as mapfile_mod
        out = []
        for path in self.domains:
            out += [(b.pos, b.size) for b in mapfile_mod.parse(path).blocks
                    if b.status == "+"]
        return out


def _context(tmp_path, src_size, volume_offset=WORKFLOW_OFFSET):
    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import RecoveryContext

    src = tmp_path / "src.img"
    with open(src, "wb") as fh:
        fh.truncate(src_size)
    return RecoveryContext(
        infile=str(src), outfile=str(tmp_path / "out.img"),
        logfile=str(tmp_path / "out.log"), workdir=str(tmp_path),
        settings=RescueSettings(sector_size=512), volume_offset=volume_offset,
    )


def test_workflow_images_the_metadata_needed_to_unlock(tmp_path):
    """The gap this closes: partition detection images 64 KiB per partition, but
    unlocking needs the container metadata — including copies at the far end of
    the disk. Without this phase the run dead-ends telling the tech to unlock a
    volume that cannot be unlocked."""
    from app.core.recovery import Phase, TargetedRecovery

    ctx = _context(tmp_path, 512 << 20)
    header = make_header(size=512 << 20,
                         metadata_blocks=(1025, TAIL_BLOCK, TAIL_BLOCK_2))
    # Partition detection has already imaged the volume's first sectors.
    with open(ctx.outfile, "wb") as fh:
        fh.truncate(512 << 20)
        fh.seek(WORKFLOW_OFFSET)
        fh.write(header)

    runner = _FakeRunner()
    rec = TargetedRecovery(runner.qt)
    results, phases = [], []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    rec.phaseStep.connect(phases.append)
    rec.start(ctx)

    assert Phase.GET_CSMETA in phases
    assert results and results[0][0] is True
    # With no unlock handler wired the run still stops and explains itself.
    assert "Unlock the volume" in results[0][1]

    targeted = runner.targeted()
    def covered(start, length):
        return any(p <= start and start + length <= p + n for p, n in targeted)

    # The front metadata, and both end-of-disk copies the header points at.
    assert covered(WORKFLOW_OFFSET, cs.FRONT_METADATA_BYTES)
    assert covered(WORKFLOW_OFFSET + TAIL_BLOCK * 4096, 4 << 20)
    assert covered(WORKFLOW_OFFSET + TAIL_BLOCK_2 * 4096, 4 << 20)


def test_locked_bitlocker_volume_still_dead_ends(tmp_path):
    """Only CoreStorage diverts: a BitLocker volume's metadata is already in the
    image, so the tech genuinely just needs the recovery key."""
    from app.core.recovery import Phase, TargetedRecovery

    ctx = _context(tmp_path, 8 << 20)
    from tests.test_bitlocker import build_boot_record
    with open(ctx.outfile, "wb") as fh:
        fh.truncate(8 << 20)
        fh.seek(WORKFLOW_OFFSET)
        fh.write(build_boot_record())

    runner = _FakeRunner()
    rec = TargetedRecovery(runner.qt)
    results, phases = [], []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    rec.phaseStep.connect(phases.append)
    rec.start(ctx)

    assert Phase.GET_CSMETA not in phases
    assert results and results[0][0] is False
    assert "BitLocker" in results[0][1]


def test_unlocked_volume_runs_the_hfsplus_plan_against_physical_sectors(tmp_path):
    """The whole point: once unlocked, the HFS+ plan parses the *plaintext*
    volume but ddrescue must be pointed at the shifted *physical* sectors."""
    from app.core import decrypt
    from app.core.recovery import Phase, TargetedRecovery
    from app.corestorage.source import CoreStorageSource
    from tests.test_hfsplus import _synth_image

    synth = tmp_path / "hfs.img"
    _synth_image(str(synth))
    plaintext = synth.read_bytes()

    class Unlocked:
        size = len(plaintext)
        volume_offset = WORKFLOW_OFFSET
        name = "TEST VOLUME"

        def read(self, offset, length):
            chunk = plaintext[offset:offset + length]
            return chunk + b"\x00" * (length - len(chunk))

        def close(self):
            pass

    mapping = Mapping(WORKFLOW_OFFSET, len(plaintext),
                      [Segment(0, WORKFLOW_SHIFT, len(plaintext))], trusted=True)
    ctx = _context(tmp_path, WORKFLOW_OFFSET + WORKFLOW_SHIFT + len(plaintext))
    open(ctx.outfile, "wb").close()

    decrypt.register(ctx.outfile, CoreStorageSource(Unlocked(), mapping))
    try:
        runner = _FakeRunner()
        rec = TargetedRecovery(runner.qt)
        results, plans, phases = [], [], []
        rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
        rec.planSelected.connect(plans.append)
        rec.phaseStep.connect(phases.append)
        rec.start(ctx, include_filedata=True)
    finally:
        decrypt.unregister(ctx.outfile)

    # The unlocked volume reads as HFS+, so the HFS+ plan is chosen and runs.
    assert results and results[0][0] is True, results
    assert plans and (Phase.GET_CATALOG, "Catalog B-tree") in plans[0]
    assert Phase.GET_VOLHEADER in phases and Phase.GET_CATALOG in phases
    assert Phase.GET_FILEDATA in phases
    # Not the unlock phase: nothing is locked any more.
    assert Phase.GET_CSMETA not in phases

    targeted = runner.targeted()
    assert targeted
    base = WORKFLOW_OFFSET + WORKFLOW_SHIFT
    # Every sector ddrescue was pointed at is in the shifted physical region —
    # if the mapping were skipped these would sit WORKFLOW_SHIFT bytes too low.
    assert all(base <= pos < base + len(plaintext) for pos, _n in targeted), targeted
    # The volume header specifically: HFS+ reads it at logical +1024.
    assert any(pos <= base + 1024 and base + 1024 + 512 <= pos + n
               for pos, n in targeted)


def test_image_this_folder_first_targets_physical_sectors(tmp_path):
    """The tree-driven path (run_ranges) shares the mapping: a folder picked out
    of the browsable tree must image the ciphertext that backs it."""
    from app.core import decrypt
    from app.core.recovery import TargetedRecovery
    from app.corestorage.source import CoreStorageSource

    size = 1 << 20

    class Unlocked:
        volume_offset = WORKFLOW_OFFSET
        name = "TEST VOLUME"

        def __init__(self):
            self.size = size

        def read(self, offset, length):
            return b"\x00" * length

        def close(self):
            pass

    mapping = Mapping(WORKFLOW_OFFSET, size,
                      [Segment(0, WORKFLOW_SHIFT, size)], trusted=True)
    ctx = _context(tmp_path, WORKFLOW_OFFSET + WORKFLOW_SHIFT + size)
    open(ctx.outfile, "wb").close()

    decrypt.register(ctx.outfile, CoreStorageSource(Unlocked(), mapping))
    try:
        runner = _FakeRunner()
        rec = TargetedRecovery(runner.qt)
        # A file at the very start of the volume, as the file tree reports it.
        rec.run_ranges(ctx, [(WORKFLOW_OFFSET, 4096)], "Imaged 'Documents'.")
    finally:
        decrypt.unregister(ctx.outfile)

    assert runner.targeted() == [(WORKFLOW_OFFSET + WORKFLOW_SHIFT, 4096)]


# --- libfvde's 1 TiB ceiling ---------------------------------------------
# A volume over 2^31 sectors cannot be read past 1 TiB by libfvde at all. That
# is what made the first version of measure() report "unmeasurable" on a real
# 12.7 TiB drive: 92% of its probes landed above the ceiling and observed
# nothing. Probing must stay below it, and the rest has to be *proved*.
CEILING = 1 << 40


def _header_for(volume_size, tail_block):
    return cs.parse(make_header(size=volume_size + (256 << 20),
                                metadata_blocks=(1025, tail_block)))


def test_uniform_shift_below_the_ceiling_extends_when_nothing_else_fits():
    """The real drive: probes stop at 1 TiB, but the volume can only be
    contiguous, because there is nowhere else for it to go."""
    lv = 12 * (1 << 40)
    header = _header_for(lv, tail_block=(SHIFT + lv + (64 << 20)) // 4096)
    fake = FakeUnlocked(lv, lambda off: SHIFT,
                        physical_size=SHIFT + lv + (256 << 20), ceiling=CEILING)
    m = segments.measure(fake, probes=32, header=header)
    assert m.trusted and m.is_contiguous
    assert m.segments[0].size == lv          # extends over the whole volume
    assert "no room for a second segment" in m.evidence
    assert m.readable_limit == CEILING
    # A range far above the ceiling still maps.
    assert m.physical_ranges([(11 << 40, 4096)]) == [(BASE + SHIFT + (11 << 40), 4096)]


def test_shift_is_not_extended_when_the_volume_would_overrun_metadata():
    """If a contiguous volume wouldn't fit, the evidence fails and so does trust."""
    lv = 12 * (1 << 40)
    header = _header_for(lv, tail_block=(SHIFT + (1 << 40)) // 4096)
    fake = FakeUnlocked(lv, lambda off: SHIFT,
                        physical_size=SHIFT + lv + (256 << 20), ceiling=CEILING)
    m = segments.measure(fake, probes=8, header=header)
    assert not m.trusted
    assert "overrun the container metadata" in m.evidence


def test_fragmentation_below_the_ceiling_is_never_extrapolated():
    """Two segments in the readable part means the unreadable tail is unknowable."""
    lv = 12 * (1 << 40)
    boundary = 512 << 30
    header = _header_for(lv, tail_block=(SHIFT + lv + (64 << 20)) // 4096)
    fake = FakeUnlocked(lv, lambda off: SHIFT if off < boundary else SHIFT * 2,
                        physical_size=SHIFT + lv + (256 << 20), ceiling=CEILING)
    m = segments.measure(fake, probes=16, header=header)
    assert not m.trusted
    assert "cannot be resolved" in m.evidence


def test_a_volume_under_the_ceiling_is_probed_directly():
    fake = FakeUnlocked(1 << 30, lambda off: SHIFT)
    m = segments.measure(fake, probes=8)
    assert m.trusted and m.evidence == "whole volume probed directly"


def test_run_unlocks_and_continues_into_the_hfsplus_plan(tmp_path):
    """The automated path: imaging the container metadata, unlocking and the
    whole HFS+ workflow are one run, not three things the tech sequences."""
    from app.core import decrypt
    from app.core.recovery import Phase, TargetedRecovery
    from app.corestorage.source import CoreStorageSource
    from tests.test_hfsplus import _synth_image

    synth = tmp_path / "hfs.img"
    _synth_image(str(synth))
    plaintext = synth.read_bytes()

    class Unlocked:
        size = len(plaintext)
        volume_offset = WORKFLOW_OFFSET
        name = "TEST VOLUME"

        def read(self, offset, length):
            chunk = plaintext[offset:offset + length]
            return chunk + b"\x00" * (length - len(chunk))

        def close(self):
            pass

    ctx = _context(tmp_path, 512 << 20)
    header = make_header(size=512 << 20,
                         metadata_blocks=(1025, TAIL_BLOCK, TAIL_BLOCK_2))
    with open(ctx.outfile, "wb") as fh:
        fh.truncate(512 << 20)
        fh.seek(WORKFLOW_OFFSET)
        fh.write(header)

    runner = _FakeRunner()
    rec = TargetedRecovery(runner.qt)
    unlocked_at = []

    def unlock(volume_offset):
        """Stands in for the password dialog + libfvde unlock."""
        unlocked_at.append(volume_offset)
        mapping = Mapping(WORKFLOW_OFFSET, len(plaintext),
                          [Segment(0, WORKFLOW_SHIFT, len(plaintext))],
                          trusted=True)
        decrypt.register(ctx.outfile, CoreStorageSource(Unlocked(), mapping))
        return True

    rec.unlock_requested = unlock
    results, plans, phases = [], [], []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    rec.planSelected.connect(plans.append)
    rec.phaseStep.connect(phases.append)
    try:
        rec.start(ctx, include_filedata=True)
    finally:
        decrypt.unregister(ctx.outfile)

    assert unlocked_at == [WORKFLOW_OFFSET]
    # One run: container metadata, then the whole HFS+ plan.
    assert phases[0] == Phase.GET_CSMETA
    for phase in (Phase.GET_VOLHEADER, Phase.GET_CATALOG, Phase.GET_FILEDATA):
        assert phase in phases, phases
    assert plans and (Phase.GET_CATALOG, "Catalog B-tree") in plans[0]
    assert results and results[0][0] is True, results
    assert "Unlock the volume" not in results[0][1]

    # The metadata phase used raw offsets; every later phase went through the
    # mapping. Both in one run is the thing that could regress silently.
    base = WORKFLOW_OFFSET + WORKFLOW_SHIFT
    targeted = runner.targeted()
    assert any(pos < base for pos, _n in targeted)                 # csmeta, raw
    assert any(base <= pos < base + len(plaintext) for pos, _n in targeted)


def test_a_failing_unlock_handler_cannot_wedge_the_run(tmp_path):
    from app.core.recovery import Phase, TargetedRecovery

    ctx = _context(tmp_path, 512 << 20)
    with open(ctx.outfile, "wb") as fh:
        fh.truncate(512 << 20)
        fh.seek(WORKFLOW_OFFSET)
        fh.write(make_header(size=512 << 20,
                             metadata_blocks=(1025, TAIL_BLOCK)))

    runner = _FakeRunner()
    rec = TargetedRecovery(runner.qt)

    def boom(_offset):
        raise RuntimeError("libfvde exploded")

    rec.unlock_requested = boom
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    rec.start(ctx)

    assert results and results[0][0] is True
    assert "Unlock the volume" in results[0][1]
    assert rec._phase == Phase.DONE and not rec.active
