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

"""Collect on-disk ranges and build a file tree from a recovered HFS+ image.

The HFS+ counterpart of the ext plan's ``collect_*_ranges`` / ``build_tree``.
Unlike ext (where the directory hierarchy lives in separate directory blocks),
HFS+ keeps *everything* in the Catalog B-tree: each leaf record's key gives the
parent CNID and the name, and a file record carries its data fork's extents
inline. So walking the catalog leaves once yields both the name tree and every
file's data location — no separate directory phase is needed.

Heavily fragmented files (more than 8 extents) need the Extents Overflow File to
complete their extent list; we index it once into an :class:`ExtentsOverflow`.

Compressed files (HFS+ ``UF_COMPRESSED``) keep their data in the resource fork /
``com.apple.decmpfs`` attribute, not the data fork; they are counted and reported
as skipped, mirroring the ext plan's indirect-block files.

Everything reads the recovered image, never the failing device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

from app.hfsplus import extents
from app.hfsplus.btree import BTree, split_key_data
from app.hfsplus.extents import (
    FORK_TYPE_DATA,
    HFS_CATALOG_FILE_ID,
    HFS_EXTENTS_FILE_ID,
    ExtentsOverflow,
)
from app.hfsplus.volume_header import (
    VOLUME_HEADER_OFFSET,
    ForkData,
    VolumeHeader,
    parse as parse_volume_header,
)
from app.core.recovery import read_image
from app.ntfs.filetree import FileNode, FileTree

ROOT_CNID = 2                 # the volume root folder's catalog node ID

# Catalog leaf record types.
REC_FOLDER = 1
REC_FILE = 2

# HFSPlusCatalogFile data-record field offsets (after the key).
_FILE_ID_OFFSET = 0x08
_OWNER_FLAGS_OFFSET = 0x29     # HFSPlusBSDInfo.ownerFlags
_DATA_FORK_OFFSET = 0x58
_UF_COMPRESSED = 0x20


@dataclass
class CatalogEntry:
    cnid: int
    parent_id: int
    name: str
    is_dir: bool
    ranges: list[tuple[int, int]] = field(default_factory=list)
    size: int = 0
    compressed: bool = False
    # False when the fork is more fragmented than we could map (tail extents in
    # an Extents Overflow File we couldn't fully recover): the file is known to
    # be incompletely mappable and must never be reported/coloured as complete.
    fully_mapped: bool = True


def _fork_from(data: bytes, off: int) -> ForkData:
    logical = struct.unpack_from(">Q", data, off)[0]
    total = struct.unpack_from(">I", data, off + 12)[0]
    return ForkData(logical_size=logical, total_blocks=total,
                    extents=extents.extent_descriptors(data, off + 16))


def load_volume(image: str, volume_offset: int) -> VolumeHeader | None:
    """Parse the HFS+ volume header from the image, or None."""
    head = read_image(image, volume_offset + VOLUME_HEADER_OFFSET, 512)
    return parse_volume_header(head, volume_offset)


_MAX_BOOTSTRAP_ITERS = 8      # extents-overflow self-extension rounds (safety cap)


def _overflow_from_ranges(image: str,
                          ranges: list[tuple[int, int]]) -> ExtentsOverflow:
    bt = BTree(image, ranges)
    if not bt.ok:
        return ExtentsOverflow()
    records = (split_key_data(rec) for rec in bt.iter_leaf_records())
    return ExtentsOverflow.from_records(records)


def resolve_overflow_file(image: str,
                          vh: VolumeHeader) -> tuple[list[tuple[int, int]], ExtentsOverflow]:
    """Resolve the Extents Overflow File's *own* full extents and index it.

    The volume header carries only its first 8 extents. When the overflow file
    is itself fragmented past 8 extents, the rest are recorded inside it (keyed
    by :data:`HFS_EXTENTS_FILE_ID`) — so we bootstrap: index what the inline
    extents give us, use that to extend the overflow file's own extent list, and
    repeat until it stops growing. Returns ``(byte_ranges, index)``.
    """
    fork = vh.extents_overflow
    blocks = list(fork.extents)
    overflow = ExtentsOverflow()
    covered = -1
    for _ in range(_MAX_BOOTSTRAP_ITERS):
        ranges = extents.blocks_to_ranges(blocks, vh)
        overflow = _overflow_from_ranges(image, ranges)
        blocks, new_covered = extents.stitch_blocks(
            fork.extents, fork.total_blocks,
            overflow.extra(FORK_TYPE_DATA, HFS_EXTENTS_FILE_ID))
        if new_covered <= covered or new_covered >= fork.total_blocks:
            break                       # stable, or fully mapped
        covered = new_covered
    return extents.blocks_to_ranges(blocks, vh), overflow


def load_overflow(image: str, vh: VolumeHeader) -> ExtentsOverflow:
    """Index the Extents Overflow B-tree (empty if not imaged / no overflow)."""
    return resolve_overflow_file(image, vh)[1]


def catalog_ranges(image: str, vh: VolumeHeader,
                   overflow: ExtentsOverflow | None = None) -> list[tuple[int, int]]:
    """Full Catalog File byte ranges (inline extents + any Extents-Overflow tail).

    A large volume's catalog can exceed 8 extents; the rest are keyed by
    :data:`HFS_CATALOG_FILE_ID` in the Extents Overflow File, so we resolve it
    through the (bootstrapped) overflow index rather than trusting inline-only.
    """
    if overflow is None:
        overflow = load_overflow(image, vh)
    return extents.resolve_fork_coverage(
        vh.catalog, vh, HFS_CATALOG_FILE_ID, FORK_TYPE_DATA, overflow).ranges


def _parse_entry(key: bytes, data: bytes, vh: VolumeHeader,
                 overflow: ExtentsOverflow) -> CatalogEntry | None:
    if len(key) < 6 or len(data) < 2:
        return None
    rec_type = struct.unpack_from(">h", data, 0)[0]
    if rec_type not in (REC_FOLDER, REC_FILE):
        return None   # thread records (3/4) carry no name/extents we need
    parent_id = struct.unpack_from(">I", key, 0)[0]
    name_len = struct.unpack_from(">H", key, 4)[0]
    name = key[6:6 + 2 * name_len].decode("utf-16-be", errors="replace")
    if len(data) < _FILE_ID_OFFSET + 4:
        return None
    cnid = struct.unpack_from(">I", data, _FILE_ID_OFFSET)[0]

    if rec_type == REC_FOLDER:
        return CatalogEntry(cnid=cnid, parent_id=parent_id, name=name, is_dir=True)

    compressed = (len(data) > _OWNER_FLAGS_OFFSET
                  and bool(data[_OWNER_FLAGS_OFFSET] & _UF_COMPRESSED))
    fork = _fork_from(data, _DATA_FORK_OFFSET)
    if compressed:
        return CatalogEntry(cnid=cnid, parent_id=parent_id, name=name,
                            is_dir=False, size=fork.logical_size, compressed=True)
    res = extents.resolve_fork_coverage(fork, vh, cnid, FORK_TYPE_DATA, overflow)
    return CatalogEntry(cnid=cnid, parent_id=parent_id, name=name, is_dir=False,
                        ranges=res.ranges, size=fork.logical_size,
                        fully_mapped=res.fully_mapped)


def iter_entries(image: str, vh: VolumeHeader,
                 overflow: ExtentsOverflow | None = None) -> Iterator[CatalogEntry]:
    """Yield every folder/file entry by walking the Catalog B-tree leaves."""
    if overflow is None:
        overflow = load_overflow(image, vh)
    bt = BTree(image, catalog_ranges(image, vh, overflow))
    if not bt.ok:
        return
    for rec in bt.iter_leaf_records():
        key, data = split_key_data(rec)
        entry = _parse_entry(key, data, vh, overflow)
        if entry is not None:
            yield entry


@dataclass
class FiledataScan:
    """Result of walking the catalog for imageable file data.

    ``incomplete`` names the files whose extent map came up short (see
    :class:`~app.hfsplus.extents.ForkResolution`): the ranges we can image are
    included, but the file cannot be reconstructed in full from current
    metadata. Surfacing them lets the tech re-image the Extents Overflow File
    and try again rather than hand back a silently-truncated video.
    """
    ranges: list[tuple[int, int]] = field(default_factory=list)
    n_files: int = 0
    n_skipped: int = 0
    incomplete: list[tuple[str, int]] = field(default_factory=list)  # (name, size)


def scan_filedata(image: str, vh: VolumeHeader) -> FiledataScan:
    """All regular-file data-fork ranges plus counts and incompletely-mapped files."""
    overflow = load_overflow(image, vh)
    scan = FiledataScan()
    for entry in iter_entries(image, vh, overflow):
        if entry.is_dir:
            continue
        if entry.compressed:
            scan.n_skipped += 1
            continue
        if not entry.fully_mapped:
            scan.incomplete.append((entry.name, entry.size))
        if entry.ranges:
            scan.n_files += 1
            scan.ranges += entry.ranges
    return scan


def collect_filedata_ranges(image: str,
                            vh: VolumeHeader) -> tuple[list[tuple[int, int]], int, int]:
    """Back-compat shim: ``(ranges, n_files, n_skipped)``. See :func:`scan_filedata`."""
    scan = scan_filedata(image, vh)
    return scan.ranges, scan.n_files, scan.n_skipped


def build_tree(image: str, volume_offset: int) -> FileTree | None:
    """Rebuild a browseable FileTree from the catalog (root folder = CNID 2)."""
    vh = load_volume(image, volume_offset)
    if vh is None:
        return None
    overflow = load_overflow(image, vh)
    nodes: dict[int, FileNode] = {
        ROOT_CNID: FileNode(ROOT_CNID, "/", True, ROOT_CNID, resident=True)
    }
    for entry in iter_entries(image, vh, overflow):
        if entry.cnid == ROOT_CNID:
            continue   # the root folder's own record; the synthetic root stands in
        nodes[entry.cnid] = FileNode(
            record_no=entry.cnid, name=entry.name, is_dir=entry.is_dir,
            parent_no=entry.parent_id, ranges=entry.ranges,
            resident=not entry.ranges, size=entry.size,
            fully_mapped=entry.fully_mapped,
        )

    # Link children to parents (orphans whose parent wasn't recovered hang off
    # the root so they stay reachable), mirroring ext/ntfs build_tree.
    for cnid, node in nodes.items():
        if cnid == ROOT_CNID:
            continue
        parent = node.parent_no if node.parent_no in nodes else ROOT_CNID
        nodes[parent].children.append(cnid)

    return FileTree(nodes=nodes, root=ROOT_CNID)
