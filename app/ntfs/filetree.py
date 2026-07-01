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

"""Build a browseable file tree from a recovered ``$MFT``.

Turns the flat list of MFT records into a parent/child hierarchy with, for each
node, the absolute on-disk byte ranges that hold its content:

  * files       -> their ``$DATA`` runs
  * directories -> their ``$INDEX_ALLOCATION`` runs ($I30 B-tree blocks)

Those ranges are what the UI compares against the ddrescue mapfile to colour
each node clear / light green / dark green. Small files and directories whose
content is *resident* in the MFT record have no on-disk ranges — once the $MFT
itself is captured they are fully recovered, so ``resident`` is flagged.

Everything here parses the recovered image, never the failing device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ntfs import mft
from app.ntfs.boot_sector import BootSector
from app.ntfs.runlist import runs_to_byte_ranges


@dataclass
class FileNode:
    record_no: int
    name: str
    is_dir: bool
    parent_no: int
    ranges: list[tuple[int, int]] = field(default_factory=list)  # (start, len) bytes
    resident: bool = False    # content lives in the MFT record (no on-disk runs)
    size: int = 0             # logical size in bytes
    # False when the file is more fragmented than its extent map could be
    # resolved (HFS+ tail extents in an unrecovered Extents Overflow File): even
    # if every mapped range is imaged, the file is known-incomplete.
    fully_mapped: bool = True
    children: list[int] = field(default_factory=list)  # child record numbers


@dataclass
class FileTree:
    nodes: dict[int, FileNode]
    root: int = mft.ROOT_RECORD_NUMBER

    def children_of(self, record_no: int) -> list[FileNode]:
        node = self.nodes.get(record_no)
        if node is None:
            return []
        kids = [self.nodes[c] for c in node.children if c in self.nodes]
        kids.sort(key=lambda n: (not n.is_dir, n.name.lower()))
        return kids


def subtree_ranges(tree: FileTree, record_no: int) -> list[tuple[int, int]]:
    """All on-disk file-content ranges under ``record_no`` (itself + descendants).

    Used to image one folder's data *first* on a failing drive: we collect every
    non-directory node's ranges in the subtree (directory-metadata ranges are
    skipped — they came in with the catalog). Filesystem-agnostic: every plan
    builds the same :class:`FileTree` with per-node ``ranges``.
    """
    ranges: list[tuple[int, int]] = []
    seen: set[int] = set()
    stack = [record_no]
    while stack:
        num = stack.pop()
        if num in seen:
            continue
        seen.add(num)
        node = tree.nodes.get(num)
        if node is None:
            continue
        if not node.is_dir:
            ranges += node.ranges
        stack.extend(node.children)
    return ranges


def subtree_incomplete_count(tree: FileTree, record_no: int) -> int:
    """How many files under ``record_no`` have an incomplete extent map.

    These are files whose scattered tail can't be located yet (NTFS extension
    record / HFS+ overflow not recovered, ext interior extent block missing, or
    an unsupported indirect-block file). Imaging the folder still grabs what is
    mapped, but the count warns that some files can't come out whole yet.
    """
    n = 0
    seen: set[int] = set()
    stack = [record_no]
    while stack:
        num = stack.pop()
        if num in seen:
            continue
        seen.add(num)
        node = tree.nodes.get(num)
        if node is None:
            continue
        if not node.is_dir and not node.fully_mapped:
            n += 1
        stack.extend(node.children)
    return n


def resolve_runs(
    rec: mft.MftRecord,
    by_num: dict[int, mft.MftRecord],
    attr_type: int,
    boot: BootSector | None = None,
    read_volume=None,
) -> tuple[list, bool]:
    """Full run list for ``rec``'s ``attr_type`` stream, plus whether it is complete.

    A heavily-fragmented file spills its run list into *extension* MFT records;
    the base record's ``$ATTRIBUTE_LIST`` says which record holds each fragment.
    We stitch the fragments back together in VCN order. ``fully_mapped`` is False
    when a referenced extension record is missing from the recovered $MFT (so the
    file's tail can't be located), or when the attribute list itself couldn't be
    fully read — the same "known-incomplete" signal HFS+ uses for unresolved
    overflow extents.

    The attribute list is usually resident (inside ``rec``). On very fragmented
    files it is itself non-resident; given ``boot`` + a ``read_volume(offset,
    length)`` reader we read it from the imaged clusters, else we flag incomplete.
    """
    entries, list_ok = _list_entries(rec, boot, read_volume)
    if not entries:
        # No attribute list (list_ok True), or a non-resident one we couldn't
        # read (list_ok False): the base record's own runs are all we can see.
        return list(_attr_runs(rec, attr_type)), list_ok

    frags = sorted((e for e in entries if e.type == attr_type and e.name == ""),
                   key=lambda e: e.start_vcn)
    if not frags:
        # This stream isn't split out into the list (e.g. resident/small) — the
        # base record holds it whole.
        return list(_attr_runs(rec, attr_type)), True

    runs: list = []
    fully = list_ok          # a partially-read list may already be missing frags
    for rec_no in _ordered_unique(f.record_number for f in frags):
        src = by_num.get(rec_no)
        if src is None:
            fully = False          # extension record not recovered -> tail lost
            continue
        runs.extend(_attr_runs(src, attr_type))
    return runs, fully


def _list_entries(rec: mft.MftRecord, boot, read_volume) -> tuple[list, bool]:
    """``(entries, readable)`` for ``rec``'s $ATTRIBUTE_LIST.

    ``readable`` is False when a non-resident list's clusters weren't imaged (so
    some fragment entries may be missing) — the caller treats that as incomplete.
    """
    attr = rec.attribute_list()
    if attr is None:
        return [], True                       # no list at all -> complete
    if not attr.non_resident:
        return mft.parse_attribute_list(attr.resident_content), True
    if boot is None or read_volume is None:
        return [], False                      # non-resident, no reader -> unknown
    ranges = runs_to_byte_ranges(attr.runs, boot.bytes_per_cluster, boot.volume_offset)
    chunks: list[bytes] = []
    readable = bool(ranges)
    for start, length in ranges:
        data = read_volume(start, length)
        if not data or not any(data):
            readable = False                  # this part of the list isn't imaged
        chunks.append(data or b"")
    content = b"".join(chunks)
    if attr.real_size:
        content = content[:attr.real_size]
    if not any(content):
        return [], False
    return mft.parse_attribute_list(content), readable


def _attr_runs(rec: mft.MftRecord, attr_type: int) -> list:
    if attr_type == mft.ATTR_INDEX_ALLOCATION:
        return rec.index_allocation_runs()
    return rec.data_runs()


def _ordered_unique(items):
    seen: set = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def build_file_tree(boot: BootSector, mft_bytes: bytes, read_volume=None) -> FileTree:
    """Assemble a :class:`FileTree` from an assembled ``$MFT`` byte stream.

    ``read_volume(offset, length) -> bytes`` (optional) reads back from the image
    so non-resident ``$ATTRIBUTE_LIST`` attributes — the run maps of the most
    fragmented files — can be followed; without it those files are flagged
    incomplete rather than silently truncated.
    """
    nodes: dict[int, FileNode] = {}
    cluster = boot.bytes_per_cluster

    all_records = list(
        mft.iter_records(mft_bytes, boot.mft_record_size, boot.bytes_per_sector))
    by_num = {r.record_number: r for r in all_records}

    for rec in all_records:
        if not rec.in_use or rec.is_extension:
            continue
        fn = rec.best_file_name()
        if fn is None:
            continue  # nameless (e.g. metafiles without $FILE_NAME) — skip
        attr_type = mft.ATTR_INDEX_ALLOCATION if rec.is_directory else mft.ATTR_DATA
        runs, fully_mapped = resolve_runs(rec, by_num, attr_type, boot, read_volume)
        ranges = runs_to_byte_ranges(runs, cluster, boot.volume_offset)
        nodes[rec.record_number] = FileNode(
            record_no=rec.record_number,
            name=fn.name,
            is_dir=rec.is_directory,
            parent_no=fn.parent_ref,
            ranges=ranges,
            resident=not runs,   # no non-resident runs => content is in the MFT
            size=fn.real_size,
            fully_mapped=fully_mapped,
        )

    # Synthesize a root node if the real one (record 5) wasn't recovered, so the
    # tree always has somewhere to hang top-level entries.
    if mft.ROOT_RECORD_NUMBER not in nodes:
        nodes[mft.ROOT_RECORD_NUMBER] = FileNode(
            record_no=mft.ROOT_RECORD_NUMBER, name="\\", is_dir=True,
            parent_no=mft.ROOT_RECORD_NUMBER, resident=True,
        )

    # Link children to parents (orphans whose parent wasn't recovered hang off
    # the root so they stay reachable).
    root = mft.ROOT_RECORD_NUMBER
    for rec_no, node in nodes.items():
        if rec_no == root:
            continue
        parent = node.parent_no if node.parent_no in nodes else root
        if parent == rec_no:           # self-parent guard
            parent = root
        nodes[parent].children.append(rec_no)

    return FileTree(nodes=nodes, root=root)
