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

"""Transient-logfile-read guard: ddrescue rewrites its logfile in place, so a
poll can catch it truncated. The map must not blank on those partial reads."""

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.mapfile import Block, Mapfile  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_ = QApplication.instance() or QApplication([])

_BIG = Mapfile(blocks=[Block(0, 1_000_000_000, "+"),
                       Block(1_000_000_000, 800_000_000_000, "?")])


def test_empty_read_is_transient():
    assert MainWindow._mapfile_is_transient(Mapfile(blocks=[]), _BIG)


def test_collapsed_domain_is_transient():
    tiny = Mapfile(blocks=[Block(0, 65536, "+")])
    assert MainWindow._mapfile_is_transient(tiny, _BIG)


def test_full_read_is_kept():
    assert not MainWindow._mapfile_is_transient(_BIG, None)
    bigger = Mapfile(blocks=[Block(0, 2_000_000_000, "+"),
                             Block(2_000_000_000, 799_000_000_000, "?")])
    assert not MainWindow._mapfile_is_transient(bigger, _BIG)


def test_first_update_always_kept():
    # No prior map -> a non-empty read is accepted (e.g. a fresh/small drive).
    assert not MainWindow._mapfile_is_transient(
        Mapfile(blocks=[Block(0, 1024, "+")]), None)
