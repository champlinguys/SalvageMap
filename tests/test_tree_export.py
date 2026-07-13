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

import os

from app.core import tree_export, tree_status
from app.core.mapfile import Block, FinishedIndex, Mapfile
from app.ntfs.filetree import FileNode, FileTree


def build_tree() -> FileTree:
    """Root ▸ folder ▸ {good.txt (recovered), bad.txt (not)} + loose.txt (not)."""
    nodes = {
        5: FileNode(record_no=5, name="\\", is_dir=True, parent_no=5),
        6: FileNode(record_no=6, name="Photos", is_dir=True, parent_no=5),
        7: FileNode(record_no=7, name="good.txt", is_dir=False, parent_no=6,
                    ranges=[(0, 1000)]),
        8: FileNode(record_no=8, name="bad.txt", is_dir=False, parent_no=6,
                    ranges=[(2000, 1000)]),
        9: FileNode(record_no=9, name="loose.txt", is_dir=False, parent_no=5,
                    ranges=[(4000, 1000)]),
    }
    nodes[5].children = [6, 9]
    nodes[6].children = [7, 8]
    return FileTree(nodes=nodes, root=5)


def build_mapfile() -> Mapfile:
    """Only good.txt's range [0,1000) is finished; nothing else is."""
    mf = Mapfile()
    mf.blocks = [Block(0, 1000, "+"), Block(1000, 4000, "?")]
    return mf


def test_status_two_buckets():
    tree = build_tree()
    index = FinishedIndex.from_mapfile(build_mapfile())
    # good.txt fully finished -> recovered; the others -> missing.
    assert tree_status.customer_status(
        tree_status.node_state(tree.nodes[7], index)) == "recovered"
    assert tree_status.customer_status(
        tree_status.node_state(tree.nodes[8], index)) == "missing"
    assert tree_status.customer_status(
        tree_status.node_state(tree.nodes[9], index)) == "missing"
    # The folder isn't fully recovered (bad.txt missing) -> missing.
    roll = tree_status.rollup(tree, index)
    got, bad, total, incomplete = roll[6]
    assert tree_status.customer_status(
        tree_status.classify(got, bad, total, incomplete)) == "missing"


def build_tree_with_hidden() -> FileTree:
    nodes = {
        5: FileNode(record_no=5, name="\\", is_dir=True, parent_no=5),
        6: FileNode(record_no=6, name="photo.jpg", is_dir=False, parent_no=5,
                    ranges=[(0, 100)]),
        7: FileNode(record_no=7, name=".Spotlight-V100", is_dir=True, parent_no=5),
        8: FileNode(record_no=8, name="secret.db", is_dir=False, parent_no=7,
                    ranges=[(200, 100)]),
        9: FileNode(record_no=9, name=".DS_Store", is_dir=False, parent_no=5,
                    ranges=[(400, 100)]),
        10: FileNode(record_no=10, name="\x00\x00\x00\x00HFS+ Private Data",
                     is_dir=True, parent_no=5),
    }
    nodes[5].children = [6, 7, 9, 10]
    nodes[7].children = [8]
    return FileTree(nodes=nodes, root=5)


def test_is_hidden():
    assert tree_export.is_hidden(".DS_Store")
    assert tree_export.is_hidden(".Spotlight-V100")
    assert tree_export.is_hidden("._resourcefork")
    assert tree_export.is_hidden("\x00\x00\x00\x00HFS+ Private Data")
    assert not tree_export.is_hidden("photo.jpg")
    assert not tree_export.is_hidden("Photos")


def test_export_hides_system_entries(tmp_path):
    tree = build_tree_with_hidden()
    txt = os.path.join(tmp_path, "r.txt")
    n_ok, n_total = tree_export.export_txt(tree, None, txt)
    text = open(txt, encoding="utf-8").read()
    assert "photo.jpg" in text
    assert ".Spotlight-V100" not in text and "secret.db" not in text
    assert ".DS_Store" not in text and "HFS+ Private" not in text
    assert n_total == 1                          # only photo.jpg counted

    # And with hiding off, they come back.
    txt2 = os.path.join(tmp_path, "r2.txt")
    _, n_total2 = tree_export.export_txt(tree, None, txt2, hide_hidden=False)
    assert ".Spotlight-V100" in open(txt2, encoding="utf-8").read()
    assert n_total2 == 5

    # HTML honours the same flag.
    html_out = os.path.join(tmp_path, "r.html")
    tree_export.export_html(tree, None, html_out)
    html = open(html_out, encoding="utf-8").read()
    assert "photo.jpg" in html
    assert "Spotlight" not in html and "secret.db" not in html


def test_export_txt(tmp_path):
    tree = build_tree()
    out = os.path.join(tmp_path, "report.txt")
    n_ok, n_total = tree_export.export_txt(tree, build_mapfile(), out)
    text = open(out, encoding="utf-8").read()
    assert n_total == 4 and n_ok == 1          # good.txt only
    assert "Photos/" in text
    assert "good.txt  [Recovered]" in text
    assert "bad.txt  [Not recovered]" in text
    assert "├──" in text or "└──" in text      # tree connectors present


def test_export_txt_no_mapfile(tmp_path):
    """Without a mapfile everything reads as not recovered, no crash."""
    tree = build_tree()
    out = os.path.join(tmp_path, "report.txt")
    n_ok, n_total = tree_export.export_txt(tree, None, out)
    assert n_ok == 0 and n_total == 4
    assert "Recovered]" in open(out, encoding="utf-8").read()  # label present


def test_export_html_self_contained(tmp_path):
    tree = build_tree()
    out = os.path.join(tmp_path, "report.html")
    logo = "data:image/png;base64,AAAA"
    tree_export.export_html(tree, build_mapfile(), out, logo_data_uri=logo)
    html = open(out, encoding="utf-8").read()
    # No external resources referenced.
    assert "http://" not in html and "https://" not in html
    assert 'src="/' not in html
    # Names + statuses live in the embedded JSON model; filter controls and the
    # embedded logo are present in the markup.
    assert "good.txt" in html and "loose.txt" in html
    assert '"recovered"' in html and '"missing"' in html
    assert 'data-filter="recovered"' in html
    assert "var DATA =" in html                  # lazy-render model embedded
    assert logo in html


def test_export_html_lazy_not_full_dom(tmp_path):
    """Rows are built by JS from the model, not emitted as a giant static DOM."""
    tree = build_tree()
    out = os.path.join(tmp_path, "report.html")
    tree_export.export_html(tree, build_mapfile(), out)
    html = open(out, encoding="utf-8").read()
    # The tree container starts empty; no pre-rendered <li>/<details> rows.
    assert '<div id="tree"></div>' in html
    assert "<details>" not in html and "<li" not in html


def test_export_html_escapes_script_breakout(tmp_path):
    """A '<' in a filename must not be able to close the embedding script tag."""
    nodes = {
        5: FileNode(record_no=5, name="\\", is_dir=True, parent_no=5),
        6: FileNode(record_no=6, name="a<b>&.txt", is_dir=False, parent_no=5,
                    ranges=[(0, 10)]),
    }
    nodes[5].children = [6]
    tree = FileTree(nodes=nodes, root=5)
    out = os.path.join(tmp_path, "report.html")
    tree_export.export_html(tree, None, out)
    html = open(out, encoding="utf-8").read()
    assert "a\\u003cb>&.txt" in html             # '<' neutralised in the JSON
    assert "a<b>&.txt" not in html               # raw name never injected
