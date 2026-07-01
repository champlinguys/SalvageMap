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

"""Directory prioritization: subtree_ranges + the engine's run_ranges path."""

from app.ntfs.filetree import FileNode, FileTree, subtree_ranges


def _sample_tree() -> FileTree:
    nodes = {
        5: FileNode(5, "\\", True, 5, children=[10, 99]),
        10: FileNode(10, "docs", True, 5, children=[11, 12]),
        11: FileNode(11, "a.bin", False, 10, ranges=[(1000, 200)]),
        12: FileNode(12, "sub", True, 10, children=[13, 14]),
        13: FileNode(13, "n.bin", False, 12, ranges=[(2000, 50)]),
        14: FileNode(14, "tiny.txt", False, 12, resident=True),   # no on-disk data
        99: FileNode(99, "other.bin", False, 5, ranges=[(5000, 10)]),
    }
    return FileTree(nodes=nodes, root=5)


def test_subtree_ranges_collects_descendant_files():
    tree = _sample_tree()
    assert set(subtree_ranges(tree, 10)) == {(1000, 200), (2000, 50)}


def test_subtree_ranges_excludes_siblings_outside_subtree():
    tree = _sample_tree()
    assert (5000, 10) not in subtree_ranges(tree, 10)


def test_subtree_ranges_single_file():
    tree = _sample_tree()
    assert subtree_ranges(tree, 11) == [(1000, 200)]


def test_subtree_ranges_resident_only_is_empty():
    tree = _sample_tree()
    assert subtree_ranges(tree, 14) == []


# --- engine run_ranges ----------------------------------------------------
def test_run_ranges_images_selection_and_finishes(tmp_path):
    import shutil

    from PySide6.QtCore import QObject, Signal

    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import Phase, RecoveryContext, TargetedRecovery

    src = str(tmp_path / "src.img")
    with open(src, "wb") as fh:
        fh.write(b"\x00" * (8192))
    out = str(tmp_path / "out.img")

    class CopyingRunner(QObject):
        finished = Signal(int)

        def __init__(self):
            super().__init__()
            self.phases = 0
            self.last_settings = None

        def start(self, infile, outfile, logfile, settings):
            shutil.copyfile(infile, outfile)
            self.last_settings = settings
            self.phases += 1
            self.finished.emit(0)

        def take_unaligned_error(self):
            return False

    runner = CopyingRunner()
    rec = TargetedRecovery(runner)
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))
    sizes = []
    rec.domainSize.connect(sizes.append)

    ctx = RecoveryContext(
        infile=src, outfile=out, logfile=str(tmp_path / "out.log"),
        workdir=str(tmp_path), settings=RescueSettings(sector_size=512),
        volume_offset=0,
    )
    rec.run_ranges(ctx, [(1024, 512), (4096, 512)], "Imaged 'docs'.")

    assert results == [(True, "Imaged 'docs'.")]
    assert rec._phase == Phase.DONE
    assert runner.phases == 1
    assert sizes and sizes[0] == 1024          # two 512-byte ranges targeted


def test_run_ranges_empty_finishes_without_imaging(tmp_path):
    from PySide6.QtCore import QObject, Signal

    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import Phase, RecoveryContext, TargetedRecovery

    src = str(tmp_path / "src.img")
    with open(src, "wb") as fh:
        fh.write(b"\x00" * 4096)

    class CopyingRunner(QObject):
        finished = Signal(int)

        def __init__(self):
            super().__init__()
            self.phases = 0

        def start(self, *a):
            self.phases += 1
            self.finished.emit(0)

        def take_unaligned_error(self):
            return False

    runner = CopyingRunner()
    rec = TargetedRecovery(runner)
    results = []
    rec.finished.connect(lambda ok, msg: results.append((ok, msg)))

    ctx = RecoveryContext(
        infile=src, outfile=str(tmp_path / "out.img"),
        logfile=str(tmp_path / "out.log"), workdir=str(tmp_path),
        settings=RescueSettings(sector_size=512), volume_offset=0,
    )
    rec.run_ranges(ctx, [], "nothing")

    assert results and results[0][0] is True
    assert runner.phases == 0                   # nothing to image
    assert rec._phase == Phase.DONE
