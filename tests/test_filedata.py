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

"""Image-parsing tests for the file-data domain (uses mkntfs if available)."""

import shutil
import subprocess

import pytest

from app.ntfs import targeted_recovery as tr

mkntfs = shutil.which("mkntfs")
pytestmark = pytest.mark.skipif(mkntfs is None, reason="mkntfs not available")


def _make_ntfs_image(path: str, size_mb: int = 16) -> None:
    with open(path, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    subprocess.run([mkntfs, "-F", "-Q", path], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_load_boot_and_mft_from_image(tmp_path):
    img = str(tmp_path / "vol.img")
    _make_ntfs_image(img)

    boot = tr.load_boot(img, 0)
    assert boot is not None
    assert boot.bytes_per_sector in (512, 4096)
    assert boot.mft_offset > 0

    mft_ranges = tr.load_mft_ranges(img, boot)
    assert mft_ranges, "should find the $MFT extent from record 0"


def test_collect_filedata_ranges_nonempty(tmp_path):
    img = str(tmp_path / "vol.img")
    _make_ntfs_image(img)
    boot = tr.load_boot(img, 0)
    mft_bytes = tr.assemble_mft(img, tr.load_mft_ranges(img, boot))

    ranges, n_files = tr.collect_filedata_ranges(boot, mft_bytes)
    # Even an empty volume has system metafiles ($LogFile, etc.) with $DATA.
    assert n_files > 0
    assert ranges
    # ranges are (start, length) with positive length
    assert all(length > 0 for _, length in ranges)


def test_build_filedata_domain(tmp_path):
    img = str(tmp_path / "vol.img")
    _make_ntfs_image(img)
    size = 16 * 1024 * 1024

    dmap = tr.build_filedata_domain(img, 0, size, 512)
    assert dmap is not None
    covered = sum(b.size for b in dmap.blocks if b.status == "+")
    assert 0 < covered < size  # some allocated data, but not the whole disk
    # gapless + ordered domain
    for a, b in zip(dmap.blocks, dmap.blocks[1:]):
        assert a.end == b.pos
    assert dmap.blocks[-1].end == size


def test_build_filedata_domain_on_garbage_returns_none(tmp_path):
    junk = str(tmp_path / "junk.img")
    with open(junk, "wb") as fh:
        fh.write(b"\x00" * (1024 * 1024))
    assert tr.build_filedata_domain(junk, 0, 1024 * 1024, 512) is None
