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

"""Off-thread blocking jobs: the GUI must keep painting while they run."""

import threading
import time

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.ui.busy import run_blocking  # noqa: E402

_ = QApplication.instance() or QApplication([])


def test_returns_the_value():
    assert run_blocking(QWidget(), "…", lambda: 6 * 7) == 42


def test_reraises_on_the_calling_thread():
    def boom():
        raise OSError("no such image")

    with pytest.raises(OSError, match="no such image"):
        run_blocking(QWidget(), "…", boom)


def test_runs_off_the_gui_thread():
    gui_thread = threading.current_thread().ident
    where = run_blocking(QWidget(), "…", lambda: threading.current_thread().ident)
    assert where != gui_thread


def test_event_loop_keeps_running_during_a_long_job():
    """The whole point: a frozen event loop is what triggers "force quit?"."""
    ticks = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        run_blocking(QWidget(), "…", lambda: time.sleep(1.0))
    finally:
        timer.stop()
    # ~50 ticks are due; allow plenty of slack for a loaded CI box, but a
    # blocked loop would deliver none at all.
    assert len(ticks) > 10
