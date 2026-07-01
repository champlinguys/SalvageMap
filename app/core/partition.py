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

"""Scan a disk's partition table (MBR or GPT) to locate volumes.

Filesystem-agnostic: used so the user can hand us a whole disk (e.g. ``/dev/sdc``)
and we find each volume's byte offset *and* which filesystem it holds, to drive
the targeted recovery — instead of requiring them to pick the partition device.

We read the partition table directly from the device (just the first sectors,
plus a short read per partition) and identify the filesystem by probing each
partition's first blocks. ``start`` is the partition's absolute byte offset on
the disk — exactly the ``volume_offset`` the recovery workflow needs.

Adding a filesystem here is one entry in :data:`_FS_PROBES`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.ntfs import boot_sector

MBR_TYPE_NAMES = {
    0x05: "Extended", 0x0F: "Extended (LBA)", 0x85: "Linux extended",
    0x07: "NTFS/exFAT", 0x0B: "FAT32", 0x0C: "FAT32 (LBA)", 0x0E: "FAT16 (LBA)",
    0x82: "Linux swap", 0x83: "Linux", 0xEE: "GPT protective", 0xEF: "EFI System",
}
_EXTENDED_TYPES = {0x05, 0x0F, 0x85}
_GPT_SIGNATURE = b"EFI PART"
_MBR_BOOT_SIG = b"\x55\xaa"

# ext2/3/4 superblock: lives 1024 bytes into the volume; the 16-bit magic
# 0xEF53 sits at offset 0x38 within it (absolute 0x438 from the volume start).
EXT_SUPERBLOCK_OFFSET = 0x400
EXT_MAGIC_OFFSET = EXT_SUPERBLOCK_OFFSET + 0x38
EXT_MAGIC = 0xEF53

# HFS+ volume header: lives 1024 bytes into the volume; the 16-bit signature
# 'H+' (0x482B) or 'HX' (0x4858, HFSX) sits at its start (absolute 0x400).
HFSPLUS_HEADER_OFFSET = 0x400
HFSPLUS_SIGNATURES = (0x482B, 0x4858)

_PROBE_BYTES = 0x440  # enough to cover an NTFS VBR, the ext magic and the HFS+ sig

# Human labels for the detected filesystems.
FS_LABELS = {"ntfs": "NTFS/exFAT", "ext": "Linux (ext)",
             "hfsplus": "Mac OS (HFS+)", "": "Unknown"}
# Filesystems the targeted workflow can recover (drives picker highlighting).
RECOVERABLE = {"ntfs", "ext", "hfsplus"}


@dataclass
class Partition:
    index: int
    start: int          # absolute byte offset on the disk
    size: int           # bytes
    type_name: str
    fs_type: str        # "ntfs" | "ext" | "" (detected from the volume itself)
    scheme: str         # "mbr" | "gpt"
    label: str = ""

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def is_ntfs(self) -> bool:
        """Back-compat: NTFS was the only recognised filesystem originally."""
        return self.fs_type == "ntfs"

    @property
    def is_recoverable(self) -> bool:
        return self.fs_type in RECOVERABLE


def _pread(fd: int, offset: int, length: int) -> bytes:
    try:
        return os.pread(fd, length, offset)
    except OSError:
        return b""


# --- filesystem identification -------------------------------------------
def _looks_ntfs(head: bytes) -> bool:
    if len(head) < 0x50:
        return False
    try:
        boot_sector.parse(head)
        return True
    except ValueError:
        return False


def _looks_ext(head: bytes) -> bool:
    if len(head) < EXT_MAGIC_OFFSET + 2:
        return False
    return int.from_bytes(head[EXT_MAGIC_OFFSET:EXT_MAGIC_OFFSET + 2], "little") == EXT_MAGIC


def _looks_hfsplus(head: bytes) -> bool:
    if len(head) < HFSPLUS_HEADER_OFFSET + 2:
        return False
    sig = int.from_bytes(head[HFSPLUS_HEADER_OFFSET:HFSPLUS_HEADER_OFFSET + 2], "big")
    return sig in HFSPLUS_SIGNATURES


# Ordered (probe-on-bytes) pairs; first match wins. NTFS first since its VBR is
# at offset 0 and is the more specific signature.
_FS_PROBES = (("ntfs", _looks_ntfs), ("ext", _looks_ext), ("hfsplus", _looks_hfsplus))


def identify_filesystem(head: bytes) -> str:
    """Return the fs tag for a volume given its first :data:`_PROBE_BYTES`."""
    for tag, probe in _FS_PROBES:
        if probe(head):
            return tag
    return ""


def _fs_at(fd: int, start: int) -> str:
    return identify_filesystem(_pread(fd, start, _PROBE_BYTES))


def scan_device(path: str, sector_size: int = 512) -> list[Partition]:
    """Return the partitions found on ``path`` (raises OSError if unreadable)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        mbr = _pread(fd, 0, 512)
        if len(mbr) < 512 or mbr[510:512] != _MBR_BOOT_SIG:
            return []
        # A filesystem VBR (e.g. the user pointed us at a partition device, not a
        # whole disk) also ends in 0x55AA. If sector 0 itself identifies as a
        # filesystem, there is no partition table to read. (ext volumes have no
        # 0x55AA at offset 510, so they already return [] above.)
        if identify_filesystem(mbr):
            return []
        # GPT if LBA1 carries the EFI signature (protective MBR uses type 0xEE).
        gpt_header = _pread(fd, sector_size, 512)
        if gpt_header[:8] == _GPT_SIGNATURE:
            return _parse_gpt(fd, gpt_header, sector_size)
        return _parse_mbr(fd, mbr, sector_size)
    finally:
        os.close(fd)


# --- MBR ------------------------------------------------------------------
def _parse_mbr(fd: int, mbr: bytes, sector_size: int) -> list[Partition]:
    # Validate boot flags first: a real MBR entry's status byte is 0x00 or 0x80.
    # Anything else means we're looking at non-partition-table data (e.g. a FAT
    # VBR whose 0x55AA also matched), so report no table rather than garbage.
    for i in range(4):
        flag = mbr[446 + i * 16]
        if flag not in (0x00, 0x80):
            return []

    parts: list[Partition] = []
    idx = 1
    for i in range(4):
        entry = mbr[446 + i * 16:446 + (i + 1) * 16]
        ptype = entry[4]
        lba = int.from_bytes(entry[8:12], "little")
        count = int.from_bytes(entry[12:16], "little")
        if ptype == 0 or count == 0:
            continue
        if ptype in _EXTENDED_TYPES:
            idx = _parse_extended(fd, lba * sector_size, sector_size, parts, idx)
            continue
        start = lba * sector_size
        parts.append(_make_partition(
            fd, idx, start, count * sector_size,
            MBR_TYPE_NAMES.get(ptype, f"0x{ptype:02X}"), "mbr",
        ))
        idx += 1
    return parts


def _parse_extended(fd, ext_start, sector_size, parts, idx, guard=64) -> int:
    cur = ext_start
    seen = 0
    while cur and seen < guard:
        seen += 1
        ebr = _pread(fd, cur, 512)
        if len(ebr) < 512 or ebr[510:512] != _MBR_BOOT_SIG:
            break
        logical = ebr[446:462]
        nxt = ebr[462:478]
        ptype = logical[4]
        lba = int.from_bytes(logical[8:12], "little")
        count = int.from_bytes(logical[12:16], "little")
        if count and ptype:
            start = cur + lba * sector_size
            parts.append(_make_partition(
                fd, idx, start, count * sector_size,
                MBR_TYPE_NAMES.get(ptype, f"0x{ptype:02X}"), "mbr",
            ))
            idx += 1
        next_lba = int.from_bytes(nxt[8:12], "little")
        if next_lba == 0:
            break
        cur = ext_start + next_lba * sector_size
    return idx


# --- GPT ------------------------------------------------------------------
def _parse_gpt(fd: int, header: bytes, sector_size: int) -> list[Partition]:
    entries_lba = int.from_bytes(header[72:80], "little")
    n_entries = int.from_bytes(header[80:84], "little")
    entry_size = int.from_bytes(header[84:88], "little")
    if entry_size < 128 or n_entries == 0:
        return []
    n_entries = min(n_entries, 256)  # sanity clamp
    table = _pread(fd, entries_lba * sector_size, n_entries * entry_size)

    parts: list[Partition] = []
    idx = 1
    for i in range(n_entries):
        entry = table[i * entry_size:(i + 1) * entry_size]
        if len(entry) < 128 or entry[0:16] == b"\x00" * 16:
            continue  # unused
        first_lba = int.from_bytes(entry[32:40], "little")
        last_lba = int.from_bytes(entry[40:48], "little")
        if last_lba < first_lba:
            continue
        name = entry[56:128].decode("utf-16-le", errors="replace").rstrip("\x00")
        start = first_lba * sector_size
        size = (last_lba - first_lba + 1) * sector_size
        parts.append(_make_partition(fd, idx, start, size, "Basic data", "gpt", name))
        idx += 1
    return parts


def _make_partition(fd, idx, start, size, type_name, scheme, label="") -> Partition:
    """Build a Partition, identifying its filesystem from the volume itself.

    The on-disk partition *type* byte is only a hint; we confirm by probing the
    volume's first blocks, so a 0x83 "Linux" partition that actually holds ext
    is tagged ``ext`` (and a non-ext Linux partition stays untagged).
    """
    fs_type = _fs_at(fd, start)
    if fs_type:
        type_name = FS_LABELS[fs_type]
    return Partition(index=idx, start=start, size=size, type_name=type_name,
                     fs_type=fs_type, scheme=scheme, label=label)


def first_recoverable(parts: list[Partition]) -> Partition | None:
    """First partition holding a filesystem the workflow can recover."""
    for p in parts:
        if p.is_recoverable:
            return p
    return None


def first_ntfs(parts: list[Partition]) -> Partition | None:
    """Back-compat helper: first NTFS partition."""
    for p in parts:
        if p.is_ntfs:
            return p
    return None
