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

import struct

from app.ntfs import partition


def build_ntfs_vbr() -> bytearray:
    """A minimal 512-byte NTFS boot sector that boot_sector.parse accepts."""
    vbr = bytearray(512)
    vbr[0x03:0x0B] = b"NTFS    "
    struct.pack_into("<H", vbr, 0x0B, 512)   # bytes/sector
    vbr[0x0D] = 8                              # sectors/cluster
    struct.pack_into("<Q", vbr, 0x28, 200000) # total sectors
    struct.pack_into("<Q", vbr, 0x30, 4)      # $MFT LCN
    struct.pack_into("<Q", vbr, 0x38, 2)      # $MFTMirr LCN
    struct.pack_into("<b", vbr, 0x40, -10)    # 1024-byte MFT records
    struct.pack_into("<b", vbr, 0x41, 1)
    struct.pack_into("<Q", vbr, 0x48, 0x1122334455667788)
    vbr[510:512] = b"\x55\xaa"
    return vbr


def write_image(tmp_path, data: bytes):
    p = tmp_path / "disk.img"
    p.write_bytes(data)
    return str(p)


def test_mbr_single_ntfs_partition(tmp_path):
    mbr = bytearray(512)
    # primary entry 0: type 0x07 (NTFS), start LBA 1, 10 sectors
    off = 446
    mbr[off + 4] = 0x07
    struct.pack_into("<I", mbr, off + 8, 1)
    struct.pack_into("<I", mbr, off + 12, 10)
    mbr[510:512] = b"\x55\xaa"

    image = bytes(mbr) + bytes(build_ntfs_vbr()) + bytes(4096)
    path = write_image(tmp_path, image)

    parts = partition.scan_device(path)
    assert len(parts) == 1
    p = parts[0]
    assert p.scheme == "mbr"
    assert p.start == 512          # LBA 1 * 512
    assert p.size == 10 * 512
    assert p.is_ntfs is True
    assert partition.first_ntfs(parts) is p


def test_mbr_non_ntfs_partition_not_flagged(tmp_path):
    mbr = bytearray(512)
    off = 446
    mbr[off + 4] = 0x83  # Linux
    struct.pack_into("<I", mbr, off + 8, 1)
    struct.pack_into("<I", mbr, off + 12, 10)
    mbr[510:512] = b"\x55\xaa"
    image = bytes(mbr) + bytes(4096)  # no NTFS VBR
    parts = partition.scan_device(write_image(tmp_path, image))
    assert len(parts) == 1
    assert parts[0].is_ntfs is False
    assert partition.first_ntfs(parts) is None


def test_gpt_single_ntfs_partition(tmp_path):
    first_lba = 34
    start = first_lba * 512

    # Protective MBR (just a valid signature).
    mbr = bytearray(512)
    mbr[510:512] = b"\x55\xaa"

    # GPT header at LBA1.
    header = bytearray(512)
    header[0:8] = b"EFI PART"
    struct.pack_into("<Q", header, 72, 2)     # entries start at LBA 2
    struct.pack_into("<I", header, 80, 4)     # 4 entries
    struct.pack_into("<I", header, 84, 128)   # 128 bytes each

    # One used entry at LBA2.
    entries = bytearray(4 * 128)
    e = entries  # first entry at offset 0
    e[0:16] = bytes(range(1, 17))             # nonzero type GUID -> used
    struct.pack_into("<Q", e, 32, first_lba)  # first LBA
    struct.pack_into("<Q", e, 40, 100)        # last LBA
    e[56:56 + len("DATA") * 2] = "DATA".encode("utf-16-le")

    image = bytearray(start + 512)
    image[0:512] = mbr
    image[512:1024] = header
    image[1024:1024 + len(entries)] = entries
    image[start:start + 512] = build_ntfs_vbr()

    parts = partition.scan_device(write_image(tmp_path, bytes(image)))
    assert len(parts) == 1
    p = parts[0]
    assert p.scheme == "gpt"
    assert p.start == start
    assert p.size == (100 - first_lba + 1) * 512
    assert p.is_ntfs is True
    assert p.label == "DATA"


def test_no_partition_table(tmp_path):
    parts = partition.scan_device(write_image(tmp_path, bytes(4096)))
    assert parts == []
