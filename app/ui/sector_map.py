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

"""Data Extractor / DMDE style sector map.

Renders a ddrescue mapfile as a wrapped grid of tall rectangles. Each cell
spans a range of the rescue domain and is coloured by the worst status of the
bytes under it (see :mod:`app.core.mapfile`), so a cell only turns green once
all of its sectors are finished. The grid updates live while a rescue runs.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from app.core import mapfile
from app.core.mapfile import Block

# Colours per status char.
STATUS_COLORS: dict[str, QColor] = {
    "+": QColor(40, 180, 70),     # finished  -> green
    "?": QColor(70, 70, 78),      # non-tried -> dark grey
    "*": QColor(225, 200, 40),    # non-trimmed -> yellow
    "/": QColor(235, 140, 30),    # non-scraped -> orange
    "-": QColor(210, 50, 50),     # bad-sector -> red
}
BACKGROUND = QColor(13, 17, 23)        # matches theme.BG (#0d1117)
CURSOR_COLOR = QColor(45, 212, 191)    # theme.ACCENT (teal)

STATUS_LABELS = {
    "+": "Finished",
    "?": "Non-tried",
    "*": "Non-trimmed",
    "/": "Non-scraped",
    "-": "Bad sector",
}

# Upper bound on rendered cells so the backing image and aggregation stay cheap
# even on multi-terabyte domains. (True 1-cell-per-sector zoom on huge disks is
# a future enhancement; here a cell floors at this resolution.)
MAX_CELLS = 400_000

# Default resolution for the "whole disk" pop-out: render this many cells across
# the entire domain (wrapped into as many rows as needed and scrolled), instead
# of only enough cells to fill the visible viewport. Zoom multiplies it, up to
# MAX_CELLS / one-cell-per-sector.
FULL_EXTENT_CELLS = 50_000


class SectorMapWidget(QWidget):
    """Scrollable grid of status cells. Place inside a QScrollArea."""

    # byte_offset is a full disk offset (can exceed 2**31), so pass it as a
    # Python object rather than a C++ 32-bit int.
    cellHovered = Signal(int, object, str)  # cell_index, byte_offset, status

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 120)
        self.setMouseTracking(True)

        self._blocks: list[Block] = []
        self._domain_start = 0
        self._domain_size = 0
        self._current_pos = 0
        self._sector_size = 512

        self.cell_w = 7
        self.cell_h = 16
        self.gap = 1
        self._zoom = 1.0  # multiplies the fit cell count
        self._full_extent = False  # render whole domain (scroll) vs. fit viewport

        self._cols = 1
        self._rows = 1
        self._n_cells = 0
        self._cells: list[str] = []
        self._image: QImage | None = None

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(120)  # debounce live updates
        self._render_timer.timeout.connect(self._rebuild)

    # --- data -------------------------------------------------------------
    def set_mapfile(self, mf: mapfile.Mapfile) -> None:
        self._blocks = mf.blocks
        self._domain_start = mf.domain_start
        self._domain_size = mf.domain_size
        self._current_pos = mf.current_pos
        self.schedule_rebuild()

    def set_sector_size(self, size: int) -> None:
        self._sector_size = max(1, size)

    def set_full_extent(self, on: bool) -> None:
        """Render the whole domain (wrapped + scrollable) instead of fitting it
        into the visible viewport. Used by the pop-out window."""
        self._full_extent = on
        self.schedule_rebuild()

    def schedule_rebuild(self) -> None:
        self._render_timer.start()

    # --- zoom -------------------------------------------------------------
    def zoom_in(self) -> None:
        self._zoom = min(self._zoom * 2.0, 1 << 20)
        self._rebuild()

    def zoom_out(self) -> None:
        self._zoom = max(self._zoom / 2.0, 1.0)
        self._rebuild()

    def zoom_fit(self) -> None:
        self._zoom = 1.0
        self._rebuild()

    # --- layout + render --------------------------------------------------
    def _max_useful_cells(self) -> int:
        if self._domain_size <= 0:
            return 0
        by_sector = max(1, self._domain_size // self._sector_size)
        return int(min(by_sector, MAX_CELLS))

    def _compute_n_cells(self) -> int:
        cols = max(1, (self.width()) // (self.cell_w + self.gap))
        if self._full_extent:
            base = min(self._max_useful_cells(), FULL_EXTENT_CELLS)
        else:
            visible_rows = max(1, (self.height()) // (self.cell_h + self.gap))
            base = cols * visible_rows
        n = int(base * self._zoom)
        return max(1, min(n, self._max_useful_cells()))

    def _rebuild(self) -> None:
        if self._domain_size <= 0:
            self._cells = []
            self._image = None
            self.setMinimumHeight(120)
            self.update()
            return

        self._cols = max(1, self.width() // (self.cell_w + self.gap))
        self._n_cells = self._compute_n_cells()
        self._rows = max(1, math.ceil(self._n_cells / self._cols))
        self._cells = mapfile.aggregate_progress(
            self._blocks, self._domain_start, self._domain_size, self._n_cells
        )
        self.setMinimumHeight(self._rows * (self.cell_h + self.gap) + self.gap)
        self._render_image()
        self.update()

    def _render_image(self) -> None:
        w = self._cols * (self.cell_w + self.gap) + self.gap
        h = self._rows * (self.cell_h + self.gap) + self.gap
        img = QImage(max(1, w), max(1, h), QImage.Format_RGB32)
        img.fill(BACKGROUND)
        painter = QPainter(img)
        for i, (status, frac) in enumerate(self._cells):
            col = i % self._cols
            row = i // self._cols
            x = self.gap + col * (self.cell_w + self.gap)
            y = self.gap + row * (self.cell_h + self.gap)
            painter.fillRect(x, y, self.cell_w, self.cell_h, self._cell_color(status, frac))
        # Current-position cursor.
        if self._domain_size > 0 and self._n_cells > 0:
            rel = self._current_pos - self._domain_start
            if 0 <= rel < self._domain_size:
                ci = min(self._n_cells - 1, int(rel / self._domain_size * self._n_cells))
                col = ci % self._cols
                row = ci // self._cols
                x = self.gap + col * (self.cell_w + self.gap)
                y = self.gap + row * (self.cell_h + self.gap)
                painter.setPen(CURSOR_COLOR)
                painter.drawRect(x - 1, y - 1, self.cell_w + 1, self.cell_h + 1)
        painter.end()
        self._image = img

    def _cell_color(self, status: str, frac: float) -> QColor:
        """Colour for a cell. Partially-finished cells are a dim green so that
        recovered data is visible even when no cell is 100% finished (common on
        a huge disk where each cell spans hundreds of MB)."""
        if status == "+":
            if frac >= 0.999:
                return STATUS_COLORS["+"]
            # Blend non-tried grey -> green by the finished fraction (with a
            # visible floor so even a sliver of recovery shows up).
            t = 0.2 + 0.8 * frac
            base = STATUS_COLORS["?"]
            green = STATUS_COLORS["+"]
            return QColor(
                int(base.red() + (green.red() - base.red()) * t),
                int(base.green() + (green.green() - base.green()) * t),
                int(base.blue() + (green.blue() - base.blue()) * t),
            )
        return STATUS_COLORS.get(status, STATUS_COLORS["?"])

    # --- Qt events --------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        self._rebuild()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        if self._image is not None:
            painter.drawImage(0, 0, self._image)
        painter.end()

    def _cell_at(self, x: int, y: int) -> int | None:
        col = (x - self.gap) // (self.cell_w + self.gap)
        row = (y - self.gap) // (self.cell_h + self.gap)
        if col < 0 or col >= self._cols or row < 0:
            return None
        idx = row * self._cols + col
        if 0 <= idx < self._n_cells:
            return idx
        return None

    def mouseMoveEvent(self, event):  # noqa: N802
        idx = self._cell_at(int(event.position().x()), int(event.position().y()))
        if idx is None or self._n_cells == 0:
            return
        offset = self._domain_start + int(idx / self._n_cells * self._domain_size)
        status = self._cells[idx][0] if idx < len(self._cells) else "?"
        self.cellHovered.emit(idx, offset, status)
