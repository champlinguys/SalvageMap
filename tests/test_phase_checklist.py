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

"""Phase checklist state transitions (done / running / skipped / failed)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from app.ntfs.targeted_recovery import Phase
from app.ui import phase_checklist as pc

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _make():
    c = pc.PhaseChecklist()
    return c, c._order


def test_full_run_marks_skipped_and_done():
    c, order = _make()
    c.set_active(Phase.GET_TABLE)
    c.set_active(Phase.GET_BOOT)       # VBRs bypassed
    c.set_active(Phase.GET_FILEDATA)   # MFT0/MFT/INDEX bypassed
    st = dict(zip(order, c._states))
    assert st[Phase.GET_TABLE] == pc.DONE
    assert st[Phase.GET_BOOT] == pc.DONE
    assert st[Phase.GET_VBRS] == pc.SKIPPED
    assert st[Phase.GET_INDEX] == pc.SKIPPED
    assert st[Phase.GET_FILEDATA] == pc.RUNNING


def test_finish_ok_and_fail():
    c, order = _make()
    c.set_active(Phase.GET_MFT)
    c.mark_finished(True)
    assert c._states[order.index(Phase.GET_MFT)] == pc.DONE

    c.reset()
    c.set_active(Phase.GET_BOOT)
    c.mark_finished(False)
    assert c._states[order.index(Phase.GET_BOOT)] == pc.FAILED


def test_reset_clears_all():
    c, _ = _make()
    c.set_active(Phase.GET_MFT)
    c.reset()
    assert all(s == pc.PENDING for s in c._states)
