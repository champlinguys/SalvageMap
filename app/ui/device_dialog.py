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

"""Block-device picker — an lsblk-style table to choose a source device."""

from __future__ import annotations

import json
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app.ui import theme

LSBLK_FIELDS = "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MODEL,MOUNTPOINT,RM,RO,TRAN,VENDOR"
COLUMNS = ["Device", "Size", "Type", "FS", "Label", "Model", "Mount"]
_PATH_ROLE = Qt.UserRole + 1


def _humanize(n: int | None) -> str:
    if not n:
        return ""
    units = ["B", "K", "M", "G", "T", "P"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return str(n)


def list_block_devices() -> list[dict]:
    """Return the lsblk tree (raises on failure)."""
    out = subprocess.run(
        ["lsblk", "--json", "--bytes", "-o", LSBLK_FIELDS],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout).get("blockdevices", [])


class DeviceDialog(QDialog):
    """Pick a block device (disk or partition) to use as the rescue source."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose block device")
        self.resize(820, 460)
        self._selected: str | None = None

        layout = QVBoxLayout(self)
        header = QLabel("Select the source device or partition to recover from:")
        header.setStyleSheet(f"color:{theme.FG_DIM}; padding:2px 0 6px 0;")
        layout.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        controls = QHBoxLayout()
        self.show_virtual = QCheckBox("Show loop / virtual devices")
        self.show_virtual.toggled.connect(self.refresh)
        controls.addWidget(self.show_virtual)
        controls.addStretch(1)
        refresh_btn = QPushButton("⟲ Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use device")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._set_ok_enabled(False)

        self.refresh()

    # --- data -------------------------------------------------------------
    def refresh(self) -> None:
        self.tree.clear()
        try:
            devices = list_block_devices()
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            item = QTreeWidgetItem([f"lsblk failed: {exc}"])
            self.tree.addTopLevelItem(item)
            return
        shown = 0
        for dev in devices:
            if not self._include(dev):
                continue
            top = self._make_item(dev)
            self.tree.addTopLevelItem(top)
            for child in dev.get("children", []) or []:
                top.addChild(self._make_item(child))
            top.setExpanded(True)
            shown += 1
        if shown == 0:
            hint = QTreeWidgetItem(
                ["No physical devices found — tick “Show loop / virtual devices”."]
            )
            hint.setFlags(Qt.ItemIsEnabled)  # not selectable
            self.tree.addTopLevelItem(hint)
        self._set_ok_enabled(False)

    def _include(self, dev: dict) -> bool:
        if self.show_virtual.isChecked():
            return True
        return dev.get("type") not in ("loop", "rom")

    def _make_item(self, dev: dict) -> QTreeWidgetItem:
        fstype = dev.get("fstype") or ""
        item = QTreeWidgetItem([
            dev.get("path") or f"/dev/{dev.get('name', '?')}",
            _humanize(dev.get("size")),
            dev.get("type") or "",
            fstype,
            dev.get("label") or "",
            (dev.get("model") or dev.get("vendor") or "").strip(),
            dev.get("mountpoint") or "",
        ])
        path = dev.get("path") or f"/dev/{dev.get('name', '')}"
        item.setData(0, _PATH_ROLE, path)
        # Highlight sources our targeted workflow can recover (NTFS / ext / HFS+).
        if fstype.lower() in ("ntfs", "ext2", "ext3", "ext4", "hfsplus"):
            for col in range(item.columnCount()):
                item.setForeground(col, theme.qcolor(theme.ACCENT))
        elif dev.get("ro"):
            item.setForeground(0, theme.qcolor(theme.FG_DIM))
        return item

    # --- selection --------------------------------------------------------
    def _current_path(self) -> str | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, _PATH_ROLE)

    def _on_selection(self) -> None:
        self._set_ok_enabled(bool(self._current_path()))

    def _on_double_click(self, _item, _col) -> None:
        if self._current_path():
            self.accept()

    def _set_ok_enabled(self, enabled: bool) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(enabled)

    def accept(self) -> None:
        self._selected = self._current_path()
        if self._selected:
            super().accept()

    def selected_device(self) -> str | None:
        return self._selected
