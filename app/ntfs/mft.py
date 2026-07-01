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

"""Parse NTFS MFT (FILE) records and their attributes.

Each MFT record describes one file or directory as a sequence of attributes.
For targeted recovery we care about the run lists of:

  * ``$DATA``               (0x80) — file contents (and the $MFT's own extent)
  * ``$INDEX_ALLOCATION``   (0xA0) — directory index B-tree blocks ($I30)

On a half-recovered image, records may be missing (zeros) or corrupt, so every
record is validated (``FILE`` signature + update-sequence fixups) and bad ones
are skipped rather than trusted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

from app.ntfs.runlist import Run, decode_runlist

# Attribute type codes.
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0
ATTR_END = 0xFFFFFFFF

FLAG_IN_USE = 0x0001
FLAG_DIRECTORY = 0x0002

# $FILE_NAME namespaces, ordered worst -> best so we can prefer a real Win32
# name over a generated DOS 8.3 alias when a record carries several.
NS_POSIX, NS_WIN32, NS_DOS, NS_WIN32_DOS = 0, 1, 2, 3
_NS_PREFERENCE = {NS_DOS: 0, NS_POSIX: 1, NS_WIN32: 2, NS_WIN32_DOS: 3}

# Record number of the NTFS root directory ("\"); $FILE_NAME parent refs that
# point here mark a top-level entry.
ROOT_RECORD_NUMBER = 5


@dataclass
class FileName:
    """A parsed ``$FILE_NAME`` (0x30) attribute."""

    parent_ref: int          # MFT record number of the parent directory
    name: str
    namespace: int
    real_size: int           # logical file size (bytes)
    alloc_size: int          # allocated size (bytes)


@dataclass
class Attribute:
    type: int
    non_resident: bool
    name: str
    attr_id: int
    runs: list[Run] = field(default_factory=list)        # non-resident
    resident_content: bytes = b""                          # resident
    real_size: int = 0
    alloc_size: int = 0      # non-resident: on-disk allocated size (whole clusters)
    start_vcn: int = 0       # non-resident: first VCN this fragment covers


@dataclass
class AttrListEntry:
    """One entry of a ``$ATTRIBUTE_LIST`` (0x20): where an attribute fragment lives.

    A file too fragmented for its run list to fit in one MFT record spills the
    extra ``$DATA`` fragments into *extension* records; the base record's
    ``$ATTRIBUTE_LIST`` enumerates every fragment and which record holds it.
    """

    type: int
    name: str
    start_vcn: int
    record_number: int       # MFT record holding this fragment (base or extension)
    attr_id: int


@dataclass
class MftRecord:
    record_number: int
    in_use: bool
    is_directory: bool
    base_record: int = 0     # 0 for a base record; else the base it extends
    attributes: list[Attribute] = field(default_factory=list)

    @property
    def is_extension(self) -> bool:
        """True if this record only extends another (its attrs belong to base)."""
        return self.base_record != 0

    def attrs_of_type(self, attr_type: int) -> list[Attribute]:
        return [a for a in self.attributes if a.type == attr_type]

    def file_names(self) -> list[FileName]:
        """All parsed ``$FILE_NAME`` attributes (resident)."""
        out: list[FileName] = []
        for a in self.attrs_of_type(ATTR_FILE_NAME):
            fn = _parse_file_name(a.resident_content)
            if fn is not None:
                out.append(fn)
        return out

    def best_file_name(self) -> FileName | None:
        """Preferred name (Win32 over DOS 8.3), or None if no $FILE_NAME."""
        names = self.file_names()
        if not names:
            return None
        return max(names, key=lambda fn: _NS_PREFERENCE.get(fn.namespace, 1))

    def data_runs(self) -> list[Run]:
        runs: list[Run] = []
        for a in self.attrs_of_type(ATTR_DATA):
            runs.extend(a.runs)
        return runs

    def index_allocation_runs(self) -> list[Run]:
        runs: list[Run] = []
        for a in self.attrs_of_type(ATTR_INDEX_ALLOCATION):
            runs.extend(a.runs)
        return runs

    def unnamed_attr(self, attr_type: int) -> Attribute | None:
        """The first unnamed (main-stream) attribute of ``attr_type``, if any."""
        for a in self.attrs_of_type(attr_type):
            if a.name == "":
                return a
        return None

    def attribute_list(self) -> Attribute | None:
        return self.unnamed_attr(ATTR_ATTRIBUTE_LIST)

    def attribute_list_entries(self) -> list[AttrListEntry]:
        """Parsed ``$ATTRIBUTE_LIST`` entries (empty if none, or if non-resident).

        A non-resident attribute list keeps its entries in clusters on the
        volume that the recovered $MFT doesn't contain, so we can't read them
        here; the caller treats that as "cannot fully map" rather than trusting
        the base record alone.
        """
        attr = self.attribute_list()
        if attr is None or attr.non_resident:
            return []
        return parse_attribute_list(attr.resident_content)

    def has_nonresident_attribute_list(self) -> bool:
        attr = self.attribute_list()
        return attr is not None and attr.non_resident


def parse_attribute_list(content: bytes) -> list[AttrListEntry]:
    """Decode a resident ``$ATTRIBUTE_LIST`` body into its entries.

    Layout of each entry (variable length, packed):
      0x00 u32 type, 0x04 u16 entry length, 0x06 u8 name len, 0x07 u8 name off,
      0x08 u64 starting VCN, 0x10 u64 base file reference (low 48 bits = record
      number), 0x18 u16 attribute id, then the UTF-16LE name.
    """
    entries: list[AttrListEntry] = []
    pos = 0
    n = len(content)
    while pos + 0x1A <= n:
        attr_type = struct.unpack_from("<I", content, pos)[0]
        if attr_type == ATTR_END:
            break
        entry_len = struct.unpack_from("<H", content, pos + 0x04)[0]
        if entry_len < 0x1A or pos + entry_len > n:
            break
        name_len = content[pos + 0x06]
        name_off = content[pos + 0x07]
        start_vcn = struct.unpack_from("<Q", content, pos + 0x08)[0]
        ref = struct.unpack_from("<Q", content, pos + 0x10)[0] & ((1 << 48) - 1)
        attr_id = struct.unpack_from("<H", content, pos + 0x18)[0]
        name = ""
        if name_len:
            raw = content[pos + name_off:pos + name_off + name_len * 2]
            name = raw.decode("utf-16-le", errors="replace")
        entries.append(AttrListEntry(attr_type, name, start_vcn, ref, attr_id))
        pos += entry_len
    return entries


def apply_fixups(record: bytearray, sector_size: int = 512) -> bool:
    """Apply the update-sequence array in place.

    Returns False if the record fails fixup validation (== corrupt/torn write),
    in which case the caller should discard it.
    """
    if len(record) < 0x30:
        return False
    usa_off = struct.unpack_from("<H", record, 0x04)[0]
    usa_count = struct.unpack_from("<H", record, 0x06)[0]
    if usa_count == 0:
        return False
    if usa_off + usa_count * 2 > len(record):
        return False
    usn = record[usa_off:usa_off + 2]
    for i in range(1, usa_count):
        sector_tail = i * sector_size - 2
        if sector_tail + 2 > len(record):
            return False
        if record[sector_tail:sector_tail + 2] != usn:
            return False  # fixup mismatch -> torn/corrupt record
        fix = record[usa_off + i * 2:usa_off + i * 2 + 2]
        record[sector_tail:sector_tail + 2] = fix
    return True


def _parse_file_name(content: bytes) -> FileName | None:
    """Decode a ``$FILE_NAME`` attribute body. None if too short/garbage.

    Layout: parent ref (u64; low 48 bits = record number), then timestamps,
    allocated size (0x28), real size (0x30), flags (0x38), then at 0x40 the
    name length in chars, 0x41 the namespace, and the UTF-16LE name at 0x42.
    """
    if len(content) < 0x42:
        return None
    parent_ref = struct.unpack_from("<Q", content, 0x00)[0] & ((1 << 48) - 1)
    alloc_size = struct.unpack_from("<Q", content, 0x28)[0]
    real_size = struct.unpack_from("<Q", content, 0x30)[0]
    name_len = content[0x40]
    namespace = content[0x41]
    raw = content[0x42:0x42 + name_len * 2]
    if len(raw) < name_len * 2:
        return None
    name = raw.decode("utf-16-le", errors="replace")
    return FileName(parent_ref, name, namespace, real_size, alloc_size)


def _parse_attribute(data: bytes, off: int) -> tuple[Attribute | None, int]:
    """Parse one attribute at ``off``. Returns (attr_or_None, next_off)."""
    if off + 4 > len(data):
        return None, len(data)
    attr_type = struct.unpack_from("<I", data, off)[0]
    if attr_type == ATTR_END:
        return None, len(data)
    if off + 0x10 > len(data):
        return None, len(data)
    length = struct.unpack_from("<I", data, off + 0x04)[0]
    if length < 0x18 or off + length > len(data):
        return None, len(data)  # malformed; stop walking
    non_resident = data[off + 0x08] != 0
    name_len = data[off + 0x09]
    name_off = struct.unpack_from("<H", data, off + 0x0A)[0]
    attr_id = struct.unpack_from("<H", data, off + 0x0E)[0]

    name = ""
    if name_len:
        raw = data[off + name_off:off + name_off + name_len * 2]
        name = raw.decode("utf-16-le", errors="replace")

    attr = Attribute(type=attr_type, non_resident=non_resident, name=name, attr_id=attr_id)
    if non_resident:
        attr.start_vcn = struct.unpack_from("<Q", data, off + 0x10)[0]
        attr.alloc_size = struct.unpack_from("<Q", data, off + 0x28)[0]
        attr.real_size = struct.unpack_from("<Q", data, off + 0x30)[0]
        runlist_off = struct.unpack_from("<H", data, off + 0x20)[0]
        run_data = data[off + runlist_off:off + length]
        attr.runs = decode_runlist(run_data)
    else:
        content_len = struct.unpack_from("<I", data, off + 0x10)[0]
        content_off = struct.unpack_from("<H", data, off + 0x14)[0]
        attr.resident_content = data[off + content_off:off + content_off + content_len]
        attr.real_size = content_len

    return attr, off + length


def parse_record(
    raw: bytes,
    record_number: int = -1,
    sector_size: int = 512,
) -> MftRecord | None:
    """Parse a single MFT record. Returns None if missing/corrupt.

    The record is validated by signature and fixups; corrupt records (including
    all-zero regions on a partial image) yield None.
    """
    if len(raw) < 0x30 or raw[:4] != b"FILE":
        return None
    record = bytearray(raw)
    if not apply_fixups(record, sector_size):
        return None

    flags = struct.unpack_from("<H", record, 0x16)[0]
    first_attr = struct.unpack_from("<H", record, 0x14)[0]
    used_size = struct.unpack_from("<I", record, 0x18)[0]
    base_record = struct.unpack_from("<Q", record, 0x20)[0] & ((1 << 48) - 1)
    if first_attr >= len(record):
        return None
    limit = min(used_size, len(record)) if used_size else len(record)

    rec = MftRecord(
        record_number=record_number,
        in_use=bool(flags & FLAG_IN_USE),
        is_directory=bool(flags & FLAG_DIRECTORY),
        base_record=base_record,
    )

    off = first_attr
    data = bytes(record[:limit])
    while off < len(data):
        attr, off = _parse_attribute(data, off)
        if attr is None:
            break
        rec.attributes.append(attr)
    return rec


def iter_records(
    mft_bytes: bytes,
    record_size: int,
    sector_size: int = 512,
) -> Iterator[MftRecord]:
    """Yield every parseable record from an assembled $MFT byte stream.

    Missing/corrupt records are silently skipped (the whole point on a
    partially-recovered image).
    """
    n = len(mft_bytes) // record_size
    for i in range(n):
        chunk = mft_bytes[i * record_size:(i + 1) * record_size]
        rec = parse_record(chunk, record_number=i, sector_size=sector_size)
        if rec is not None:
            yield rec
