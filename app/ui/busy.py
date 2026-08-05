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

"""Run a long blocking job without freezing the window.

Several jobs here are minutes of pure-Python work over a huge structure: parsing
a multi-gigabyte ``$MFT``, sweeping an image for encrypted volumes, deriving a
BitLocker key, writing a report for half a million files. Run on the GUI thread
they stop the event loop, and the desktop then offers to force-quit the app —
which, mid-recovery, is the worst possible button to put in front of someone.

:func:`run_blocking` moves the job to a worker thread and spins a local event
loop behind a modal busy dialog, so the window keeps painting and the desktop
stays satisfied. The job itself is unchanged: it still blocks the *caller*, and
still returns its value (or raises) exactly as a direct call would — so call
sites keep reading top-to-bottom.

The work must not touch widgets: pass pure parsing/IO, and do the UI updates
with the value it returns.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from PySide6.QtCore import QEventLoop, Qt, QThread
from PySide6.QtWidgets import QApplication, QProgressDialog

T = TypeVar("T")


class _Worker(QThread):
    """Runs one callable, keeping its result or its exception for the caller."""

    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self._fn = fn
        self.result: object = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the GUI thread
            self.error = exc


def run_blocking(parent, message: str, fn: Callable[[], T],
                 title: str = "Working…") -> T:
    """Run ``fn()`` off the GUI thread, showing a busy dialog until it finishes.

    Returns whatever ``fn`` returns; an exception raised inside ``fn`` is
    re-raised here, so callers keep their existing ``try``/``except``.
    """
    dialog = QProgressDialog(message, "", 0, 0, parent)   # 0,0 = busy indicator
    dialog.setWindowTitle(title)
    dialog.setCancelButton(None)          # the jobs here have no safe abort point
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumWidth(420)

    worker = _Worker(fn)
    loop = QEventLoop()
    done = False

    def _on_finished() -> None:
        # Tracked with a flag as well as the loop: the signal is queued to this
        # thread, so it can be delivered by the processEvents() below — before
        # loop.exec() starts, which would then wait for a quit that never comes.
        nonlocal done
        done = True
        loop.quit()

    worker.finished.connect(_on_finished)
    worker.start()
    # If the job is quick, don't flash a dialog: give it a moment to finish first.
    if not worker.wait(150):
        dialog.show()
        QApplication.processEvents()
        if not done:
            loop.exec()
    worker.wait()
    dialog.close()

    if worker.error is not None:
        raise worker.error
    return worker.result  # type: ignore[return-value]
