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

"""ext4 parser tests: synthesised structures + a real mke2fs image."""

import os
import shutil
import struct
import subprocess

import pytest

from app.ext import dirent, extents
from app.ext.superblock import EXT_MAGIC, parse as parse_superblock


# --- superblock (synthesised bytes) --------------------------------------
def _superblock_bytes(block_size=1024, inode_size=256, ipg=2048,
                      bpg=8192, inodes=2048, blocks=8192,
                      first_data_block=1, desc_size=64, incompat=0x2C2 | 0x80):
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0x00, inodes)
    struct.pack_into("<I", sb, 0x04, blocks)
    struct.pack_into("<I", sb, 0x14, first_data_block)
    log = (block_size // 1024).bit_length() - 1
    struct.pack_into("<I", sb, 0x18, log)
    struct.pack_into("<I", sb, 0x20, bpg)
    struct.pack_into("<I", sb, 0x28, ipg)
    struct.pack_into("<H", sb, 0x38, EXT_MAGIC)
    struct.pack_into("<I", sb, 0x4C, 1)            # rev_level dynamic
    struct.pack_into("<H", sb, 0x58, inode_size)
    struct.pack_into("<I", sb, 0x60, incompat)     # includes 64bit (0x80)
    struct.pack_into("<H", sb, 0xFE, desc_size)
    return bytes(sb)


def test_superblock_parse_geometry():
    sb = parse_superblock(_superblock_bytes(), volume_offset=0)
    assert sb is not None
    assert sb.block_size == 1024
    assert sb.inode_size == 256
    assert sb.inodes_per_group == 2048
    assert sb.n_groups == 1
    assert sb.desc_size == 64 and sb.incompat_64bit
    assert sb.gdt_block == 2
    assert sb.block_offset(2) == 2048


def test_superblock_rejects_bad_magic():
    bad = bytearray(_superblock_bytes())
    struct.pack_into("<H", bad, 0x38, 0x1234)
    assert parse_superblock(bytes(bad), 0) is None
    assert parse_superblock(b"\x00" * 1024, 0) is None


def test_superblock_volume_offset_applied():
    sb = parse_superblock(_superblock_bytes(), volume_offset=0x100000)
    assert sb.block_offset(0) == 0x100000


# --- extent tree (synthesised) -------------------------------------------
def _extent_leaf(*runs):
    """An extent-tree leaf header + (logical, length, phys_block) entries."""
    node = bytearray(12 + 12 * len(runs))
    struct.pack_into("<HHHH", node, 0, 0xF30A, len(runs), len(runs), 0)  # depth 0
    for i, (logical, length, phys) in enumerate(runs):
        base = 12 + i * 12
        struct.pack_into("<I", node, base, logical)
        struct.pack_into("<H", node, base + 4, length)
        struct.pack_into("<H", node, base + 6, phys >> 32)
        struct.pack_into("<I", node, base + 8, phys & 0xFFFFFFFF)
    return bytes(node)


class _SB:
    block_size = 1024
    volume_offset = 0
    def block_offset(self, b):
        return self.volume_offset + b * self.block_size


def test_extents_leaf_ranges():
    node = _extent_leaf((0, 2, 67), (2, 1, 100))
    ranges = extents.collect_ranges(node, _SB(), lambda _b: b"")
    assert ranges == [(67 * 1024, 2 * 1024), (100 * 1024, 1 * 1024)]


def test_extents_uninitialized_length_corrected():
    # ee_len > 32768 marks an uninitialized (still allocated) extent.
    node = _extent_leaf((0, 32768 + 3, 200))
    ranges = extents.collect_ranges(node, _SB(), lambda _b: b"")
    assert ranges == [(200 * 1024, 3 * 1024)]


def test_extents_bad_magic_empty():
    assert extents.collect_ranges(b"\x00" * 60, _SB(), lambda _b: b"") == []


def _extent_index(depth, *idxs):
    """An interior (depth>0) node with (logical, child_block) index entries."""
    node = bytearray(12 + 12 * len(idxs))
    struct.pack_into("<HHHH", node, 0, 0xF30A, len(idxs), len(idxs), depth)
    for i, (logical, child) in enumerate(idxs):
        base = 12 + i * 12
        struct.pack_into("<I", node, base, logical)
        struct.pack_into("<I", node, base + 4, child & 0xFFFFFFFF)
        struct.pack_into("<H", node, base + 8, child >> 32)
    return bytes(node)


def test_extents_coverage_complete_when_interior_imaged():
    leaf = _extent_leaf((0, 2, 67))
    root = _extent_index(1, (0, 500))          # points at child block 500
    ranges, complete = extents.collect_ranges_coverage(
        root, _SB(), lambda b: leaf if b == 500 else b"")
    assert complete is True
    assert ranges == [(67 * 1024, 2 * 1024)]


def test_extents_coverage_incomplete_when_interior_missing():
    root = _extent_index(1, (0, 500))
    # child block 500 reads back as zeros (not imaged) -> tail unresolved.
    ranges, complete = extents.collect_ranges_coverage(
        root, _SB(), lambda _b: b"\x00" * 1024)
    assert complete is False
    assert ranges == []


def test_inode_coverage_flags_indirect_block_file():
    from app.ext import catalog
    from app.ext.inode import Inode
    from app.ext.superblock import EXTENTS_FL, S_IFREG

    # Regular file, non-zero size, WITHOUT the extents flag => indirect blocks.
    indirect = Inode(number=12, mode=S_IFREG, size=4096, flags=0,
                     links_count=1, i_block=b"\x00" * 60)
    ranges, full = catalog._inode_coverage(indirect, _SB(), lambda _b: b"")
    assert ranges == [] and full is False       # flagged, not silently complete

    extent_file = Inode(number=13, mode=S_IFREG, size=2048, flags=EXTENTS_FL,
                        links_count=1, i_block=_extent_leaf((0, 2, 67)))
    ranges2, full2 = catalog._inode_coverage(extent_file, _SB(), lambda _b: b"")
    assert full2 is True and ranges2 == [(67 * 1024, 2 * 1024)]


# --- directory entries (synthesised) -------------------------------------
def _dir_block(entries, size=1024):
    """Build a directory data block from (inode, file_type, name) tuples."""
    blk = bytearray(size)
    pos = 0
    for i, (inode, ftype, name) in enumerate(entries):
        raw = name.encode()
        last = i == len(entries) - 1
        rec = size - pos if last else 8 + ((len(raw) + 3) & ~3)
        struct.pack_into("<I", blk, pos, inode)
        struct.pack_into("<H", blk, pos + 4, rec)
        blk[pos + 6] = len(raw)
        blk[pos + 7] = ftype
        blk[pos + 8:pos + 8 + len(raw)] = raw
        pos += rec
    return bytes(blk)


def test_dirent_iter_skips_dot_and_unused():
    block = _dir_block([
        (2, 2, "."), (2, 2, ".."), (13, 1, "big.bin"), (15, 2, "sub"),
    ])
    got = [(e.inode, e.is_dir, e.name) for e in dirent.iter_entries(block)]
    assert got == [(13, False, "big.bin"), (15, True, "sub")]


# --- end-to-end against a real ext4 image (needs mke2fs) -----------------
mke2fs = shutil.which("mke2fs")


def _make_ext4_image(path: str) -> None:
    root = os.path.join(os.path.dirname(path), "root")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    with open(os.path.join(root, "big.bin"), "wb") as fh:
        fh.write(os.urandom(200000))
    with open(os.path.join(root, "hello.txt"), "w") as fh:
        fh.write("hello world\n")
    with open(os.path.join(root, "sub", "nested.dat"), "wb") as fh:
        fh.write(os.urandom(5000))
    with open(path, "wb") as fh:
        fh.truncate(8 * 1024 * 1024)
    subprocess.run([mke2fs, "-F", "-q", "-t", "ext4", "-b", "1024", "-d", root, path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.skipif(mke2fs is None, reason="mke2fs not available")
def test_ext4_geometry_and_tree(tmp_path):
    from app.ext import catalog
    img = str(tmp_path / "ext4.img")
    _make_ext4_image(img)

    sb, gdt = catalog.load_geometry(img, 0)
    assert sb.block_size == 1024 and sb.inode_size == 256
    assert gdt, "should locate at least one inode table"

    tree = catalog.build_tree(img, 0)
    names = {n.name for n in tree.nodes.values()}
    assert {"big.bin", "hello.txt", "sub", "nested.dat"} <= names
    # The nested file hangs under "sub", not the root.
    sub = next(n for n in tree.nodes.values() if n.name == "sub")
    nested = next(n for n in tree.nodes.values() if n.name == "nested.dat")
    assert nested.parent_no == sub.record_no
    # big.bin is multi-block, so it has on-disk extents.
    big = next(n for n in tree.nodes.values() if n.name == "big.bin")
    assert big.ranges


@pytest.mark.skipif(mke2fs is None, reason="mke2fs not available")
def test_engine_runs_ext_plan_end_to_end(tmp_path):
    """The generic engine selects ExtPlan and walks every phase to completion."""
    import shutil as _shutil

    from PySide6.QtCore import QObject, Signal

    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import Phase, RecoveryContext, TargetedRecovery

    src = str(tmp_path / "ext4.img")
    _make_ext4_image(src)
    out = str(tmp_path / "out.img")

    class CopyingRunner(QObject):
        """Fake ddrescue: 'images' a phase by making the source fully available."""
        finished = Signal(int)

        def __init__(self):
            super().__init__()
            self.phases = 0

        def start(self, infile, outfile, logfile, settings):
            _shutil.copyfile(infile, outfile)  # all requested ranges now present
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
        workdir=str(tmp_path), settings=RescueSettings(sector_size=1024),
        volume_offset=0, fs_type="ext",
    )
    rec.start(ctx, include_filedata=True)

    assert results and results[0][0] is True
    assert rec._phase == Phase.DONE
    assert selected and (Phase.GET_INODES, "Inode tables") in selected[0]
    # superblock, gdt, inodes, dirs, filedata = 5 imaging phases.
    assert runner.phases == 5


@pytest.mark.skipif(mke2fs is None, reason="mke2fs not available")
def test_ext4_filedata_domain_bounded(tmp_path):
    from app.core.volume import detect_filesystem
    img = str(tmp_path / "ext4.img")
    _make_ext4_image(img)
    size = 8 * 1024 * 1024

    plan = detect_filesystem(img, 0)
    assert plan is not None and plan.name == "ext4"

    dmap = plan.filedata_domain(img, 0, size, 1024)
    assert dmap is not None
    covered = sum(b.size for b in dmap.blocks if b.status == "+")
    assert 0 < covered < size            # some allocated data, not the whole disk
    for a, b in zip(dmap.blocks, dmap.blocks[1:]):
        assert a.end == b.pos            # gapless + ordered
    assert dmap.blocks[-1].end == size
