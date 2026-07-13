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

"""Options pop-up for the branded HTML file-tree export.

Lets a data-recovery professional optionally pick a logo image to embed in the
report header. The report date is auto-included and shown here read-only. Kept
deliberately minimal — logo + date only.
"""

from __future__ import annotations

import datetime
import os

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.ui import theme


class HtmlExportDialog(QDialog):
    def __init__(self, parent=None, start_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Export to HTML")
        self.resize(560, 170)
        self._start_dir = start_dir or os.path.expanduser("~")
        self._logo = ""

        info = QLabel(
            "Optionally add your logo to brand the report. The recovered-file "
            "list is embedded in a single HTML file you can share with the "
            "customer — it opens in any browser with no internet needed."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.FG_DIM}; padding-bottom:6px;")

        self.logo_edit = QLineEdit()
        self.logo_edit.setPlaceholderText("(optional) path to a PNG/JPG/SVG logo")
        logo_browse = QPushButton("Browse…")
        logo_browse.clicked.connect(self._browse_logo)

        date_label = QLabel(datetime.date.today().isoformat())
        date_label.setStyleSheet(f"color:{theme.FG_DIM};")

        self.hide_hidden = QCheckBox(
            "Hide hidden & system files  (.DS_Store, Spotlight, HFS+ private…)")
        self.hide_hidden.setChecked(True)

        grid = QGridLayout()
        grid.addWidget(QLabel("Logo image:"), 0, 0)
        grid.addWidget(self.logo_edit, 0, 1)
        grid.addWidget(logo_browse, 0, 2)
        grid.addWidget(QLabel("Report date:"), 1, 0)
        grid.addWidget(date_label, 1, 1)
        grid.addWidget(self.hide_hidden, 2, 1)
        grid.setColumnStretch(1, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(grid)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _browse_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose logo image",
            self.logo_edit.text() or self._start_dir,
            "Images (*.png *.jpg *.jpeg *.svg *.gif);;All files (*)",
        )
        if path:
            self.logo_edit.setText(path)

    def accept(self) -> None:
        self._logo = self.logo_edit.text().strip()
        super().accept()

    def logo_path(self) -> str | None:
        """Chosen logo path, or ``None`` if the field was left blank."""
        return self._logo or None

    def hide_hidden_files(self) -> bool:
        """Whether hidden / filesystem-internal entries should be omitted."""
        return self.hide_hidden.isChecked()
