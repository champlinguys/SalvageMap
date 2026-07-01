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

"""File-tree feature: $FILE_NAME parsing, tree build, status fraction + boxes."""

import shutil
import struct
import subprocess

import pytest

from app.core import mapfile
from app.core.mapfile import Block, FinishedIndex, Mapfile, range_finished_fraction
from app.ntfs import mft
from app.ntfs.filetree import build_file_tree, resolve_runs
from app.ntfs.runlist import Run


def _attr_list_bytes(entries):
    """Build a resident $ATTRIBUTE_LIST body from (type, start_vcn, record) tuples."""
    out = bytearray()
    for atype, vcn, rec in entries:
        e = bytearray(0x1A)
        struct.pack_into("<I", e, 0x00, atype)
        struct.pack_into("<H", e, 0x04, 0x1A)   # entry length (no name)
        e[0x07] = 0x1A                            # name offset
        struct.pack_into("<Q", e, 0x08, vcn)
        struct.pack_into("<Q", e, 0x10, rec)     # base file reference
        out += e
    return bytes(out)


def _data_attr(runs):
    a = mft.Attribute(mft.ATTR_DATA, True, "", 0)
    a.runs = runs
    return a


def _list_attr(entries):
    a = mft.Attribute(mft.ATTR_ATTRIBUTE_LIST, False, "", 0)
    a.resident_content = _attr_list_bytes(entries)
    return a


def test_ntfs_stitches_fragmented_data_across_extension_records():
    # Base record holds VCN 0; the fragmented tail (VCN 48) lives in extension
    # record 7, enumerated by the base's $ATTRIBUTE_LIST.
    base = mft.MftRecord(5, True, False, 0, [
        _list_attr([(mft.ATTR_DATA, 0, 5), (mft.ATTR_DATA, 48, 7)]),
        _data_attr([Run(48, 100)]),
    ])
    ext = mft.MftRecord(7, True, False, 5, [_data_attr([Run(48, 5000)])])

    runs, full = resolve_runs(base, {5: base, 7: ext}, mft.ATTR_DATA)
    assert full is True
    assert runs == [Run(48, 100), Run(48, 5000)]   # stitched in VCN order

    # Extension record not recovered -> known-incomplete, only the base fragment.
    runs2, full2 = resolve_runs(base, {5: base}, mft.ATTR_DATA)
    assert full2 is False
    assert runs2 == [Run(48, 100)]


def test_ntfs_single_record_is_fully_mapped():
    rec = mft.MftRecord(9, True, False, 0, [_data_attr([Run(10, 200)])])
    runs, full = resolve_runs(rec, {9: rec}, mft.ATTR_DATA)
    assert full is True and runs == [Run(10, 200)]


class _Boot:
    bytes_per_cluster = 4096
    volume_offset = 0


def _nonresident_list_attr(entries, lcn):
    """A non-resident $ATTRIBUTE_LIST whose body lives at cluster ``lcn``."""
    a = mft.Attribute(mft.ATTR_ATTRIBUTE_LIST, True, "", 0)
    a.runs = [Run(1, lcn)]
    body = _attr_list_bytes(entries)
    a.real_size = len(body)
    return a, body


def test_ntfs_reads_nonresident_attribute_list():
    entries = [(mft.ATTR_DATA, 0, 5), (mft.ATTR_DATA, 48, 7)]
    list_attr, body = _nonresident_list_attr(entries, lcn=200)
    base = mft.MftRecord(5, True, False, 0, [list_attr, _data_attr([Run(48, 100)])])
    ext = mft.MftRecord(7, True, False, 5, [_data_attr([Run(48, 5000)])])
    by_num = {5: base, 7: ext}

    # Reader returns the list body at cluster 200 (byte offset 200*4096).
    def read_volume(offset, length):
        return body if offset == 200 * 4096 else b"\x00" * length

    runs, full = resolve_runs(base, by_num, mft.ATTR_DATA, _Boot(), read_volume)
    assert full is True
    assert runs == [Run(48, 100), Run(48, 5000)]

    # No reader -> can't follow the non-resident list -> flagged incomplete.
    runs_nr, full_nr = resolve_runs(base, by_num, mft.ATTR_DATA)
    assert full_nr is False

    # Reader present but the list's clusters weren't imaged (all zeros) -> incomplete.
    runs_zero, full_zero = resolve_runs(
        base, by_num, mft.ATTR_DATA, _Boot(), lambda o, l: b"\x00" * l)
    assert full_zero is False


# --- $FILE_NAME parsing ---------------------------------------------------
def _fn_content(parent, name, namespace, real=1234, alloc=4096):
    raw = name.encode("utf-16-le")
    b = bytearray(0x42 + len(raw))
    struct.pack_into("<Q", b, 0x00, parent)
    struct.pack_into("<Q", b, 0x28, alloc)
    struct.pack_into("<Q", b, 0x30, real)
    b[0x40] = len(name)
    b[0x41] = namespace
    b[0x42:0x42 + len(raw)] = raw
    return bytes(b)


def test_parse_file_name_fields():
    fn = mft._parse_file_name(_fn_content(5, "report.txt", mft.NS_WIN32, real=999))
    assert fn is not None
    assert fn.parent_ref == 5
    assert fn.name == "report.txt"
    assert fn.namespace == mft.NS_WIN32
    assert fn.real_size == 999


def test_parse_file_name_too_short():
    assert mft._parse_file_name(b"\x00" * 8) is None


def test_best_file_name_prefers_win32_over_dos():
    rec = mft.MftRecord(record_number=42, in_use=True, is_directory=False)
    rec.attributes = [
        mft.Attribute(mft.ATTR_FILE_NAME, False, "", 0,
                      resident_content=_fn_content(5, "REPORT~1.TXT", mft.NS_DOS)),
        mft.Attribute(mft.ATTR_FILE_NAME, False, "", 0,
                      resident_content=_fn_content(5, "report.txt", mft.NS_WIN32)),
    ]
    best = rec.best_file_name()
    assert best.name == "report.txt"
    assert len(rec.file_names()) == 2


def test_extension_record_flagged():
    rec = mft.MftRecord(record_number=9, in_use=True, is_directory=False,
                        base_record=3)
    assert rec.is_extension


# --- mapfile range fraction ----------------------------------------------
def test_range_finished_fraction():
    mf = Mapfile(blocks=[Block(0, 1000, "+"), Block(1000, 500, "?"),
                         Block(1500, 500, "+")])
    idx = FinishedIndex.from_mapfile(mf)
    assert idx.fraction([(0, 1000)]) == 1.0
    assert idx.fraction([(1000, 500)]) == 0.0
    assert idx.fraction([(500, 1500)]) == pytest.approx(1000 / 1500)
    assert idx.fraction([]) is None                  # resident: no on-disk bytes
    assert range_finished_fraction(mf, [(0, 2000)]) == pytest.approx(0.75)


# --- tree build (needs mkntfs) -------------------------------------------
mkntfs = shutil.which("mkntfs")


@pytest.mark.skipif(mkntfs is None, reason="mkntfs not available")
def test_build_file_tree_from_image(tmp_path):
    from app.ntfs import targeted_recovery as tr
    img = str(tmp_path / "v.img")
    with open(img, "wb") as fh:
        fh.truncate(24 * 1024 * 1024)
    subprocess.run([mkntfs, "-F", "-Q", img], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    boot = tr.load_boot(img, 0)
    mft_bytes = tr.assemble_mft(img, tr.load_mft_ranges(img, boot))
    tree = build_file_tree(boot, mft_bytes)

    assert tree.root in tree.nodes
    names = {n.name for n in tree.nodes.values()}
    assert "$MFT" in names and "$Boot" in names
    # $MFT's data is the MFT extent itself -> it has on-disk ranges.
    mft_node = next(n for n in tree.nodes.values() if n.name == "$MFT")
    assert mft_node.ranges
    # Children hang off the root.
    assert tree.children_of(tree.root)


# --- status box classifier (needs Qt) ------------------------------------
def test_state_for_classifier():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from app.ntfs.filetree import FileNode
    from app.ui.file_tree_panel import CLEAR, DARK, LIGHT, FileTreePanel

    _ = QApplication.instance() or QApplication([])
    mf = Mapfile(blocks=[Block(0, 1000, "+"), Block(1000, 1000, "?")])
    idx = FinishedIndex.from_mapfile(mf)

    resident = FileNode(1, "a", False, 5, ranges=[], resident=True)
    full = FileNode(2, "b", False, 5, ranges=[(0, 1000)])
    none = FileNode(3, "c", False, 5, ranges=[(1000, 1000)])
    partial = FileNode(4, "d", False, 5, ranges=[(0, 500), (1000, 500)])

    assert FileTreePanel._state_for(resident, idx) == DARK
    assert FileTreePanel._state_for(full, idx) == DARK
    assert FileTreePanel._state_for(none, idx) == CLEAR
    assert FileTreePanel._state_for(partial, idx) == LIGHT


def test_classify_states():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from app.ui.file_tree_panel import BAD, CLEAR, DARK, LIGHT, FileTreePanel

    _ = QApplication.instance() or QApplication([])
    c = FileTreePanel._classify
    assert c(0, 0, 0) == DARK           # nothing on disk -> in $MFT
    assert c(1000, 0, 1000) == DARK     # fully finished
    assert c(400, 0, 1000) == LIGHT     # partial
    assert c(0, 1000, 1000) == BAD      # tried, unreadable
    assert c(0, 0, 1000) == CLEAR       # not tried yet
    assert c(400, 600, 1000) == LIGHT   # some good even amid bad


def test_expanded_children_coloured_immediately():
    """Regression: expanding a folder must colour its new children at once,
    not leave them on the default clear box until the next refresh (which never
    comes after a static import)."""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from app.ntfs.filetree import FileNode, FileTree
    from app.ui.file_tree_panel import DARK, _REC_ROLE, FileTreePanel

    _ = QApplication.instance() or QApplication([])
    nodes = {
        5: FileNode(5, "\\", True, 5, ranges=[], children=[10]),
        10: FileNode(10, "Docs", True, 5, ranges=[], children=[11]),
        11: FileNode(11, "movie.avi", False, 10, ranges=[(0, 4096)]),
    }
    panel = FileTreePanel()
    panel.set_tree(FileTree(nodes=nodes, root=5))
    panel._pending_mf = Mapfile(blocks=[Block(0, 4096, "+")])  # file is finished
    panel._do_refresh()

    folder = panel.topLevelItem(0)            # "Docs"
    panel.expandItem(folder)                  # creates + colours the child
    child = folder.child(0)
    assert child.data(0, _REC_ROLE) == 11
    assert child.icon(0).cacheKey() == panel._icons[DARK].cacheKey()


def test_directory_rollup_reflects_children():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from app.ntfs.filetree import FileNode, FileTree
    from app.ui.file_tree_panel import BAD, DARK, LIGHT, FileTreePanel

    _ = QApplication.instance() or QApplication([])
    nodes = {
        5: FileNode(5, "\\", True, 5, ranges=[], children=[10]),
        10: FileNode(10, "Docs", True, 5, ranges=[(0, 1000)], children=[11]),
        11: FileNode(11, "report.txt", False, 10, ranges=[(2000, 1000)]),
    }
    panel = FileTreePanel()
    panel.set_tree(FileTree(nodes=nodes, root=5))

    # Folder index recovered but the file inside is not -> folder is partial.
    idx = FinishedIndex.from_mapfile(
        Mapfile(blocks=[Block(0, 1000, "+"), Block(1000, 2000, "?")]))
    roll = panel._rollup(idx)
    assert roll[10] == (1000, 0, 2000, 0)   # (finished, bad, total, incomplete)
    assert panel._classify(*roll[10]) == LIGHT

    # File's sectors are bad on a finished run -> folder still partial (LIGHT),
    # but a fully-bad file shows red.
    idx_bad = FinishedIndex.from_mapfile(
        Mapfile(blocks=[Block(0, 1000, "+"), Block(2000, 1000, "-")]))
    assert panel._state_for(nodes[11], idx_bad) == BAD

    # Once the file is recovered too, the folder is complete.
    idx2 = FinishedIndex.from_mapfile(Mapfile(blocks=[Block(0, 3000, "+")]))
    assert panel._classify(*panel._rollup(idx2)[10]) == DARK


def test_incompletely_mapped_file_never_shows_complete():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from app.ntfs.filetree import FileNode, FileTree
    from app.ui.file_tree_panel import AMBER, DARK, FileTreePanel

    _ = QApplication.instance() or QApplication([])
    nodes = {
        5: FileNode(5, "\\", True, 5, ranges=[], children=[10, 12]),
        # A heavily-fragmented video whose tail extents couldn't be mapped.
        10: FileNode(10, "clip.mov", False, 5, ranges=[(0, 1000)],
                     fully_mapped=False),
        12: FileNode(12, "note.txt", False, 5, ranges=[(4000, 1000)]),
    }
    panel = FileTreePanel()
    panel.set_tree(FileTree(nodes=nodes, root=5))

    # Even with every mapped byte finished, an unmapped file caps at amber, and
    # the root folder containing it inherits amber rather than dark green.
    idx = FinishedIndex.from_mapfile(Mapfile(blocks=[Block(0, 5000, "+")]))
    assert panel._state_for(nodes[10], idx) == AMBER
    assert panel._state_for(nodes[12], idx) == DARK
    assert panel._classify(*panel._rollup(idx)[5]) == AMBER

    # The final-pass report lists the unmapped file and unions the retry ranges.
    ranges, n_unfinished, n_unmapped = panel.incomplete_report(
        Mapfile(blocks=[Block(0, 1000, "+"), Block(4000, 1000, "?")]))
    assert n_unmapped == 1                 # clip.mov
    assert n_unfinished == 2               # clip.mov (amber) + note.txt (unfinished)
    assert (4000, 1000) in ranges
