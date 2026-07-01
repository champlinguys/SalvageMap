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

from app.ntfs import runlist
from app.ntfs.runlist import Run


def test_single_run_classic_example():
    # header 0x21: len_size=1, off_size=2; length=0x18=24; offset=0x5634
    runs = runlist.decode_runlist(bytes([0x21, 0x18, 0x34, 0x56, 0x00]))
    assert runs == [Run(24, 0x5634)]


def test_two_runs_positive_deltas():
    data = bytes([0x11, 0x30, 0x60,        # len 48, lcn 96
                  0x21, 0x10, 0x00, 0x01,  # len 16, delta +256 -> 352
                  0x00])
    runs = runlist.decode_runlist(data)
    assert runs == [Run(48, 96), Run(16, 352)]


def test_negative_delta():
    data = bytes([0x21, 0x10, 0x00, 0x02,  # len 16, lcn 512
                  0x11, 0x10, 0xFF,        # len 16, delta -1 -> 511
                  0x00])
    runs = runlist.decode_runlist(data)
    assert runs == [Run(16, 512), Run(16, 511)]


def test_sparse_run_has_no_lcn():
    # off_size 0 -> sparse hole
    data = bytes([0x01, 0x20,              # len 32, sparse
                  0x11, 0x10, 0x40,        # len 16, lcn 64 (delta from 0)
                  0x00])
    runs = runlist.decode_runlist(data)
    assert runs == [Run(32, None), Run(16, 64)]


def test_truncated_runlist_is_tolerated():
    runs = runlist.decode_runlist(bytes([0x21, 0x18, 0x34]))  # missing a byte
    assert runs == []


def test_runs_to_byte_ranges_skips_sparse():
    runs = [Run(48, 96), Run(32, None), Run(16, 352)]
    ranges = runlist.runs_to_byte_ranges(runs, cluster_size=4096)
    assert ranges == [(96 * 4096, 48 * 4096), (352 * 4096, 16 * 4096)]


def test_runs_to_byte_ranges_with_volume_offset():
    runs = [Run(1, 10)]
    ranges = runlist.runs_to_byte_ranges(runs, cluster_size=512, volume_offset=1_000_000)
    assert ranges == [(1_000_000 + 10 * 512, 512)]
