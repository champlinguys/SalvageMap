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

"""Live mirror of ddrescue's terminal output.

Shows exactly what ddrescue prints (its status block, redrawn in place), driven
by :class:`~app.core.terminal.LiveScreen`. The whole screen is replaced on each
update so the block updates in place instead of scrolling.
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit


class DdrescueView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("monospace")
        font.setStyleHint(QFont.TypeWriter)
        font.setPointSize(10)
        self.setFont(font)
        self.setStyleSheet(
            "QPlainTextEdit { background:#0b0f14; color:#d8dee9; "
            "border:1px solid #222b36; selection-background-color:#143b38; }"
        )
        self.setPlaceholderText("ddrescue output appears here while a rescue runs…")

    def set_screen(self, text: str) -> None:
        self.setPlainText(text)

    def clear_screen(self) -> None:
        self.setPlainText("")
