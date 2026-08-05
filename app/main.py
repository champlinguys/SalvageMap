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

"""Application entry point."""

from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui import theme
from app.ui.main_window import MainWindow

ICON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "resources", "salvagemap.svg",
)


def _install_excepthook() -> None:
    """Report unhandled exceptions instead of letting them kill the process.

    PySide6 calls qFatal() — an immediate abort, no traceback the user ever sees
    under pkexec — when an exception escapes a slot and sys.excepthook is still
    the default. A rescue in progress must survive a bug in an unrelated slot:
    ddrescue keeps running and the mapfile keeps saving, so the user can stop
    cleanly and resume. Installing any hook of our own suppresses the abort.
    """
    def hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        text = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        box = QMessageBox(
            QMessageBox.Critical, "SalvageMap — internal error",
            f"{text}\n\nThe rescue is unaffected; progress is saved to the "
            "logfile. Please report this with the details below.",
        )
        box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
        box.exec()

    sys.excepthook = hook


def main() -> int:
    app = QApplication(sys.argv)
    _install_excepthook()
    app.setApplicationName("SalvageMap")
    app.setDesktopFileName("SalvageMap")   # link to the .desktop for taskbar grouping
    app.setStyle("Fusion")
    app.setFont(theme.app_font())
    app.setStyleSheet(theme.stylesheet())
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
