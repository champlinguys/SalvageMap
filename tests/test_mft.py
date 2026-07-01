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

from app.ntfs import mft
from app.ntfs.runlist import Run

RECORD_SIZE = 1024
SECTOR = 512
USN = 0xAAAA


def build_record(attr_type: int, flags: int, runlist_bytes: bytes) -> bytearray:
    """Construct a valid 1024-byte FILE record with one non-resident attr."""
    rec = bytearray(RECORD_SIZE)
    rec[0:4] = b"FILE"
    struct.pack_into("<H", rec, 0x04, 0x30)   # update-seq offset
    struct.pack_into("<H", rec, 0x06, 3)      # update-seq count (1 USN + 2 fixups)
    struct.pack_into("<H", rec, 0x14, 0x38)   # first attribute offset
    struct.pack_into("<H", rec, 0x16, flags)
    struct.pack_into("<I", rec, 0x18, 0x100)  # used size
    struct.pack_into("<I", rec, 0x1C, RECORD_SIZE)

    # Update sequence array: USN + original tail words.
    struct.pack_into("<H", rec, 0x30, USN)
    struct.pack_into("<H", rec, 0x32, 0x1111)
    struct.pack_into("<H", rec, 0x34, 0x2222)
    # On-disk, each sector tail holds the USN.
    struct.pack_into("<H", rec, SECTOR - 2, USN)
    struct.pack_into("<H", rec, 2 * SECTOR - 2, USN)

    # Non-resident attribute at 0x38.
    ao = 0x38
    runlist_off = 0x40
    attr_len = runlist_off + ((len(runlist_bytes) + 7) & ~7)
    struct.pack_into("<I", rec, ao + 0x00, attr_type)
    struct.pack_into("<I", rec, ao + 0x04, attr_len)
    rec[ao + 0x08] = 1            # non-resident
    rec[ao + 0x09] = 0            # name length
    struct.pack_into("<H", rec, ao + 0x0A, runlist_off)
    struct.pack_into("<H", rec, ao + 0x0E, 0)        # attr id
    struct.pack_into("<Q", rec, ao + 0x10, 0)        # start VCN
    struct.pack_into("<Q", rec, ao + 0x18, 47)       # last VCN
    struct.pack_into("<H", rec, ao + 0x20, runlist_off)
    struct.pack_into("<Q", rec, ao + 0x28, 48 * 4096)  # allocated
    struct.pack_into("<Q", rec, ao + 0x30, 48 * 4096)  # real
    struct.pack_into("<Q", rec, ao + 0x38, 48 * 4096)  # initialized
    rec[ao + runlist_off:ao + runlist_off + len(runlist_bytes)] = runlist_bytes

    # End marker after the attribute.
    struct.pack_into("<I", rec, ao + attr_len, 0xFFFFFFFF)
    return rec


# runlist: len 48 clusters @ lcn 96
RL = bytes([0x11, 0x30, 0x60, 0x00])


def test_parse_data_record():
    rec = build_record(mft.ATTR_DATA, mft.FLAG_IN_USE, RL)
    parsed = mft.parse_record(bytes(rec), record_number=5)
    assert parsed is not None
    assert parsed.in_use is True
    assert parsed.is_directory is False
    assert parsed.data_runs() == [Run(48, 96)]


def test_parse_directory_index_allocation():
    rec = build_record(
        mft.ATTR_INDEX_ALLOCATION, mft.FLAG_IN_USE | mft.FLAG_DIRECTORY, RL
    )
    parsed = mft.parse_record(bytes(rec))
    assert parsed.is_directory is True
    assert parsed.index_allocation_runs() == [Run(48, 96)]
    assert parsed.data_runs() == []


def test_fixups_actually_applied():
    rec = build_record(mft.ATTR_DATA, mft.FLAG_IN_USE, RL)
    record = bytearray(rec)
    ok = mft.apply_fixups(record, SECTOR)
    assert ok is True
    # Sector tails restored to the original words from the USA.
    assert struct.unpack_from("<H", record, SECTOR - 2)[0] == 0x1111
    assert struct.unpack_from("<H", record, 2 * SECTOR - 2)[0] == 0x2222


def test_corrupt_fixup_rejected():
    rec = build_record(mft.ATTR_DATA, mft.FLAG_IN_USE, RL)
    # Tear the record: a sector tail no longer matches the USN.
    struct.pack_into("<H", rec, SECTOR - 2, 0xBEEF)
    assert mft.parse_record(bytes(rec)) is None


def test_zero_region_is_skipped():
    assert mft.parse_record(bytes(RECORD_SIZE)) is None  # all zeros -> not "FILE"


def test_iter_records_skips_gaps():
    good = build_record(mft.ATTR_DATA, mft.FLAG_IN_USE, RL)
    stream = bytes(good) + bytes(RECORD_SIZE) + bytes(good)  # good, hole, good
    records = list(mft.iter_records(stream, RECORD_SIZE))
    assert len(records) == 2
    assert records[0].record_number == 0
    assert records[1].record_number == 2  # the zero record at index 1 skipped
