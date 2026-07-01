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

"""Browseable recovered-file tree with per-node recovery status boxes.

Shows the NTFS directory hierarchy parsed from the recovered ``$MFT`` and, next
to each entry, a small box coloured by how much of that file/directory's on-disk
data the ddrescue mapfile has finished:

    clear        -> nothing recovered yet
    light green  -> partially recovered (in progress)
    dark green   -> fully recovered (or resident in the captured $MFT)
    amber        -> every mappable byte recovered, but the file's extent map is
                    incomplete (tail extents unresolved) — as complete as it can
                    get from current metadata, yet not the whole file
    red          -> tried but unreadable

The tree is populated lazily (children created on expand) so it stays responsive
on disks with hundreds of thousands of files.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QMenu, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator,
)

from app.core.mapfile import FinishedIndex
from app.ntfs.filetree import FileNode, FileTree
from app.ui.sector_map import STATUS_COLORS

_REC_ROLE = Qt.UserRole          # record number on each item
_POP_ROLE = Qt.UserRole + 1      # 1 once a dir's children are materialised

CLEAR, LIGHT, DARK, AMBER, BAD = "clear", "light", "dark", "amber", "bad"


def _make_box(color: QColor | None) -> QIcon:
    pix = QPixmap(13, 13)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setPen(QColor(120, 120, 128))
    p.setBrush(color if color is not None else QColor(0, 0, 0, 0))
    p.drawRect(1, 1, 10, 10)
    p.end()
    return QIcon(pix)


class FileTreePanel(QTreeWidget):
    """QTreeWidget rendering a :class:`FileTree` with recovery-status boxes."""

    # Emitted with a record number when the user asks to image a file/folder
    # first (right-click ▸ "Image this folder first").
    prioritizeRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # No "Size" column: NTFS only updates the $FILE_NAME size fields lazily,
        # so the recovered $MFT often reports 0 — better to omit it than mislead.
        self.setHeaderLabels(["Name"])
        self.setColumnWidth(0, 280)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(12, 12))
        self.setIndentation(14)
        # Override the global theme's tall item padding for a compact listing.
        self.setStyleSheet("QTreeView::item, QTreeWidget::item { padding: 0px 4px; }")
        self._tree: FileTree | None = None
        self._icons = {
            CLEAR: _make_box(None),
            LIGHT: _make_box(QColor(120, 215, 130)),
            DARK: _make_box(STATUS_COLORS["+"]),
            AMBER: _make_box(QColor(230, 170, 60)),  # mapped-incomplete -> amber
            BAD: _make_box(STATUS_COLORS["-"]),   # tried but unreadable -> red
        }
        self.itemExpanded.connect(self._on_expanded)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # Debounce live status refreshes (the mapfile polls every ~0.75 s).
        self._pending_mf = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(350)
        self._refresh_timer.timeout.connect(self._do_refresh)

    # --- data -------------------------------------------------------------
    @property
    def tree(self) -> FileTree | None:
        return self._tree

    def set_tree(self, tree: FileTree | None) -> None:
        self.clear()
        self._tree = tree
        if tree is None:
            return
        root = tree.nodes.get(tree.root)
        if root is None:
            return
        for child in tree.children_of(tree.root):
            self.addTopLevelItem(self._make_item(child))

    def _make_item(self, node: FileNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name])
        item.setData(0, _REC_ROLE, node.record_no)
        item.setData(0, _POP_ROLE, 0)
        item.setIcon(0, self._icons[CLEAR])
        if node.is_dir and self._tree and self._tree.nodes[node.record_no].children:
            # Placeholder so the expand arrow shows; replaced on first expand.
            item.addChild(QTreeWidgetItem([""]))
        return item

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _POP_ROLE) or self._tree is None:
            return
        item.takeChildren()  # drop the placeholder
        rec_no = item.data(0, _REC_ROLE)
        for child in self._tree.children_of(rec_no):
            item.addChild(self._make_item(child))
        item.setData(0, _POP_ROLE, 1)
        # Colour the freshly-created children now, against the last mapfile.
        # Without this they'd sit on the default clear box until the next
        # refresh — which never comes after an import (no live updates).
        self._do_refresh()

    # --- context menu -----------------------------------------------------
    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None or self._tree is None:
            return
        rec_no = item.data(0, _REC_ROLE)
        node = self._tree.nodes.get(rec_no)
        if node is None:
            return
        menu = QMenu(self)
        label = "Image this folder first" if node.is_dir else "Image this file first"
        act = menu.addAction(label)
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is act:
            self.prioritizeRequested.emit(rec_no)

    # --- status -----------------------------------------------------------
    def refresh_status(self, mf) -> None:
        """Queue a recolour against ``mf`` (debounced for live polling)."""
        self._pending_mf = mf
        if mf is None or self._tree is None:
            return
        self._refresh_timer.start()

    def _do_refresh(self) -> None:
        mf = self._pending_mf
        if self._tree is None or mf is None:
            return
        index = FinishedIndex.from_mapfile(mf)
        rollup = self._rollup(index)
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            rec_no = item.data(0, _REC_ROLE)
            node = self._tree.nodes.get(rec_no) if rec_no is not None else None
            if node is not None:
                if node.is_dir:
                    got, bad, total, incomplete = rollup.get(rec_no, (0, 0, 0, 0))
                    state = self._classify(got, bad, total, incomplete)
                else:
                    state = self._state_for(node, index)
                item.setIcon(0, self._icons[state])
            it += 1

    def incomplete_report(self, mf) -> tuple[list[tuple[int, int]], int, int]:
        """Ranges + counts for a final completeness pass against mapfile ``mf``.

        Returns ``(ranges, n_unfinished, n_unmapped)`` where ``ranges`` is the
        union of on-disk ranges of every file not yet fully recovered (so
        ddrescue can retry the unfinished/bad sectors), ``n_unfinished`` counts
        those files, and ``n_unmapped`` counts files whose extent map is
        incomplete (amber) — those also need the metadata re-imaged, which the
        caller folds in via :meth:`FilesystemPlan.metadata_ranges`.
        """
        ranges: list[tuple[int, int]] = []
        n_unfinished = n_unmapped = 0
        if self._tree is None or mf is None:
            return ranges, n_unfinished, n_unmapped
        index = FinishedIndex.from_mapfile(mf)
        for node in self._tree.nodes.values():
            if node.is_dir:
                continue
            if not node.fully_mapped:
                n_unmapped += 1
            if self._state_for(node, index) != DARK:
                n_unfinished += 1
                ranges += [(s, ln) for s, ln in node.ranges if ln > 0]
        return ranges, n_unfinished, n_unmapped

    def _rollup(self, index: FinishedIndex) -> dict[int, tuple[int, int, int, int]]:
        """Per-directory (finished, bad, total, incomplete) over its whole subtree.

        A folder's box should reflect everything inside it, not just its own
        index blocks — so we aggregate each node's own on-disk ranges up through
        its ancestors (post-order over the model). ``bad`` lets a folder show red
        when its content was tried but is unreadable; ``incomplete`` counts
        files whose extent map came up short so the folder can show amber rather
        than a misleading "complete".
        """
        nodes = self._tree.nodes
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

        # Pre-order from the root (cycle-guarded), then fold children into
        # parents in reverse for a correct post-order sum.
        order: list[int] = []
        seen: set[int] = set()
        stack = [self._tree.root]
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

    @staticmethod
    def _classify(got: int, bad: int, total: int, incomplete: int = 0) -> str:
        """Box state from finished/bad/total bytes of a node (or subtree).

        ``incomplete`` (files whose extent map fell short) caps the result below
        "complete": a node that would otherwise be dark green shows amber, since
        it can never actually hold the whole file(s).
        """
        if total <= 0:               # nothing on disk here -> lives in the $MFT
            return AMBER if incomplete else DARK
        if got >= total:
            return AMBER if incomplete else DARK
        if got > 0:
            return LIGHT             # some recovered (bad may also be present)
        if bad > 0:
            return BAD               # tried, unreadable — not just "not done"
        return CLEAR

    @classmethod
    def _state_for(cls, node: FileNode, index: FinishedIndex) -> str:
        incomplete = 0 if node.fully_mapped else 1
        if not node.ranges:           # resident content: in the captured $MFT
            return AMBER if incomplete else DARK
        got = bad = total = 0
        for start, length in node.ranges:
            if length > 0:
                total += length
                got += index.finished_bytes(start, length)
                bad += index.bad_bytes(start, length)
        return cls._classify(got, bad, total, incomplete)
