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

"""A tiny terminal screen emulator for ddrescue's live status output.

ddrescue prints a fixed status block and then *redraws it in place* using
carriage returns and ANSI cursor-up (``ESC[<n>A``) sequences. Appending that to
a plain log produces garbage. Instead we replay the control codes onto a virtual
screen so we can mirror exactly what ddrescue would show in a terminal, then
display the whole screen and replace it on each update.

Only the handful of control codes ddrescue actually emits are implemented
(CR, LF, cursor up/down/left/right, erase-to-end-of-line); everything else is
ignored. Escape sequences split across read chunks are buffered until complete.
"""

from __future__ import annotations


class LiveScreen:
    def __init__(self, max_lines: int = 400):
        self.lines: list[str] = [""]
        self.row = 0
        self.col = 0
        self.max_lines = max_lines
        self._pending = ""  # partial escape sequence carried between feeds

    def feed(self, text: str) -> None:
        text = self._pending + text
        self._pending = ""
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    self._pending = text[i:]
                    return
                if text[i + 1] != "[":
                    i += 2  # unsupported escape; skip ESC + next
                    continue
                j = i + 2
                num = ""
                while j < n and text[j].isdigit():
                    num += text[j]
                    j += 1
                if j >= n:
                    self._pending = text[i:]  # incomplete CSI; wait for more
                    return
                self._apply_csi(text[j], int(num) if num else 1)
                i = j + 1
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.row += 1
                self.col = 0
                self._ensure_row()
            elif ord(ch) >= 0x20:
                self._put(ch)
            i += 1
        self._trim()

    def render(self) -> str:
        return "\n".join(self.lines)

    def reset(self) -> None:
        self.lines = [""]
        self.row = self.col = 0
        self._pending = ""

    # --- internals --------------------------------------------------------
    def _apply_csi(self, cmd: str, count: int) -> None:
        if cmd == "A":
            self.row = max(0, self.row - count)
        elif cmd == "B":
            self.row += count
            self._ensure_row()
        elif cmd == "C":
            self.col += count
        elif cmd == "D":
            self.col = max(0, self.col - count)
        elif cmd == "K":
            self._ensure_row()
            self.lines[self.row] = self.lines[self.row][: self.col]
        # other CSI commands (colour 'm', clear-screen 'J', …) are ignored

    def _ensure_row(self) -> None:
        while self.row >= len(self.lines):
            self.lines.append("")

    def _put(self, ch: str) -> None:
        self._ensure_row()
        line = self.lines[self.row]
        if self.col > len(line):
            line += " " * (self.col - len(line))
        self.lines[self.row] = line[: self.col] + ch + line[self.col + 1:]
        self.col += 1

    def _trim(self) -> None:
        if len(self.lines) > self.max_lines:
            drop = len(self.lines) - self.max_lines
            self.lines = self.lines[drop:]
            self.row = max(0, self.row - drop)
