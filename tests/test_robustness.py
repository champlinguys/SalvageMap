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

"""Robustness on partial/garbage images: parsers skip, the engine never wedges.

These are regression tests for the post-review fixes:
  * ext parsers must return None on a short-but-magic-bearing buffer (the kind a
    not-yet-fully-imaged region produces) instead of raising struct.error,
  * a handler exception must end the workflow cleanly (engine disconnects, goes
    idle) instead of leaving the state machine wedged "active".
"""

import struct

from PySide6.QtCore import QObject, Signal

from app.core.ddrescue_runner import RescueSettings
from app.core.recovery import (
    FilesystemPlan,
    Phase,
    PhaseHandler,
    RecoveryContext,
    TargetedRecovery,
)
from app.ext import group_desc, inode
from app.ext.superblock import EXT_MAGIC, Superblock
from app.ext.superblock import parse as parse_superblock


# --- ext parser truncation tolerance -------------------------------------
def test_superblock_short_buffer_with_magic_returns_none():
    # Magic present at 0x38 but the buffer ends before blocks_hi at 0x150.
    buf = bytearray(0x140)
    struct.pack_into("<H", buf, 0x38, EXT_MAGIC)
    assert parse_superblock(bytes(buf), 0) is None  # must not raise


def test_inode_short_buffer_with_mode_returns_none():
    # A non-zero mode but the slice ends before size_hi at 0x6C (the final partial
    # inode of a truncated table read on a half-imaged volume).
    raw = bytearray(0x68)              # 104 bytes: past the old guard, before 0x70
    struct.pack_into("<H", raw, 0x00, 0x8000)  # regular-file mode
    assert inode.parse(bytes(raw), 99) is None  # must not raise


def test_group_desc_truncated_trailing_entry_uses_low_half():
    sb = Superblock(
        volume_offset=0, block_size=1024, inode_size=256, inodes_count=2048,
        blocks_count=8192, blocks_per_group=8192, inodes_per_group=2048,
        first_data_block=1, desc_size=64, incompat_64bit=True, first_ino=11,
    )
    # One descriptor, truncated to 40 bytes: holds the low half (0x08) but not the
    # 64-bit high half at 0x28. Must use the low half, not raise.
    entry = bytearray(40)
    struct.pack_into("<I", entry, 0x08, 98)
    blocks = group_desc.parse(bytes(entry), sb)
    assert blocks == [98]


# --- engine never wedges on a handler exception --------------------------
class _BoomPlan(FilesystemPlan):
    name = "boom"
    first_phase = Phase.GET_BOOT

    def steps(self):
        return [(Phase.GET_BOOT, "Boom")]

    def handler(self, phase):
        def _build(_st):
            raise RuntimeError("simulated parser bug")
        return PhaseHandler(Phase.GET_BOOT, "Boom", _build, lambda _st: None)


class _FakeRunner(QObject):
    finished = Signal(int)

    def __init__(self):
        super().__init__()
        self.connections = 0

    def start(self, *_a):
        pass

    def take_unaligned_error(self):
        return False


def test_handler_exception_fails_cleanly(tmp_path, monkeypatch):
    from app.core import volume
    monkeypatch.setattr(volume, "plan_for_fs", lambda _tag: _BoomPlan())

    infile = tmp_path / "src.img"
    infile.write_bytes(b"\x00" * 4096)
    runner = _FakeRunner()
    rec = TargetedRecovery(runner)
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))

    ctx = RecoveryContext(
        infile=str(infile), outfile=str(tmp_path / "out.img"),
        logfile=str(tmp_path / "out.log"), workdir=str(tmp_path),
        settings=RescueSettings(sector_size=512), volume_offset=0,
    )
    rec.start(ctx)

    assert results and results[0][0] is False
    assert "Internal error" in results[0][1]
    assert rec._phase == Phase.DONE
    assert not rec.active
    # The runner signal was disconnected, so a fresh run can start (no wedge).
    rec.start(ctx)
    assert len(results) == 2


class _MissingPhasePlan(FilesystemPlan):
    """A plan that targets a phase it never defines (handler lookup raises)."""
    name = "missing"
    first_phase = Phase.GET_DIRS   # but handler() below only knows GET_BOOT

    def steps(self):
        return [(Phase.GET_DIRS, "Dirs")]

    def handler(self, phase):
        return {Phase.GET_BOOT: PhaseHandler(
            Phase.GET_BOOT, "boot", lambda _s: None, lambda _s: None)}[phase]


def test_missing_phase_handler_fails_cleanly(tmp_path, monkeypatch):
    """A plan that enters an undefined phase must fail, not wedge on KeyError."""
    from app.core import volume
    monkeypatch.setattr(volume, "plan_for_fs", lambda _tag: _MissingPhasePlan())

    infile = tmp_path / "src.img"
    infile.write_bytes(b"\x00" * 4096)
    rec = TargetedRecovery(_FakeRunner())
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))

    ctx = RecoveryContext(
        infile=str(infile), outfile=str(tmp_path / "out.img"),
        logfile=str(tmp_path / "out.log"), workdir=str(tmp_path),
        settings=RescueSettings(sector_size=512), volume_offset=0,
    )
    rec.start(ctx)
    assert results and results[0][0] is False
    assert rec._phase == Phase.DONE and not rec.active
    rec.start(ctx)            # no wedge: a second run proceeds
    assert len(results) == 2
