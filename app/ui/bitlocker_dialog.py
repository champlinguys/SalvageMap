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

"""Unlock a BitLocker volume found in the image, with its recovery key.

Shows what the FVE metadata says about each volume — encryption method, the
description Windows stored (usually the drive model), and the protector
*Identifier* — because the identifier is how you tell whether the recovery-key
file in front of you belongs to this drive at all. Getting that wrong is the
usual reason an unlock fails, so it is on screen before you type anything.

Nothing here touches the failing drive: the FVE metadata and the ciphertext all
come from the image already rescued.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from app.bitlocker.detect import LockedVolume
from app.bitlocker.keys import method_supported, parse_recovery_password
from app.ui import theme


def _humanize(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:,.0f} {unit}" if unit == "B" else f"{f:,.1f} {unit}"
        f /= 1024
    return str(n)


class BitLockerUnlockDialog(QDialog):
    """Pick a discovered BitLocker volume and enter its 48-digit recovery key."""

    def __init__(self, volumes: list[LockedVolume], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Unlock BitLocker volume")
        self.resize(660, 340)
        self._volumes = volumes
        self._key = ""

        info = QLabel(
            "Unlocking is read-only and in-memory: the image keeps its "
            "ciphertext, and the recovery key is never written to disk."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.FG_DIM}; padding-bottom:6px;")

        self.volume_box = QComboBox()
        for v in volumes:
            self.volume_box.addItem(
                f"0x{v.offset:X}  ({_humanize(v.size)})  {v.method_name}")
        self.volume_box.currentIndexChanged.connect(self._show_volume)

        self.identifier = QLabel()
        self.identifier.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.protectors = QLabel()
        self.protectors.setWordWrap(True)

        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText(
            "123456-123456-123456-123456-123456-123456-123456-123456")
        self.key_edit.textChanged.connect(self._validate)

        self.key_status = QLabel()
        self.key_status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Volume:", self.volume_box)
        form.addRow("Identifier:", self.identifier)
        form.addRow("Description:", self.description)
        form.addRow("Protectors:", self.protectors)
        form.addRow("Recovery key:", self.key_edit)
        form.addRow("", self.key_status)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Unlock")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(self.buttons)

        self._show_volume(0)
        self._validate()

    # --- volume details ---------------------------------------------------
    def selected_volume(self) -> LockedVolume:
        return self._volumes[max(self.volume_box.currentIndex(), 0)]

    def recovery_key(self) -> str:
        return self._key

    def _show_volume(self, _index: int) -> None:
        md = self.selected_volume().metadata
        ids = md.recovery_identifiers
        self.identifier.setText(", ".join(ids) if ids else "—")
        self.description.setText(md.description or "—")
        kinds = ", ".join(v.protection_type for v in md.vmks) or "none found"
        self.protectors.setText(kinds)
        self._validate()

    # --- key validation ---------------------------------------------------
    def _validate(self) -> None:
        volume = self.selected_volume()
        md = volume.metadata
        ok = True
        if not md.can_unlock_with_recovery_password:
            self._set_status(
                "This volume has no recovery-password protector — a recovery "
                "key cannot unlock it.", good=False)
            ok = False
        elif not method_supported(md.method):
            self._set_status(
                f"{md.method_name} is not supported yet (the Elephant-diffuser "
                "variants are unimplemented).", good=False)
            ok = False
        elif not self.key_edit.text().strip():
            self._set_status("Enter the 48-digit key from the recovery-key file.",
                             good=None)
            ok = False
        elif parse_recovery_password(self.key_edit.text()) is None:
            self._set_status(
                "Not a valid recovery key: it must be 8 groups of 6 digits, and "
                "each group is checked — re-read the mistyped one.", good=False)
            ok = False
        else:
            self._set_status(
                "Key is well-formed. Unlocking takes a few seconds (BitLocker's "
                "key derivation is deliberately slow).", good=True)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def _set_status(self, text: str, good: bool | None) -> None:
        colour = theme.FG_DIM if good is None else ("#3cb464" if good else "#e13232")
        self.key_status.setText(text)
        self.key_status.setStyleSheet(f"color:{colour};")

    def accept(self) -> None:
        self._key = self.key_edit.text().strip()
        super().accept()
