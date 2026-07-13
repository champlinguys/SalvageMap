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

"""Per-node recovery status for a :class:`FileTree`, against a ddrescue mapfile.

The recovery state of a file/directory isn't stored on the node — it's computed
by comparing the node's absolute on-disk byte ranges against how much the
ddrescue mapfile has finished. This module is the single source of truth for
that classification, shared by the on-screen tree (:class:`FileTreePanel`) and
the customer-facing TXT/HTML exporters (:mod:`app.core.tree_export`).

The five internal states are:

    clear  -> nothing recovered yet
    light  -> partially recovered (in progress)
    dark   -> fully recovered (or resident in the captured metadata)
    amber  -> every mappable byte recovered, but the extent map is incomplete
    bad    -> tried but unreadable

For customer reporting these colours reflect *sectors imaged* and aren't
reliably trustworthy per-file, so :func:`is_recovered` makes only the
conservative claim: recovered iff fully green (``dark``); everything else counts
as not recovered.
"""

from __future__ import annotations

from app.core.mapfile import FinishedIndex
from app.ntfs.filetree import FileNode, FileTree

CLEAR, LIGHT, DARK, AMBER, BAD = "clear", "light", "dark", "amber", "bad"


def classify(got: int, bad: int, total: int, incomplete: int = 0) -> str:
    """Box state from finished/bad/total bytes of a node (or subtree).

    ``incomplete`` (files whose extent map fell short) caps the result below
    "complete": a node that would otherwise be dark green shows amber, since it
    can never actually hold the whole file(s).
    """
    if total <= 0:               # nothing on disk here -> lives in the metadata
        return AMBER if incomplete else DARK
    if got >= total:
        return AMBER if incomplete else DARK
    if got > 0:
        return LIGHT             # some recovered (bad may also be present)
    if bad > 0:
        return BAD               # tried, unreadable — not just "not done"
    return CLEAR


def node_state(node: FileNode, index: FinishedIndex) -> str:
    """Recovery state of a single file/directory node against ``index``."""
    incomplete = 0 if node.fully_mapped else 1
    if not node.ranges:           # resident content: in the captured metadata
        return AMBER if incomplete else DARK
    got = bad = total = 0
    for start, length in node.ranges:
        if length > 0:
            total += length
            got += index.finished_bytes(start, length)
            bad += index.bad_bytes(start, length)
    return classify(got, bad, total, incomplete)


def rollup(tree: FileTree, index: FinishedIndex) -> dict[int, tuple[int, int, int, int]]:
    """Per-directory (finished, bad, total, incomplete) over its whole subtree.

    A folder's box should reflect everything inside it, not just its own index
    blocks — so we aggregate each node's own on-disk ranges up through its
    ancestors (post-order over the model). ``bad`` lets a folder show red when
    its content was tried but is unreadable; ``incomplete`` counts files whose
    extent map came up short so the folder can show amber rather than a
    misleading "complete".
    """
    nodes = tree.nodes
    agg: dict[int, list[int]] = {}
    for rec, node in nodes.items():
        got = bad = total = 0
        for start, length in node.ranges:
            if length > 0:
                total += length
                got += index.finished_bytes(start, length)
                bad += index.bad_bytes(start, length)
        incomplete = 0 if node.is_dir or node.fully_mapped else 1
        agg[rec] = [got, bad, total, incomplete]

    # Pre-order from the root (cycle-guarded), then fold children into parents in
    # reverse for a correct post-order sum.
    order: list[int] = []
    seen: set[int] = set()
    stack = [tree.root]
    while stack:
        rec = stack.pop()
        if rec in seen:
            continue
        seen.add(rec)
        order.append(rec)
        node = nodes.get(rec)
        if node:
            stack.extend(c for c in node.children if c not in seen)
    for rec in reversed(order):
        node = nodes.get(rec)
        if not node:
            continue
        for child in node.children:
            if child in agg and child != rec:
                for k in range(4):
                    agg[rec][k] += agg[child][k]
    return {rec: tuple(v) for rec, v in agg.items()}


def is_recovered(state: str) -> bool:
    """True only for a fully-recovered node (conservative customer claim)."""
    return state == DARK


def customer_status(state: str) -> str:
    """Collapse the five internal states to the two customer buckets."""
    return "recovered" if state == DARK else "missing"
