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

"""Workflow phase checklist.

Renders the active filesystem plan's steps as a vertical list with per-step
state, so you can see what's done / running / skipped at a glance instead of only
the current phase. Driven by the engine's ``phaseStep``, ``workflowReset``,
``planSelected`` and ``finished`` signals. Defaults to the NTFS steps; the engine
swaps in the selected plan's steps via :meth:`PhaseChecklist.set_steps`.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ntfs.plan import PHASE_STEPS
from app.ui.sector_map import CURSOR_COLOR, STATUS_COLORS

PENDING, RUNNING, DONE, SKIPPED, FAILED = (
    "pending", "running", "done", "skipped", "failed",
)

_GLYPH = {PENDING: "○", RUNNING: "▶", DONE: "✓", SKIPPED: "⊘", FAILED: "✗"}
_COLOR = {
    PENDING: "#8b949e",
    RUNNING: CURSOR_COLOR.name(),
    DONE: STATUS_COLORS["+"].name(),
    SKIPPED: "#565a62",
    FAILED: STATUS_COLORS["-"].name(),
}


class PhaseChecklist(QWidget):
    """A checklist of workflow phases with done/running/skipped state."""

    def __init__(self, parent=None, steps=PHASE_STEPS):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(2)
        self._labels: list[QLabel] = []
        self.set_steps(steps)

    # --- engine-driven updates -------------------------------------------
    def set_steps(self, steps) -> None:
        """Rebuild the checklist rows for ``steps`` (a list of (Phase, label))."""
        self._order = [ph for ph, _ in steps]
        self._text = [label for _, label in steps]
        self._states = [PENDING] * len(self._order)
        # Drop any existing rows, then create one label per step.
        for lbl in self._labels:
            self._layout.removeWidget(lbl)
            lbl.deleteLater()
        self._labels = []
        for _ in self._order:
            lbl = QLabel()
            self._labels.append(lbl)
            self._layout.addWidget(lbl)
        self.reset()

    def reset(self) -> None:
        """All steps back to pending (a new run is starting)."""
        for i in range(len(self._states)):
            self._states[i] = PENDING
            self._render(i)

    def set_active(self, phase) -> None:
        """Mark ``phase`` as running; finish the prior step, skip bypassed ones."""
        if phase not in self._order:
            return
        idx = self._order.index(phase)
        for i, st in enumerate(self._states):
            if st == RUNNING:
                self._states[i] = DONE
        for i in range(idx):
            if self._states[i] == PENDING:
                self._states[i] = SKIPPED
        self._states[idx] = RUNNING
        for i in range(len(self._states)):
            self._render(i)

    def mark_finished(self, ok: bool) -> None:
        """The run ended: the running step becomes done (ok) or failed."""
        for i, st in enumerate(self._states):
            if st == RUNNING:
                self._states[i] = DONE if ok else FAILED
                self._render(i)

    # --- rendering --------------------------------------------------------
    def _render(self, i: int) -> None:
        st = self._states[i]
        self._labels[i].setText(f"{_GLYPH[st]}  {self._text[i]}")
        weight = "bold" if st == RUNNING else "normal"
        self._labels[i].setStyleSheet(f"color:{_COLOR[st]}; font-weight:{weight};")
