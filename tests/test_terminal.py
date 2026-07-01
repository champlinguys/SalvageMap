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

from app.core.terminal import LiveScreen


def test_carriage_return_overwrites():
    s = LiveScreen()
    s.feed("rescued: 0 B\r")
    s.feed("rescued: 50 B")
    assert s.render() == "rescued: 50 B"


def test_cursor_up_redraws_block_in_place():
    s = LiveScreen()
    s.feed("ipos: 0 B\nrescued: 0 B\n")
    # Move up two lines and rewrite the block (as ddrescue does each tick).
    s.feed("\x1b[2Aipos: 100 B\nrescued: 64 B\n")
    rows = s.render().split("\n")
    assert rows[0] == "ipos: 100 B"
    assert rows[1] == "rescued: 64 B"


def test_single_cursor_up_per_line():
    s = LiveScreen()
    s.feed("a\nb\n")
    s.feed("\x1b[A\x1b[Ax\ny\n")  # two separate ESC[A then overwrite
    out = s.render().split("\n")
    assert out[0] == "x"
    assert out[1] == "y"


def test_escape_split_across_feeds_is_buffered():
    s = LiveScreen()
    s.feed("line\n\x1b")   # ESC arrives with no bracket yet
    s.feed("[1Aover")       # completes the CSI on next chunk
    assert s.render().startswith("over")


def test_erase_to_end_of_line():
    s = LiveScreen()
    s.feed("hello world")
    s.feed("\r")            # column 0
    s.feed("hi\x1b[K")      # overwrite "he" -> "hi", erase rest of line
    assert s.render() == "hi"


def test_erase_to_end_of_line_mid():
    s = LiveScreen()
    s.feed("abcdef\rXYZ\x1b[K")  # overwrite abc->XYZ, erase "def"
    assert s.render() == "XYZ"


def test_reset_clears():
    s = LiveScreen()
    s.feed("stuff\nmore")
    s.reset()
    assert s.render() == ""
