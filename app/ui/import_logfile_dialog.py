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

"""Import a previous ddrescue logfile and its image to resume a session.

Picks up where a prior run left off: given a logfile, the matching image is
auto-detected (same path, ``.img``) so you don't have to re-select it. Untick the
box to point at a different image. Both pickers open *existing* files, so there
is no "overwrite?" prompt — nothing is being created here.
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


class ImportLogfileDialog(QDialog):
    def __init__(self, parent=None, logfile: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Import ddrescue logfile")
        self.resize(660, 200)
        self._image = ""
        self._logfile = ""

        info = QLabel("Resume from an existing image + ddrescue logfile.")
        info.setStyleSheet(f"color:{theme.FG_DIM}; padding-bottom:6px;")

        self.log_edit = QLineEdit(logfile)
        log_browse = QPushButton("Browse…")
        log_browse.clicked.connect(self._browse_log)

        self.auto_image = QCheckBox("Use image found next to the logfile")
        self.auto_image.setChecked(True)
        self.auto_image.toggled.connect(self._on_auto_toggled)

        self.image_edit = QLineEdit()
        self.img_browse = QPushButton("Browse…")
        self.img_browse.clicked.connect(self._browse_image)
        self.image_status = QLabel()

        self.log_edit.textChanged.connect(self._sync_image)

        grid = QGridLayout()
        grid.addWidget(QLabel("ddrescue logfile:"), 0, 0)
        grid.addWidget(self.log_edit, 0, 1)
        grid.addWidget(log_browse, 0, 2)
        grid.addWidget(self.auto_image, 1, 1)
        grid.addWidget(QLabel("Image file:"), 2, 0)
        grid.addWidget(self.image_edit, 2, 1)
        grid.addWidget(self.img_browse, 2, 2)
        grid.addWidget(self.image_status, 3, 1)
        grid.setColumnStretch(1, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(grid)
        layout.addStretch(1)
        layout.addWidget(self.buttons)

        self._sync_image()
        self._on_auto_toggled(True)

    # --- image auto-detection --------------------------------------------
    def _detected_image(self) -> str:
        base, _ = os.path.splitext(self.log_edit.text().strip())
        return base + ".img" if base else ""

    def _current_image(self) -> str:
        return self._detected_image() if self.auto_image.isChecked() else self.image_edit.text().strip()

    def _sync_image(self) -> None:
        if self.auto_image.isChecked():
            self.image_edit.setText(self._detected_image())
        self._update_status()

    def _on_auto_toggled(self, checked: bool) -> None:
        self.image_edit.setEnabled(not checked)
        self.img_browse.setEnabled(not checked)
        if checked:
            self._sync_image()
        self._update_status()

    def _update_status(self) -> None:
        img = self._current_image()
        exists = bool(img) and os.path.exists(img)
        if not img:
            self.image_status.setText("")
        elif exists:
            size = os.path.getsize(img)
            self.image_status.setText(f"✓ found ({size:,} bytes)")
            self.image_status.setStyleSheet("color:#3cb464;")
        else:
            self.image_status.setText("not found — untick to choose the image")
            self.image_status.setStyleSheet("color:#e13232;")
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(bool(self.log_edit.text().strip()) and exists)

    # --- browse -----------------------------------------------------------
    def _browse_log(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ddrescue logfile", self.log_edit.text(),
            "ddrescue logfiles (*.log *.map);;All files (*)",
        )
        if path:
            self.log_edit.setText(path)

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open image file", self.image_edit.text(),
            "Disk image (*.img);;All files (*)",
        )
        if path:
            self.image_edit.setText(path)
            self._update_status()

    # --- result -----------------------------------------------------------
    def accept(self) -> None:
        logfile = self.log_edit.text().strip()
        image = self._current_image()
        if not logfile or not image:
            return
        self._logfile, self._image = logfile, image
        super().accept()

    def result_paths(self) -> tuple[str, str]:
        """Return (image_path, logfile_path)."""
        return self._image, self._logfile
