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

"""Choose the output image file and the ddrescue logfile.

The image defaults to a ``.img`` file; the ddrescue logfile defaults to the same
name and directory with a ``.log`` extension. Untick "use default logfile" to
put the logfile somewhere else.

Note: the ddrescue *logfile* (what ddrescue historically called its "mapfile")
is the recovery progress map. It is distinct from the *domain files* we generate
for targeted imaging — those are always referred to as "domain files".
"""

from __future__ import annotations

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


class OutputDialog(QDialog):
    def __init__(self, device: str, parent=None, start_dir: str | None = None):
        super().__init__(parent)
        self.device = device
        self.setWindowTitle("Output image & ddrescue logfile")
        self.resize(660, 210)
        self._start_dir = start_dir or os.path.expanduser("~")
        self._image = ""
        self._logfile = ""

        default_name = (os.path.basename(device) or "image") + ".img"
        default_path = os.path.join(self._start_dir, default_name)

        info = QLabel(f"Source device:  {device}")
        info.setStyleSheet(f"color:{theme.FG_DIM}; padding-bottom:6px;")

        self.image_edit = QLineEdit(default_path)
        img_browse = QPushButton("Browse…")
        img_browse.clicked.connect(self._browse_image)

        self.default_log = QCheckBox("Use default logfile  (same name, .log)")
        self.default_log.setChecked(True)
        self.default_log.toggled.connect(self._on_default_toggled)

        self.log_edit = QLineEdit()
        self.log_browse = QPushButton("Browse…")
        self.log_browse.clicked.connect(self._browse_log)

        self.image_edit.textChanged.connect(self._sync_log)

        grid = QGridLayout()
        grid.addWidget(QLabel("Image file:"), 0, 0)
        grid.addWidget(self.image_edit, 0, 1)
        grid.addWidget(img_browse, 0, 2)
        grid.addWidget(self.default_log, 1, 1)
        grid.addWidget(QLabel("ddrescue logfile:"), 2, 0)
        grid.addWidget(self.log_edit, 2, 1)
        grid.addWidget(self.log_browse, 2, 2)
        grid.setColumnStretch(1, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Start setup")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(grid)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self._sync_log()
        self._on_default_toggled(True)

    # --- logfile defaulting ----------------------------------------------
    @staticmethod
    def _default_logpath(image: str) -> str:
        base, _ = os.path.splitext(image)
        return base + ".log" if base else ""

    def _sync_log(self) -> None:
        if self.default_log.isChecked():
            self.log_edit.setText(self._default_logpath(self.image_edit.text().strip()))

    def _on_default_toggled(self, checked: bool) -> None:
        self.log_edit.setEnabled(not checked)
        self.log_browse.setEnabled(not checked)
        if checked:
            self._sync_log()

    # --- browse -----------------------------------------------------------
    def _browse_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Output image file",
            self.image_edit.text() or self._start_dir,
            "Disk image (*.img);;All files (*)",
        )
        if path:
            self.image_edit.setText(path)

    def _browse_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "ddrescue logfile",
            self.log_edit.text() or self._start_dir,
            "ddrescue logfile (*.log);;All files (*)",
        )
        if path:
            self.log_edit.setText(path)

    # --- result -----------------------------------------------------------
    def accept(self) -> None:
        image = self.image_edit.text().strip()
        if not image:
            return
        if not os.path.splitext(image)[1]:
            image += ".img"
            self.image_edit.setText(image)
        if self.default_log.isChecked():
            self._sync_log()
        logfile = self.log_edit.text().strip()
        if not logfile:
            return
        self._image, self._logfile = image, logfile
        super().accept()

    def result_paths(self) -> tuple[str, str]:
        """Return (image_path, logfile_path)."""
        return self._image, self._logfile
