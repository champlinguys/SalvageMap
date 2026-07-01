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

"""Pop-out window showing the full-disk sector map.

A non-modal window with its own :class:`SectorMapWidget` in full-extent mode:
the entire rescue domain is rendered (most of it non-tried grey until imaged)
and wrapped into as many rows as needed, scrolled vertically. It mirrors the
main window's live mapfile so colours fill in as the rescue runs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import mapfile
from app.ui.sector_map import STATUS_COLORS, STATUS_LABELS, SectorMapWidget


class SectorMapWindow(QWidget):
    """Standalone, scrollable, whole-disk view of the sector map."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sector map — whole disk")
        self.setWindowFlag(Qt.Window, True)
        self.resize(900, 700)

        self.map = SectorMapWidget()
        self.map.set_full_extent(True)
        self.map.cellHovered.connect(self._on_hover)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_toolbar())
        layout.addLayout(self._build_legend())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.map)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)

        self._hover = QLabel("Hover a cell to see its disk offset.")
        layout.addWidget(self._hover)

    # --- live data passthrough -------------------------------------------
    def set_sector_size(self, size: int) -> None:
        self.map.set_sector_size(size)

    def set_mapfile(self, mf: mapfile.Mapfile) -> None:
        self.map.set_mapfile(mf)

    # --- UI ---------------------------------------------------------------
    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        for label, slot in (
            ("Zoom In", self.map.zoom_in),
            ("Zoom Out", self.map.zoom_out),
            ("Fit", self.map.zoom_fit),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        return row

    def _build_legend(self) -> QHBoxLayout:
        row = QHBoxLayout()
        for ch, text in STATUS_LABELS.items():
            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(
                f"background:{STATUS_COLORS[ch].name()}; border:1px solid #000;"
            )
            row.addWidget(swatch)
            row.addWidget(QLabel(text))
            row.addSpacing(8)
        row.addStretch(1)
        return row

    def _on_hover(self, _idx: int, offset, status: str) -> None:
        self._hover.setText(
            f"Offset 0x{int(offset):X} ({int(offset):,} B) — "
            f"{STATUS_LABELS.get(status, 'Non-tried')}"
        )
