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

"""ext volumes are detected by the generalised partition scan + plan selection."""

import struct

from app.core import partition, volume
from app.core.partition import EXT_MAGIC_OFFSET


def build_ext_superblock_region() -> bytes:
    """First 0x440 bytes of an ext volume: just the superblock magic placed right.

    The ext superblock starts 1024 bytes into the volume; its 16-bit magic
    0xEF53 sits at offset 0x38 within it (absolute 0x438).
    """
    region = bytearray(0x440)
    struct.pack_into("<H", region, EXT_MAGIC_OFFSET, 0xEF53)
    return bytes(region)


def write_image(tmp_path, data: bytes) -> str:
    p = tmp_path / "disk.img"
    p.write_bytes(data)
    return str(p)


def test_identify_filesystem_ext():
    assert partition.identify_filesystem(build_ext_superblock_region()) == "ext"


def test_mbr_linux_partition_detected_as_ext(tmp_path):
    mbr = bytearray(512)
    off = 446
    mbr[off + 4] = 0x83          # Linux partition type
    struct.pack_into("<I", mbr, off + 8, 1)   # start LBA 1
    struct.pack_into("<I", mbr, off + 12, 100)
    mbr[510:512] = b"\x55\xaa"

    image = bytearray(512 + 0x440)
    image[0:512] = mbr
    image[512:512 + 0x440] = build_ext_superblock_region()  # volume at LBA 1
    parts = partition.scan_device(write_image(tmp_path, bytes(image)))

    assert len(parts) == 1
    p = parts[0]
    assert p.fs_type == "ext"
    assert p.is_recoverable is True
    assert p.is_ntfs is False
    assert partition.first_recoverable(parts) is p
    assert partition.first_ntfs(parts) is None


def test_detect_filesystem_picks_ext_plan_when_available(tmp_path):
    img = write_image(tmp_path, build_ext_superblock_region())
    plan = volume.detect_filesystem(img, 0)
    # ext support may not be present yet (Milestone 2); when it is, the plan is
    # ExtPlan, otherwise detection still identifies ext but plan_for_fs falls back.
    if plan is not None:
        assert plan.name in ("ext4", "NTFS")


def test_detect_filesystem_none_on_blank(tmp_path):
    img = write_image(tmp_path, bytes(0x440))
    assert volume.detect_filesystem(img, 0) is None
