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

"""Customer-facing exports of the recovered-file tree (plain TXT + branded HTML).

Both walk a :class:`~app.ntfs.filetree.FileTree` in the same order the on-screen
browser uses (``children_of`` — directories first, then case-insensitive name)
and label each entry with its recovery status against a ddrescue mapfile. Status
comes from :mod:`app.core.tree_status`; per the reviewer's guidance we only make
the conservative claim (recovered iff fully green — everything else counts as
not recovered) since the underlying colours track sectors imaged, not files.

The HTML export is a single self-contained file: the tree is embedded as compact
JSON and rendered **lazily** in the browser (each folder's children are built
only when it is opened), so even a disk with hundreds of thousands of files
opens instantly instead of freezing the browser laying out the whole DOM.

By default both exports hide hidden / filesystem-internal clutter (dotfiles like
``.DS_Store`` / ``.Spotlight-V100`` and the HFS+ private data directories) so the
customer sees only their own files.
"""

from __future__ import annotations

import datetime
import json

from app.core.mapfile import FinishedIndex
from app.core import tree_status
from app.ntfs.filetree import FileNode, FileTree

RECOVERED_LABEL = "Recovered"
MISSING_LABEL = "Not recovered"


def is_hidden(name: str) -> bool:
    """True for hidden / filesystem-internal entries not worth showing a customer.

    Covers Unix/macOS dotfiles (``.DS_Store``, ``.Spotlight-V100``, ``.fseventsd``,
    ``.Trashes``, AppleDouble ``._*`` sidecars, …) and the HFS+ private data
    directories (``\\x00\\x00\\x00\\x00HFS+ Private Data``,
    ``.HFS+ Private Directory Data\\r``).
    """
    if name.startswith("."):
        return True
    return "hfs+ private" in name.lower()


def _index_for(mf) -> FinishedIndex | None:
    """FinishedIndex for ``mf`` — ``None`` when no mapfile is available yet."""
    return FinishedIndex.from_mapfile(mf) if mf is not None else None


def _state(node: FileNode, index: FinishedIndex | None,
           rollup: dict[int, tuple[int, int, int, int]]) -> str:
    """Recovery bucket (``"recovered"``/``"missing"``) for one node."""
    if index is None:
        return "missing"          # nothing imaged/polled yet -> not recovered
    if node.is_dir:
        got, bad, total, incomplete = rollup.get(node.record_no, (0, 0, 0, 0))
        state = tree_status.classify(got, bad, total, incomplete)
    else:
        state = tree_status.node_state(node, index)
    return tree_status.customer_status(state)


def _visible_children(tree: FileTree, rec_no: int, hide_hidden: bool) -> list[FileNode]:
    kids = tree.children_of(rec_no)
    if hide_hidden:
        kids = [n for n in kids if not is_hidden(n.name)]
    return kids


# --- plain text -----------------------------------------------------------

def export_txt(tree: FileTree, mf, out_path: str, *,
               hide_hidden: bool = True) -> tuple[int, int]:
    """Write an indented, human-readable tree to ``out_path``.

    Returns ``(n_recovered, n_total)`` counting entries (files + folders).
    """
    index = _index_for(mf)
    rollup = tree_status.rollup(tree, index) if index is not None else {}
    counts = [0, 0]  # [recovered, total]
    lines: list[str] = []

    def walk(rec_no: int, prefix: str) -> None:
        kids = _visible_children(tree, rec_no, hide_hidden)
        for i, node in enumerate(kids):
            last = i == len(kids) - 1
            connector = "└── " if last else "├── "
            bucket = _state(node, index, rollup)
            counts[1] += 1
            if bucket == "recovered":
                counts[0] += 1
            label = RECOVERED_LABEL if bucket == "recovered" else MISSING_LABEL
            name = node.name + ("/" if node.is_dir else "")
            lines.append(f"{prefix}{connector}{name}  [{label}]")
            if node.is_dir:
                walk(node.record_no, prefix + ("    " if last else "│   "))

    walk(tree.root, "")

    date = datetime.date.today().isoformat()
    header = [
        "Recovered files report",
        f"Generated: {date}",
        "",
        f"Legend:  [{RECOVERED_LABEL}] = file fully recovered   "
        f"[{MISSING_LABEL}] = not (fully) recovered",
        f"Summary: {counts[0]} of {counts[1]} entries recovered",
        "",
    ]
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(header + lines) + "\n")
    return counts[0], counts[1]


# --- self-contained HTML --------------------------------------------------

def _build_model(tree: FileTree, index, rollup, hide_hidden: bool):
    """Nested JSON-able model of the tree plus ``(n_recovered, n_total)``.

    Each node is ``{"n": name, "s": "recovered"|"missing"}`` with directories
    additionally carrying ``"d": 1`` and ``"c": [children]``. The browser builds
    DOM from this lazily.
    """
    counts = [0, 0]

    def node_obj(node: FileNode) -> dict:
        bucket = _state(node, index, rollup)
        counts[1] += 1
        if bucket == "recovered":
            counts[0] += 1
        obj: dict = {"n": node.name, "s": bucket}
        if node.is_dir:
            obj["d"] = 1
            obj["c"] = [node_obj(c)
                        for c in _visible_children(tree, node.record_no, hide_hidden)]
        return obj

    roots = [node_obj(c)
             for c in _visible_children(tree, tree.root, hide_hidden)]
    return roots, counts[0], counts[1]


def export_html(tree: FileTree, mf, out_path: str, *,
                logo_data_uri: str | None = None,
                report_date: str | None = None,
                hide_hidden: bool = True) -> tuple[int, int]:
    """Write a self-contained, dark-mode, browseable HTML report to ``out_path``.

    ``logo_data_uri`` — optional ``data:`` URI embedded in the header so the
    report needs no external assets. ``report_date`` defaults to today.
    Returns ``(n_recovered, n_total)``.
    """
    index = _index_for(mf)
    rollup = tree_status.rollup(tree, index) if index is not None else {}
    if report_date is None:
        report_date = datetime.date.today().isoformat()

    model, n_ok, n_total = _build_model(tree, index, rollup, hide_hidden)
    # Compact JSON, and neutralise "</script>" so a filename can't break out of
    # the embedding <script> tag (escaping "<" is sufficient for that).
    data_js = json.dumps(model, separators=(",", ":")).replace("<", "\\u003c")

    logo_html = (
        f'<img class="logo" src="{logo_data_uri}" alt="logo">'
        if logo_data_uri else ""
    )
    summary = f"{n_ok} of {n_total} entries recovered"
    doc = (
        _HTML_TEMPLATE
        .replace("__LOGO__", logo_html)
        .replace("__DATE__", _escape_text(report_date))
        .replace("__SUMMARY__", _escape_text(summary))
        .replace("__DATA__", data_js)
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return n_ok, n_total


def _escape_text(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recovered files report</title>
<style>
  :root {
    --bg: #1a1c20; --panel: #23262c; --fg: #e6e8eb; --muted: #9aa0a8;
    --line: #34383f; --green: #35c25a; --miss: #6b7078; --accent: #4a9eff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 18px 20px; background: var(--panel);
    border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5;
  }
  header .logo { max-height: 56px; max-width: 220px; border-radius: 6px; }
  header .titles { display: flex; flex-direction: column; }
  header h1 { font-size: 18px; margin: 0; }
  header .meta { color: var(--muted); font-size: 13px; }
  .controls {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
    padding: 12px 20px; background: var(--panel);
    border-bottom: 1px solid var(--line); position: sticky; top: 92px; z-index: 4;
  }
  .controls input[type=search] {
    flex: 1 1 180px; min-width: 140px; padding: 7px 10px;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--line); border-radius: 6px;
  }
  .controls button {
    padding: 7px 12px; background: var(--bg); color: var(--fg);
    border: 1px solid var(--line); border-radius: 6px; cursor: pointer;
  }
  .controls button.active { border-color: var(--accent); color: var(--accent); }
  #tree { padding: 12px 20px 40px; overflow-x: auto; }
  ul { list-style: none; margin: 0; padding-left: 18px; }
  #tree > ul { padding-left: 0; }
  li { margin: 1px 0; }
  summary { cursor: pointer; }
  summary::-webkit-details-marker { color: var(--muted); }
  .row { display: inline-flex; align-items: center; gap: 8px; padding: 2px 0; }
  li.file .row { padding-left: 15px; }
  .dot {
    width: 11px; height: 11px; border-radius: 3px; flex: 0 0 auto;
    border: 1px solid rgba(255,255,255,0.25);
  }
  .dot.recovered { background: var(--green); }
  .dot.missing { background: transparent; }
  .name { word-break: break-all; }
  .status { color: var(--muted); font-size: 12px; }
  .path { color: var(--muted); font-size: 12px; font-style: italic; }
  .results .path::before { content: "in "; }
  .note, .empty { color: var(--muted); padding: 8px 4px; }
  @media (max-width: 560px) {
    header, .controls { padding-left: 12px; padding-right: 12px; }
    .controls { top: 88px; }
    #tree { padding-left: 10px; padding-right: 10px; }
    ul { padding-left: 12px; }
    .status { display: none; }
  }
</style>
</head>
<body>
<header>
  __LOGO__
  <div class="titles">
    <h1>Recovered files report</h1>
    <span class="meta">__DATE__ &middot; __SUMMARY__</span>
  </div>
</header>
<div class="controls">
  <input type="search" id="q" placeholder="Search files…" autocomplete="off">
  <button data-filter="all" class="active">All</button>
  <button data-filter="recovered">Recovered</button>
  <button data-filter="missing">Not recovered</button>
</div>
<div id="tree"></div>
<script>
var DATA = __DATA__;
(function() {
  var LABEL = { recovered: "Recovered", missing: "Not recovered" };
  var RESULT_CAP = 2000;
  var container = document.getElementById('tree');
  var q = document.getElementById('q');
  var buttons = document.querySelectorAll('.controls button');
  var filter = 'all';
  var timer = null;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
    });
  }
  function rowHtml(node, extra) {
    return '<span class="row" data-status="' + node.s + '">'
      + '<span class="dot ' + node.s + '"></span>'
      + '<span class="name">' + esc(node.n) + (node.d ? '/' : '') + '</span>'
      + (extra || '')
      + '<span class="status">' + LABEL[node.s] + '</span></span>';
  }

  // --- lazy tree: build a folder's children only when it is first opened ---
  function buildList(nodes) {
    var ul = document.createElement('ul');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var li = document.createElement('li');
      if (node.d) {
        li.className = 'dir';
        var det = document.createElement('details');
        var sum = document.createElement('summary');
        sum.innerHTML = rowHtml(node);
        det.appendChild(sum);
        det._node = node;
        det.addEventListener('toggle', function() {
          if (this.open && !this._built) {
            this._built = true;
            if (this._node.c && this._node.c.length)
              this.appendChild(buildList(this._node.c));
          }
        });
        li.appendChild(det);
      } else {
        li.className = 'file';
        li.innerHTML = rowHtml(node);
      }
      ul.appendChild(li);
    }
    return ul;
  }

  var treeEl = buildList(DATA);
  container.appendChild(treeEl);

  function showTree() {
    container.textContent = '';
    container.appendChild(treeEl);
  }

  // --- search / filter: flat, capped result list (fast on huge trees) ------
  function renderResults(results, truncated) {
    container.textContent = '';
    if (!results.length) {
      var p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'No matching files.';
      container.appendChild(p);
      return;
    }
    var ul = document.createElement('ul');
    ul.className = 'results';
    results.forEach(function(r) {
      var li = document.createElement('li');
      li.className = 'file';
      var path = r.path ? '<span class="path">' + esc(r.path) + '</span>' : '';
      li.innerHTML = rowHtml(r.node, path);
      ul.appendChild(li);
    });
    container.appendChild(ul);
    if (truncated) {
      var note = document.createElement('p');
      note.className = 'note';
      note.textContent = 'Showing the first ' + results.length
        + ' matches — narrow your search to see more.';
      container.appendChild(note);
    }
  }

  function runFilter() {
    var term = q.value.trim().toLowerCase();
    if (!term && filter === 'all') { showTree(); return; }
    var results = [];
    var truncated = false;
    (function walk(nodes, path) {
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var okStatus = filter === 'all' || node.s === filter;
        var okTerm = !term || node.n.toLowerCase().indexOf(term) !== -1;
        if (okStatus && okTerm) {
          if (results.length >= RESULT_CAP) { truncated = true; return; }
          results.push({ node: node, path: path });
        }
        if (node.d && node.c) walk(node.c, path ? path + '/' + node.n : node.n);
      }
    })(DATA, '');
    renderResults(results, truncated);
  }

  function scheduleFilter() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(runFilter, 150);
  }

  q.addEventListener('input', scheduleFilter);
  buttons.forEach(function(b) {
    b.addEventListener('click', function() {
      buttons.forEach(function(x) { x.classList.remove('active'); });
      b.classList.add('active');
      filter = b.getAttribute('data-filter');
      runFilter();
    });
  });
})();
</script>
</body>
</html>
"""
