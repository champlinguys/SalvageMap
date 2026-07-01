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

"""Status panel: rescue progress, rates and the colour legend."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.ui.phase_checklist import PhaseChecklist
from app.ui.sector_map import STATUS_COLORS, STATUS_LABELS


def _humanize(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{n} B"


class Legend(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for ch, label in STATUS_LABELS.items():
            swatch = QLabel()
            pix = QPixmap(14, 14)
            pix.fill(STATUS_COLORS[ch])
            swatch.setPixmap(pix)
            layout.addWidget(swatch)
            layout.addWidget(QLabel(label))
            layout.addSpacing(8)
        layout.addStretch(1)


class StatusPanel(QWidget):
    """Key/value readout of the current rescue."""

    FIELDS = [
        ("source", "Source"),
        ("volume", "Volume"),
        ("output", "Output image"),
        ("logfile", "ddrescue logfile"),
        # "Domain" = the current domain file / phase (from ddrescue's live
        # output); "Total" = the whole image so far (from the polled mapfile).
        # Keeping them separate stops the two from overwriting each other.
        ("phase_rescued", "Domain rescued"),
        ("phase_pct", "Domain %"),
        ("total_rescued", "Total rescued"),
        ("total_pct", "Total %"),
        ("errsize", "Error size"),
        ("errors", "Error areas"),
        ("rate", "Current rate"),
        ("pass", "Pass"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._domain_size = 0       # bytes of the current domain file (0 = none)
        self._phase_pct_raw = "—"   # latest ddrescue "pct rescued" string
        outer = QVBoxLayout(self)
        form_host = QFrame()
        form = QFormLayout(form_host)
        self._labels: dict[str, QLabel] = {}
        for key, caption in self.FIELDS:
            lbl = QLabel("—")
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._labels[key] = lbl
            form.addRow(caption + ":", lbl)
        outer.addWidget(form_host)
        self._checklist_header = QLabel("Workflow:")
        outer.addWidget(self._checklist_header)
        self.checklist = PhaseChecklist()
        outer.addWidget(self.checklist)
        self.set_checklist_visible(False)  # only shown during targeted workflows
        outer.addWidget(QLabel("Legend:"))
        outer.addWidget(Legend())
        outer.addStretch(1)

    def set_checklist_visible(self, visible: bool) -> None:
        """Show/hide the workflow checklist (hidden for plain full-device runs)."""
        self._checklist_header.setVisible(visible)
        self.checklist.setVisible(visible)

    def set_field(self, key: str, value: str) -> None:
        if key in self._labels:
            self._labels[key].setText(value)

    def update_from_mapfile(self, mf) -> None:
        """Whole-image totals from the polled mapfile (accumulates all phases)."""
        totals = mf.status_totals()
        rescued = totals.get("+", 0)
        bad = totals.get("-", 0) + totals.get("/", 0) + totals.get("*", 0)
        size = mf.domain_size or 1
        self.set_field("total_rescued", _humanize(rescued))
        self.set_field("total_pct", f"{rescued / size * 100:.2f}%")
        self.set_field("errsize", _humanize(bad))
        self.set_field("pass", str(mf.current_pass))

    # ddrescue live-output token -> our field key. These reflect the CURRENT
    # domain file (the phase ddrescue is running right now), not the whole image.
    _STATUS_MAP = {
        "rescued": "phase_rescued",
        "pct rescued": "phase_pct",
        "current rate": "rate",
        "bad areas": "errors",
    }

    def update_from_status(self, status: dict) -> None:
        """Apply parsed ddrescue stdout fields for the current domain/phase."""
        for token, field in self._STATUS_MAP.items():
            if token not in status:
                continue
            if field == "phase_pct":
                self._phase_pct_raw = str(status[token])
                self._render_domain_pct()
            else:
                self.set_field(field, str(status[token]))

    def set_domain_size(self, n: int) -> None:
        """Size (bytes) of the domain file the current phase is imaging."""
        self._domain_size = n
        self._phase_pct_raw = "—"   # new domain: don't carry the old phase's %
        self._render_domain_pct()

    def _render_domain_pct(self) -> None:
        pct = self._phase_pct_raw
        if self._domain_size > 0:
            self.set_field("phase_pct", f"{pct}  of {_humanize(self._domain_size)}")
        else:
            self.set_field("phase_pct", pct)
