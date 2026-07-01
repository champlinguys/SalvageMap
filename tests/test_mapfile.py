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

from app.core import mapfile
from app.core.mapfile import Block, FINISHED, NON_TRIED

SAMPLE = """# Mapfile. Created by GNU ddrescue version 1.30
# Command line: ddrescue /dev/sdb out.img out.map
# Start time:   2026-06-22 22:00:00
0x00001000     ?               1
0x00000000  0x00001000  +
0x00001000  0x00001000  ?
0x00002000  0x00001000  -
0x00003000  0x00001000  +
"""


def test_parse_basic():
    mf = mapfile.parse_text(SAMPLE)
    assert mf.current_pos == 0x1000
    assert mf.current_status == "?"
    assert mf.current_pass == 1
    assert len(mf.blocks) == 4
    assert mf.blocks[0] == Block(0x0000, 0x1000, "+")
    assert mf.blocks[2] == Block(0x2000, 0x1000, "-")


def test_domain_geometry_and_totals():
    mf = mapfile.parse_text(SAMPLE)
    assert mf.domain_start == 0
    assert mf.domain_end == 0x4000
    assert mf.domain_size == 0x4000
    totals = mf.status_totals()
    assert totals[FINISHED] == 0x2000
    assert totals["-"] == 0x1000
    assert mf.rescued_bytes() == 0x2000


def test_roundtrip():
    mf = mapfile.parse_text(SAMPLE)
    text = mapfile.to_text(mf)
    mf2 = mapfile.parse_text(text)
    assert mf2.blocks == mf.blocks
    assert mf2.current_pos == mf.current_pos


def test_aggregate_one_cell_per_block():
    mf = mapfile.parse_text(SAMPLE)
    cells = mapfile.aggregate(mf.blocks, mf.domain_start, mf.domain_size, 4)
    assert cells == ["+", "?", "-", "+"]


def test_aggregate_worst_wins_when_downsampling():
    mf = mapfile.parse_text(SAMPLE)
    # 2 cells over 4 equal blocks: cell0 spans + and ?, cell1 spans - and +.
    # Worst-wins -> non-tried for cell0, bad-sector for cell1.
    cells = mapfile.aggregate(mf.blocks, mf.domain_start, mf.domain_size, 2)
    assert cells == ["?", "-"]


def test_aggregate_all_finished_is_green():
    blocks = [Block(0, 0x4000, "+")]
    cells = mapfile.aggregate(blocks, 0, 0x4000, 8)
    assert cells == ["+"] * 8


def test_aggregate_upsample_beyond_one_byte_per_cell():
    blocks = [Block(0, 2, "+"), Block(2, 2, "-")]
    cells = mapfile.aggregate(blocks, 0, 4, 8)
    # First half finished, second half bad.
    assert cells[:4] == ["+"] * 4
    assert cells[4:] == ["-"] * 4


def test_parse_tolerates_partial_trailing_line():
    partial = SAMPLE + "0x00004000  0x000"  # truncated mid-write
    mf = mapfile.parse_text(partial)
    assert len(mf.blocks) == 4  # bad trailing line ignored


def test_empty_mapfile():
    mf = mapfile.parse_text("# nothing here\n")
    assert mf.blocks == []
    assert mapfile.aggregate(mf.blocks, 0, 0, 10) == []


def test_aggregate_progress_partial_green():
    # One small finished region inside a large non-tried domain.
    blocks = [
        Block(0, 100, "+"),
        Block(100, 900, "?"),
    ]
    cells = mapfile.aggregate_progress(blocks, 0, 1000, 10)  # 100 bytes/cell
    assert cells[0] == ("+", 1.0)           # first cell fully finished
    assert cells[1][0] == "?"               # rest non-tried
    assert all(c[0] == "?" for c in cells[1:])


def test_aggregate_progress_fraction_on_huge_cell():
    # 50 finished bytes in a 1000-byte cell -> 5% green.
    blocks = [Block(0, 50, "+"), Block(50, 950, "?")]
    cells = mapfile.aggregate_progress(blocks, 0, 1000, 1)
    status, frac = cells[0]
    assert status == "+"
    assert abs(frac - 0.05) < 1e-6


def test_aggregate_progress_bad_sector_wins():
    blocks = [Block(0, 500, "+"), Block(500, 1, "-"), Block(501, 499, "?")]
    cells = mapfile.aggregate_progress(blocks, 0, 1000, 1)
    # A single bad sector keeps the whole cell flagged bad (visible).
    assert cells[0] == ("-", 1.0)
