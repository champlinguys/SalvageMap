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

"""ddrescue settings dialog, with a 'failing drive' fast-first-pass preset."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.core.ddrescue_runner import RescueSettings, failing_drive_settings
from app.ui import theme


class SettingsDialog(QDialog):
    def __init__(self, settings: RescueSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ddrescue settings")
        self.resize(520, 420)
        self._settings = settings

        layout = QVBoxLayout(self)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Preset:"))
        fast = QPushButton("Failing drive (fast first pass)")
        fast.clicked.connect(self._apply_failing_preset)
        thorough = QPushButton("Thorough (retries)")
        thorough.clicked.connect(self._apply_thorough_preset)
        presets.addWidget(fast)
        presets.addWidget(thorough)
        presets.addStretch(1)
        layout.addLayout(presets)

        hint = QLabel(
            "Fast first pass: skip the slow trim/scrape phases, bail out if the "
            "drive stops responding, skip quickly past errors. Run it repeatedly "
            "(power-cycle the drive between runs) — ddrescue resumes from the logfile."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.FG_DIM};")
        layout.addWidget(hint)

        form = QFormLayout()
        self.sector_size = QSpinBox()
        self.sector_size.setRange(256, 1 << 20)
        self.sector_size.setSingleStep(512)
        form.addRow("Sector size (bytes):", self.sector_size)

        self.retry_passes = QSpinBox()
        self.retry_passes.setRange(0, 1000)
        form.addRow("Retry passes:", self.retry_passes)

        self.timeout = QLineEdit()
        self.timeout.setPlaceholderText("e.g. 30s  (blank = no timeout)")
        form.addRow("Timeout (--timeout):", self.timeout)

        self.skip_size = QLineEdit()
        self.skip_size.setPlaceholderText("e.g. 1MiB,64MiB")
        form.addRow("Skip size (--skip-size):", self.skip_size)

        self.min_read_rate = QLineEdit()
        self.min_read_rate.setPlaceholderText("e.g. 64Ki  (blank = off)")
        form.addRow("Min read rate (--min-read-rate):", self.min_read_rate)

        self.mapfile_interval = QLineEdit()
        self.mapfile_interval.setPlaceholderText("seconds, integer (e.g. 1)")
        form.addRow("Logfile save interval:", self.mapfile_interval)

        self.no_trim = QCheckBox("--no-trim (skip trimming phase)")
        self.no_scrape = QCheckBox("--no-scrape (skip scraping phase)")
        self.reverse = QCheckBox("--reverse (read back-to-front)")
        form.addRow(self.no_trim)
        form.addRow(self.no_scrape)
        form.addRow(self.reverse)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load(settings)

    # --- presets ----------------------------------------------------------
    def _apply_failing_preset(self) -> None:
        self._load(failing_drive_settings(self._current()))

    def _apply_thorough_preset(self) -> None:
        s = replace(self._current(), no_trim=False, no_scrape=False,
                    retry_passes=3, timeout="60s", skip_size=None)
        self._load(s)

    # --- load / read ------------------------------------------------------
    def _load(self, s: RescueSettings) -> None:
        self.sector_size.setValue(s.sector_size)
        self.retry_passes.setValue(s.retry_passes)
        self.timeout.setText(s.timeout or "")
        self.skip_size.setText(s.skip_size or "")
        self.min_read_rate.setText(s.min_read_rate or "")
        self.mapfile_interval.setText(s.mapfile_interval)
        self.no_trim.setChecked(s.no_trim)
        self.no_scrape.setChecked(s.no_scrape)
        self.reverse.setChecked(s.reverse)

    def _current(self) -> RescueSettings:
        return replace(
            self._settings,
            sector_size=self.sector_size.value(),
            retry_passes=self.retry_passes.value(),
            timeout=self.timeout.text().strip() or None,
            skip_size=self.skip_size.text().strip() or None,
            min_read_rate=self.min_read_rate.text().strip() or None,
            mapfile_interval=self.mapfile_interval.text().strip() or "1",
            no_trim=self.no_trim.isChecked(),
            no_scrape=self.no_scrape.isChecked(),
            reverse=self.reverse.isChecked(),
        )

    def result_settings(self) -> RescueSettings:
        return self._current()
