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

"""HFS+ parser tests: synthesised structures + a real mkfs.hfsplus image.

We can't mount HFS+ here (needs root/loopback), so file-content cases use a
synthesised image whose byte layout was verified against a real volume; the
mkfs.hfsplus-gated cases cover detection and the volume-header/catalog walk on a
genuine (empty) volume.
"""

import os
import shutil
import struct
import subprocess

import pytest

from app.hfsplus import catalog, extents
from app.hfsplus.btree import BTree, split_key_data
from app.hfsplus.extents import FORK_TYPE_DATA, ExtentsOverflow
from app.hfsplus.volume_header import ForkData, parse as parse_volume_header

BLOCK_SIZE = 4096


# --- synthesised on-disk structures --------------------------------------
def _volume_header(total_blocks, catalog_ext, extents_ext, block_size=BLOCK_SIZE):
    vh = bytearray(512)
    struct.pack_into(">H", vh, 0x00, 0x482B)        # 'H+'
    struct.pack_into(">H", vh, 0x02, 4)             # version
    struct.pack_into(">I", vh, 0x28, block_size)
    struct.pack_into(">I", vh, 0x2C, total_blocks)

    def put_fork(off, exts):
        struct.pack_into(">I", vh, off + 12, sum(c for _s, c in exts))
        for i, (sb, c) in enumerate(exts):
            struct.pack_into(">II", vh, off + 16 + i * 8, sb, c)

    put_fork(0xC0, extents_ext)    # extents overflow file
    put_fork(0x110, catalog_ext)   # catalog file
    return bytes(vh)


def _cat_record(parent, name, data):
    key = struct.pack(">IH", parent, len(name)) + name.encode("utf-16-be")
    return struct.pack(">H", len(key)) + key + data


def _folder_data(cnid):
    d = bytearray(0x58)
    struct.pack_into(">h", d, 0, 1)            # recordType folder
    struct.pack_into(">I", d, 8, cnid)         # folderID
    return bytes(d)


def _file_data(cnid, exts, logical, compressed=False):
    d = bytearray(0xF8)
    struct.pack_into(">h", d, 0, 2)            # recordType file
    struct.pack_into(">I", d, 8, cnid)         # fileID
    if compressed:
        d[0x29] |= 0x20                        # HFSPlusBSDInfo.ownerFlags UF_COMPRESSED
    struct.pack_into(">Q", d, 0x58, logical)   # dataFork.logicalSize
    struct.pack_into(">I", d, 0x58 + 12, sum(c for _s, c in exts))  # totalBlocks
    for i, (sb, c) in enumerate(exts):
        struct.pack_into(">II", d, 0x58 + 16 + i * 8, sb, c)
    return bytes(d)


def _header_node(node_size=BLOCK_SIZE, first_leaf=1):
    node = bytearray(node_size)
    struct.pack_into(">b", node, 8, 1)             # kind = header
    struct.pack_into(">H", node, 14 + 18, node_size)   # BTHeaderRec.nodeSize
    struct.pack_into(">I", node, 14 + 10, first_leaf)  # BTHeaderRec.firstLeafNode
    return bytes(node)


def _leaf_node(records, node_size=BLOCK_SIZE, f_link=0):
    node = bytearray(node_size)
    struct.pack_into(">I", node, 0, f_link)        # fLink
    struct.pack_into(">b", node, 8, -1)            # kind = leaf
    node[9] = 1                                     # height
    struct.pack_into(">H", node, 10, len(records))
    pos = 14
    offsets = [pos]
    for rec in records:
        node[pos:pos + len(rec)] = rec
        pos += len(rec)
        offsets.append(pos)
    for i, off in enumerate(offsets):
        struct.pack_into(">H", node, node_size - 2 * (i + 1), off)
    return bytes(node)


def _synth_image(path, n_blocks=64):
    """A complete little HFS+ volume: header + catalog with two files."""
    img = bytearray(BLOCK_SIZE * n_blocks)
    # catalog file occupies blocks 10 (header node) and 11 (leaf node).
    img[1024:1024 + 512] = _volume_header(
        n_blocks, catalog_ext=[(10, 2)], extents_ext=[])
    img[10 * BLOCK_SIZE:11 * BLOCK_SIZE] = _header_node()
    records = [
        _cat_record(1, "VOL", _folder_data(2)),               # root folder
        _cat_record(2, "docs", _folder_data(16)),             # /docs
        _cat_record(2, "a.bin", _file_data(17, [(20, 2)], 8000)),
        _cat_record(16, "nested.bin", _file_data(18, [(30, 1)], 3000)),
    ]
    img[11 * BLOCK_SIZE:12 * BLOCK_SIZE] = _leaf_node(records)
    img[20 * BLOCK_SIZE:22 * BLOCK_SIZE] = b"A" * (2 * BLOCK_SIZE)
    img[30 * BLOCK_SIZE:31 * BLOCK_SIZE] = b"N" * BLOCK_SIZE
    with open(path, "wb") as fh:
        fh.write(img)


# --- volume header --------------------------------------------------------
def test_volume_header_parse():
    vh = parse_volume_header(_volume_header(64, [(10, 2)], [(5, 1)]), volume_offset=0)
    assert vh is not None
    assert vh.block_size == BLOCK_SIZE
    assert vh.total_blocks == 64
    assert vh.catalog.extents == [(10, 2)]
    assert vh.extents_overflow.extents == [(5, 1)]
    assert vh.block_offset(10) == 10 * BLOCK_SIZE


def test_volume_header_rejects_bad_signature_and_truncation():
    bad = bytearray(_volume_header(64, [(10, 2)], []))
    struct.pack_into(">H", bad, 0, 0x1234)
    assert parse_volume_header(bytes(bad), 0) is None
    assert parse_volume_header(b"\x00" * 512, 0) is None
    assert parse_volume_header(b"H+", 0) is None       # too short


def test_volume_header_volume_offset_applied():
    vh = parse_volume_header(_volume_header(64, [(10, 2)], []), volume_offset=0x100000)
    assert vh.block_offset(0) == 0x100000


# --- B-tree ---------------------------------------------------------------
def test_btree_walks_leaf_records(tmp_path):
    img = str(tmp_path / "cat.img")
    with open(img, "wb") as fh:
        fh.write(_header_node() + _leaf_node([b"\x00\x02ab", b"\x00\x02cd"]))
    bt = BTree(img, [(0, 2 * BLOCK_SIZE)])
    assert bt.ok and bt.node_size == BLOCK_SIZE and bt.first_leaf == 1
    assert list(bt.iter_leaf_records()) == [b"\x00\x02ab", b"\x00\x02cd"]


def test_split_key_data():
    rec = _cat_record(2, "x", _folder_data(9))
    key, data = split_key_data(rec)
    assert struct.unpack_from(">I", key, 0)[0] == 2          # parentID
    assert key[6:8].decode("utf-16-be") == "x"
    assert struct.unpack_from(">h", data, 0)[0] == 1         # folder recordType


# --- extents overflow -----------------------------------------------------
def test_resolve_fork_inline_only():
    vh = parse_volume_header(_volume_header(64, [(10, 2)], []), 0)
    fork = ForkData(logical_size=8000, total_blocks=2, extents=[(20, 2)])
    ranges = extents.resolve_fork(fork, vh, 17, FORK_TYPE_DATA, ExtentsOverflow())
    assert ranges == [(20 * BLOCK_SIZE, 2 * BLOCK_SIZE)]


def test_resolve_fork_uses_overflow_when_inline_incomplete():
    vh = parse_volume_header(_volume_header(64, [(10, 2)], []), 0)
    # Inline covers 2 of 5 blocks; the rest come from the overflow index.
    key = struct.pack(">BBII", FORK_TYPE_DATA, 0, 17, 2)
    data = struct.pack(">II", 40, 3) + b"\x00" * 56
    overflow = ExtentsOverflow.from_records([(key, data)])
    fork = ForkData(logical_size=20000, total_blocks=5, extents=[(20, 2)])
    ranges = extents.resolve_fork(fork, vh, 17, FORK_TYPE_DATA, overflow)
    assert ranges == [(20 * BLOCK_SIZE, 2 * BLOCK_SIZE), (40 * BLOCK_SIZE, 3 * BLOCK_SIZE)]


def test_resolve_fork_coverage_flags_unmapped_tail():
    vh = parse_volume_header(_volume_header(64, [(10, 2)], []), 0)
    # Fork claims 5 blocks; inline covers 2 and the overflow index is empty
    # (its extents overflow file wasn't recovered) -> incompletely mapped.
    fork = ForkData(logical_size=20000, total_blocks=5, extents=[(20, 2)])
    res = extents.resolve_fork_coverage(fork, vh, 17, FORK_TYPE_DATA, ExtentsOverflow())
    assert res.mapped_blocks == 2 and res.total_blocks == 5
    assert res.fully_mapped is False
    assert res.ranges == [(20 * BLOCK_SIZE, 2 * BLOCK_SIZE)]

    # A fork fully covered by its inline extents is fully mapped.
    whole = ForkData(logical_size=8000, total_blocks=2, extents=[(20, 2)])
    assert extents.resolve_fork_coverage(
        whole, vh, 17, FORK_TYPE_DATA, ExtentsOverflow()).fully_mapped is True


def test_catalog_ranges_resolves_overflow_tail():
    # A catalog fragmented past its inline extents: total says 2 blocks, inline
    # covers 1, and the tail lives in the Extents Overflow File keyed by the
    # catalog's reserved file id (4). catalog_ranges must stitch it in.
    from app.hfsplus.extents import HFS_CATALOG_FILE_ID

    raw = bytearray(_volume_header(64, catalog_ext=[(10, 1)], extents_ext=[]))
    struct.pack_into(">I", raw, 0x110 + 12, 2)     # catalog fork totalBlocks = 2
    vh = parse_volume_header(bytes(raw), 0)

    overflow = ExtentsOverflow(by_fork={(FORK_TYPE_DATA, HFS_CATALOG_FILE_ID): [(11, 1)]})
    ranges = catalog.catalog_ranges("unused-image", vh, overflow)
    assert ranges == [(10 * BLOCK_SIZE, BLOCK_SIZE), (11 * BLOCK_SIZE, BLOCK_SIZE)]


def test_resolve_overflow_file_terminates_without_btree(tmp_path):
    # No valid overflow B-tree in the image -> bootstrap returns the inline
    # extents and stops (no infinite self-extension loop).
    img = str(tmp_path / "blank.img")
    with open(img, "wb") as fh:
        fh.write(b"\x00" * (BLOCK_SIZE * 64))
    vh = parse_volume_header(_volume_header(64, [(10, 1)], extents_ext=[(5, 1)]), 0)
    ranges, overflow = catalog.resolve_overflow_file(img, vh)
    assert ranges == [(5 * BLOCK_SIZE, BLOCK_SIZE)]
    assert overflow.by_fork == {}


# --- catalog (synthesised image) -----------------------------------------
def test_catalog_build_tree_synth(tmp_path):
    img = str(tmp_path / "hfs.img")
    _synth_image(img)
    tree = catalog.build_tree(img, 0)
    assert tree is not None and tree.root == 2
    by_name = {n.name: n for n in tree.nodes.values()}
    assert {"docs", "a.bin", "nested.bin"} <= set(by_name)
    # nested.bin hangs under docs, not the root.
    assert by_name["nested.bin"].parent_no == by_name["docs"].record_no
    # a.bin's data fork resolves to its on-disk extent.
    assert by_name["a.bin"].ranges == [(20 * BLOCK_SIZE, 2 * BLOCK_SIZE)]
    assert by_name["docs"].is_dir and not by_name["a.bin"].is_dir


def test_catalog_collect_filedata_and_compressed(tmp_path):
    img = str(tmp_path / "hfs.img")
    _synth_image(img)
    vh = catalog.load_volume(img, 0)
    ranges, n_files, n_skipped = catalog.collect_filedata_ranges(img, vh)
    assert n_files == 2 and n_skipped == 0
    assert (20 * BLOCK_SIZE, 2 * BLOCK_SIZE) in ranges
    assert (30 * BLOCK_SIZE, 1 * BLOCK_SIZE) in ranges


def test_catalog_skips_compressed_file(tmp_path):
    img = bytearray(BLOCK_SIZE * 64)
    img[1024:1024 + 512] = _volume_header(64, [(10, 2)], [])
    img[10 * BLOCK_SIZE:11 * BLOCK_SIZE] = _header_node()
    records = [
        _cat_record(1, "VOL", _folder_data(2)),
        _cat_record(2, "z.gz", _file_data(17, [], 5000, compressed=True)),
    ]
    img[11 * BLOCK_SIZE:12 * BLOCK_SIZE] = _leaf_node(records)
    path = str(tmp_path / "c.img")
    with open(path, "wb") as fh:
        fh.write(img)
    vh = catalog.load_volume(path, 0)
    ranges, n_files, n_skipped = catalog.collect_filedata_ranges(path, vh)
    assert n_files == 0 and n_skipped == 1 and ranges == []


# --- engine end-to-end on the synthesised image --------------------------
def test_engine_runs_hfsplus_plan_end_to_end(tmp_path):
    import shutil as _shutil

    from PySide6.QtCore import QObject, Signal

    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import Phase, RecoveryContext, TargetedRecovery

    src = str(tmp_path / "hfs.img")
    _synth_image(src)
    out = str(tmp_path / "out.img")

    class CopyingRunner(QObject):
        finished = Signal(int)

        def __init__(self):
            super().__init__()
            self.phases = 0

        def start(self, infile, outfile, logfile, settings):
            _shutil.copyfile(infile, outfile)
            self.phases += 1
            self.finished.emit(0)

        def take_unaligned_error(self):
            return False

    runner = CopyingRunner()
    rec = TargetedRecovery(runner)
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    selected = []
    rec.planSelected.connect(lambda steps: selected.append(steps))

    ctx = RecoveryContext(
        infile=src, outfile=out, logfile=str(tmp_path / "out.log"),
        workdir=str(tmp_path), settings=RescueSettings(sector_size=512),
        volume_offset=0, fs_type="hfsplus",
    )
    rec.start(ctx, include_filedata=True)

    assert results and results[0][0] is True
    assert rec._phase == Phase.DONE
    assert selected and (Phase.GET_CATALOG, "Catalog B-tree") in selected[0]
    # volheader, catalog, filedata = 3 imaging phases (no extents overflow here).
    assert runner.phases == 3


# --- end-to-end against a real HFS+ volume (needs mkfs.hfsplus) -----------
mkfs_hfsplus = shutil.which("mkfs.hfsplus") or (
    "/sbin/mkfs.hfsplus" if os.path.exists("/sbin/mkfs.hfsplus") else None)


def _make_hfsplus_image(path: str) -> None:
    with open(path, "wb") as fh:
        fh.truncate(64 * 1024 * 1024)
    subprocess.run([mkfs_hfsplus, "-v", "TEST", path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.skipif(mkfs_hfsplus is None, reason="mkfs.hfsplus not available")
def test_real_volume_detected_and_walked(tmp_path):
    from app.core import partition
    from app.core.volume import detect_filesystem

    img = str(tmp_path / "real.img")
    _make_hfsplus_image(img)

    head = open(img, "rb").read(0x440)
    assert partition.identify_filesystem(head) == "hfsplus"

    plan = detect_filesystem(img, 0)
    assert plan is not None and plan.name == "HFS+"

    vh = catalog.load_volume(img, 0)
    assert vh is not None and vh.block_size >= 512
    tree = catalog.build_tree(img, 0)
    assert tree is not None and tree.root == 2   # at least the root folder
