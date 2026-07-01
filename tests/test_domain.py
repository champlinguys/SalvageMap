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

from app.core import domain, mapfile
from app.core.mapfile import Block


def test_align_and_merge_sectors():
    # 100..600 with 512 sector -> aligned 0..1024
    merged = domain.align_and_merge([(100, 500)], sector_size=512)
    assert merged == [(0, 1024)]


def test_align_and_merge_coalesces_adjacent():
    merged = domain.align_and_merge(
        [(0, 512), (512, 512), (2048, 512)], sector_size=512
    )
    assert merged == [(0, 1024), (2048, 2560)]


def test_align_and_merge_overlap_and_unsorted():
    merged = domain.align_and_merge(
        [(4096, 512), (0, 1000), (512, 600)], sector_size=512
    )
    # (0,1000)->(0,1024); (512,1112)->(512,1536) overlaps -> (0,1536); (4096,4608)
    assert merged == [(0, 1536), (4096, 4608)]


def test_build_domain_mapfile_gapless_and_ordered():
    total = 8192
    mf = domain.build_domain_mapfile([(1024, 512), (4096, 1024)], total, 512)
    # Reconstruct full coverage
    assert mf.blocks[0].pos == 0
    # gapless: each block starts where the previous ended
    for a, b in zip(mf.blocks, mf.blocks[1:]):
        assert a.end == b.pos
    assert mf.blocks[-1].end == total
    finished = [b for b in mf.blocks if b.status == "+"]
    assert finished == [Block(1024, 512, "+"), Block(4096, 1024, "+")]
    assert domain.covered_bytes(mf) == 512 + 1024


def test_build_domain_roundtrips_through_parser():
    mf = domain.build_domain_mapfile([(2048, 2048)], 8192, 512)
    text = mapfile.to_text(mf)
    mf2 = mapfile.parse_text(text)
    assert [(b.pos, b.size, b.status) for b in mf2.blocks] == [
        (b.pos, b.size, b.status) for b in mf.blocks
    ]


def test_empty_ranges_gives_all_nontried():
    mf = domain.build_domain_mapfile([], 4096, 512)
    assert len(mf.blocks) == 1
    assert mf.blocks[0] == Block(0, 4096, "?")
    assert domain.covered_bytes(mf) == 0
