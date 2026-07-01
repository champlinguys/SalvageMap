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

"""The targeted workflow escalates --sector-size on an unaligned read error."""

import pytest

from PySide6.QtCore import QObject, Signal

from app.core.ddrescue_runner import RescueSettings
from app.ntfs import targeted_recovery as tr
from app.ntfs.targeted_recovery import Phase, RecoveryContext, TargetedRecovery


class FakeRunner(QObject):
    """Stand-in for DdrescueRunner: records starts, fakes unaligned errors."""

    finished = Signal(int)

    def __init__(self):
        super().__init__()
        self.starts = []           # sector sizes seen per start()
        self._unaligned = False

    def start(self, infile, outfile, logfile, settings):
        self.starts.append(settings.sector_size)

    def take_unaligned_error(self):
        flag = self._unaligned
        self._unaligned = False
        return flag

    # test helper: pretend ddrescue died on an unaligned read, then signal done.
    def fail_unaligned(self):
        self._unaligned = True
        self.finished.emit(1)


def _ctx(tmp_path):
    infile = tmp_path / "src.img"
    infile.write_bytes(b"\x00" * (4 * 1024 * 1024))
    return RecoveryContext(
        infile=str(infile),
        outfile=str(tmp_path / "out.img"),
        logfile=str(tmp_path / "out.map"),
        workdir=str(tmp_path),
        settings=RescueSettings(sector_size=512),
        volume_offset=0,  # skip partition detection, go straight to boot phase
    )


def test_sector_size_escalates_512_4096_8192(tmp_path):
    runner = FakeRunner()
    rec = TargetedRecovery(runner)
    ctx = _ctx(tmp_path)
    # A pre-existing rescue's logfile that MUST survive sector-size escalation.
    with open(ctx.logfile, "w") as fh:
        fh.write("0x0 ? 1\n0x0 0x1000 +\n")
    rec.start(ctx, stop_after_mft=True)
    assert rec._phase == Phase.GET_BOOT
    assert runner.starts == [512]

    runner.fail_unaligned()                      # first unaligned error
    assert runner.starts == [512, 4096]          # retried, bumped to 4096
    assert rec._phase == Phase.GET_BOOT          # same phase

    runner.fail_unaligned()                      # still unaligned at 4096
    assert runner.starts == [512, 4096, 8192]    # bumped to 8192

    # The existing logfile (prior progress) must NOT have been deleted.
    import os
    assert os.path.exists(ctx.logfile)
    assert "+" in open(ctx.logfile).read()


def test_gives_up_after_8192(tmp_path):
    runner = FakeRunner()
    rec = TargetedRecovery(runner)
    failed = []
    rec.finished.connect(lambda ok, msg: failed.append((ok, msg)))
    ctx = _ctx(tmp_path)
    ctx.settings = RescueSettings(sector_size=8192)
    rec.start(ctx, stop_after_mft=True)

    runner.fail_unaligned()                      # nothing larger to try
    assert failed and failed[0][0] is False
    assert "Unaligned" in failed[0][1]
    assert rec._phase == Phase.DONE


def test_next_sector_size():
    assert TargetedRecovery._next_sector_size(512) == 4096
    assert TargetedRecovery._next_sector_size(4096) == 8192
    assert TargetedRecovery._next_sector_size(8192) is None
