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

"""Partition picker — choose which volume on a disk to target.

Shown after the user selects a whole disk; lets them pick the NTFS partition
(its byte offset becomes the recovery ``volume_offset``) or treat the whole
device as a single volume (offset 0).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app.core.partition import Partition
from app.ui import theme

COLUMNS = ["#", "Scheme", "Start", "Size", "Type", "Filesystem", "Label"]
_OFFSET_ROLE = Qt.UserRole + 1
_FS_ROLE = Qt.UserRole + 2


def _humanize(n: int) -> str:
    units = ["B", "K", "M", "G", "T", "P"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return str(n)


class PartitionDialog(QDialog):
    def __init__(self, partitions: list[Partition], parent=None, device: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Choose partition / volume")
        self.resize(720, 360)
        self._offset = 0
        self._fs_type = ""

        layout = QVBoxLayout(self)
        header = QLabel(
            f"Partitions found on {device or 'the disk'} — "
            "pick the volume to recover from:"
        )
        header.setStyleSheet(f"color:{theme.FG_DIM}; padding-bottom:6px;")
        layout.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.tree, 1)

        # "Whole device" option first (offset 0).
        whole = QTreeWidgetItem(["—", "", "0", "", "Whole device (no partition)", "", ""])
        whole.setData(0, _OFFSET_ROLE, 0)
        self.tree.addTopLevelItem(whole)

        preselect = whole
        for p in partitions:
            fs = p.fs_type.upper() if p.fs_type else ""
            item = QTreeWidgetItem([
                str(p.index), p.scheme.upper(),
                f"0x{p.start:X} ({_humanize(p.start)})", _humanize(p.size),
                p.type_name, fs, p.label,
            ])
            item.setData(0, _OFFSET_ROLE, p.start)
            item.setData(0, _FS_ROLE, p.fs_type)
            if p.is_recoverable:
                for col in range(item.columnCount()):
                    item.setForeground(col, theme.qcolor(theme.ACCENT))
                if preselect is whole:
                    preselect = item
            self.tree.addTopLevelItem(item)

        self.tree.setCurrentItem(preselect)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Use volume")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        items = self.tree.selectedItems()
        if items:
            self._offset = int(items[0].data(0, _OFFSET_ROLE))
            self._fs_type = items[0].data(0, _FS_ROLE) or ""
        super().accept()

    def selected_offset(self) -> int:
        return self._offset

    def selected_fs_type(self) -> str:
        """Filesystem tag of the chosen volume ("ntfs"/"ext"/"" for whole-device)."""
        return self._fs_type
