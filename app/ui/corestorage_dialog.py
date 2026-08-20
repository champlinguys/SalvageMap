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

"""Unlock a CoreStorage (FileVault 2) volume found in the image, with its password.

Shows what the CoreStorage header says about each volume — the encryption
method and the physical volume's UUID — because on a Mac the tech usually has
several passwords to try (the account password, the disk password the customer
set, an old one) and no feedback beyond pass/fail. The UUID is on screen so it
can be matched against what macOS reported, before anything is typed.

The password field offers no validation beyond "not empty": unlike BitLocker's
recovery key, a CoreStorage passphrase is free-form and there is nothing to
check locally. The only test is the unlock itself.

Nothing here touches the failing drive: the CoreStorage metadata and the
ciphertext all come from the image already rescued.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.corestorage.detect import LockedVolume
from app.corestorage.keys import MISSING_LIBRARY_HINT, available
from app.ui import theme


def _humanize(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:,.0f} {unit}" if unit == "B" else f"{f:,.1f} {unit}"
        f /= 1024
    return str(n)


class CoreStorageUnlockDialog(QDialog):
    """Pick a discovered CoreStorage volume and enter its FileVault password."""

    def __init__(self, volumes: list[LockedVolume], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Unlock CoreStorage volume")
        self.resize(660, 380)
        self._volumes = volumes
        self._password = ""
        self._plist = ""

        info = QLabel(
            "Unlocking is read-only and in-memory: the image keeps its "
            "ciphertext, and the password is never written to disk."
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

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("The password the customer unlocks the disk with")
        self.password_edit.textChanged.connect(self._validate)

        self.show_password = QCheckBox("Show password")
        self.show_password.toggled.connect(
            lambda on: self.password_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        # Fallback for volumes whose EncryptedRoot.plist libfvde can't find in
        # the container itself; it lives on the Booter partition, so this is
        # only reachable when that partition made it into the image.
        self.plist_edit = QLineEdit()
        self.plist_edit.setPlaceholderText("optional — only if the unlock says it is needed")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_plist)
        plist_row = QHBoxLayout()
        plist_row.addWidget(self.plist_edit, 1)
        plist_row.addWidget(browse)

        self.status = QLabel()
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Volume:", self.volume_box)
        form.addRow("Identifier:", self.identifier)
        form.addRow("Encryption:", self.description)
        form.addRow("Password:", self.password_edit)
        form.addRow("", self.show_password)
        form.addRow("EncryptedRoot.plist:", plist_row)
        form.addRow("", self.status)

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

    def password(self) -> str:
        return self._password

    def encrypted_root_plist(self) -> str:
        return self._plist

    def _show_volume(self, _index: int) -> None:
        volume = self.selected_volume()
        self.identifier.setText(volume.identifier or "—")
        self.description.setText(volume.description or "—")
        self._validate()

    def _pick_plist(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select EncryptedRoot.plist.wipekey")
        if path:
            self.plist_edit.setText(path)

    # --- validation -------------------------------------------------------
    def _validate(self) -> None:
        ok = True
        if not available():
            self._set_status(MISSING_LIBRARY_HINT, good=False)
            ok = False
        elif not self.password_edit.text():
            self._set_status(
                "Enter the password the customer uses to unlock this disk on "
                "the Mac.", good=None)
            ok = False
        else:
            self._set_status(
                "Unlocking takes a few seconds (FileVault's key derivation is "
                "deliberately slow), then the volume is mapped so targeted "
                "imaging hits the right sectors.", good=True)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def _set_status(self, text: str, good: bool | None) -> None:
        colour = theme.FG_DIM if good is None else ("#3cb464" if good else "#e13232")
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{colour};")

    def accept(self) -> None:
        self._password = self.password_edit.text()
        self._plist = self.plist_edit.text().strip()
        super().accept()
