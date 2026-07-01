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

"""Collect on-disk ranges and build a file tree from a recovered ext image.

These are the ext counterparts of the NTFS plan's ``collect_index_ranges`` /
``collect_filedata_ranges`` / ``build_file_tree``:

  * directory ranges  — every directory inode's data blocks (the ext analogue of
    NTFS ``$INDEX_ALLOCATION``); recovering them rebuilds the name tree,
  * file-data ranges  — every regular file's extents (allocated data only),
  * file tree         — walk the directory entries from the root inode for names.

We classify inodes by scanning the imaged inode tables (no need to walk the tree
first), exactly as the NTFS workflow scans the $MFT. The directory tree walk is
only needed to attach names, and runs after the directory blocks are imaged.

Everything reads the recovered image, never the failing device.
"""

from __future__ import annotations

from typing import Callable

from app.core.recovery import read_image
from app.ext import extents, inode as inode_mod
from app.ext.dirent import iter_entries
from app.ext.superblock import Superblock, parse as parse_superblock
from app.ntfs.filetree import FileNode, FileTree

ROOT_INODE = 2


def block_reader(image: str, sb: Superblock) -> Callable[[int], bytes]:
    """A ``read_block(block) -> bytes`` closure over the image."""
    def read_block(block: int) -> bytes:
        return read_image(image, sb.block_offset(block), sb.block_size)
    return read_block


def load_geometry(image: str, volume_offset: int) -> tuple[Superblock, list[int]] | None:
    """Parse the superblock + group descriptors from the image, or None."""
    sb = parse_superblock(
        read_image(image, volume_offset + 1024, 1024), volume_offset)
    if sb is None:
        return None
    gdt = inode_mod.read_gdt(image, sb)
    if not gdt:
        return None
    return sb, gdt


def _inode_ranges(node, sb, read_block) -> list[tuple[int, int]]:
    return _inode_coverage(node, sb, read_block)[0]


def _inode_coverage(node, sb, read_block) -> tuple[list[tuple[int, int]], bool]:
    """``(ranges, fully_mapped)`` for one inode's on-disk data.

    Indirect-block files (ext2/ext3, or ext4 without the extents flag) aren't
    resolved yet, so they are flagged *not* fully mapped rather than silently
    shown as complete. Extent files are fully mapped only if the whole extent
    tree could be walked (all interior nodes were imaged).
    """
    if node.size == 0 or node.inline_data or node.is_symlink:
        return [], True             # no on-disk data / resident content
    if not node.uses_extents:
        return [], False            # indirect-block file — unsupported, flag it
    return extents.collect_ranges_coverage(node.i_block, sb, read_block)


def collect_dir_ranges(image: str, sb: Superblock,
                       gdt: list[int]) -> tuple[list[tuple[int, int]], int]:
    """All directory data-block ranges and the count of directories."""
    read_block = block_reader(image, sb)
    ranges: list[tuple[int, int]] = []
    n_dirs = 0
    for node in inode_mod.iter_inodes(image, sb, gdt):
        if not node.is_dir:
            continue
        r = _inode_ranges(node, sb, read_block)
        if r:
            n_dirs += 1
            ranges += r
    return ranges, n_dirs


def collect_filedata_ranges(image: str, sb: Superblock,
                            gdt: list[int]) -> tuple[list[tuple[int, int]], int, int]:
    """All regular-file extent ranges, the file count, and #skipped (indirect).

    ext3/ext2 files that use indirect blocks (no extents) are out of scope for
    now: they are counted and reported, not imaged.
    """
    read_block = block_reader(image, sb)
    ranges: list[tuple[int, int]] = []
    n_files = 0
    n_skipped = 0
    for node in inode_mod.iter_inodes(image, sb, gdt):
        # Reserved inodes (1..first_ino-1) are filesystem internals — notably the
        # journal (inode 8), a large regular file with extents. Imaging them would
        # waste scarce read time on data that never appears in the tree.
        if node.number < sb.first_ino:
            continue
        if not node.is_regular or node.size == 0 or node.inline_data:
            continue
        if not node.uses_extents:
            n_skipped += 1   # indirect-block file (ext3/ext2) — unsupported for now
            continue
        r = extents.collect_ranges(node.i_block, sb, read_block)
        if r:
            n_files += 1
            ranges += r
    return ranges, n_files, n_skipped


def build_tree(image: str, volume_offset: int) -> FileTree | None:
    """Rebuild a browseable FileTree by walking directories from the root inode."""
    geom = load_geometry(image, volume_offset)
    if geom is None:
        return None
    sb, gdt = geom
    read_block = block_reader(image, sb)

    def read_inode(num):
        return inode_mod.read_inode(image, sb, gdt, num)

    root_inode = read_inode(ROOT_INODE)
    nodes: dict[int, FileNode] = {
        ROOT_INODE: FileNode(ROOT_INODE, "/", True, ROOT_INODE,
                             ranges=_inode_ranges(root_inode, sb, read_block)
                             if root_inode else [], resident=True)
    }
    visited = {ROOT_INODE}
    queue = [ROOT_INODE]
    while queue:
        dnum = queue.pop()
        dnode = read_inode(dnum)
        if dnode is None or not dnode.is_dir:
            continue
        for start, length in _inode_ranges(dnode, sb, read_block):
            data = read_image(image, start, length)
            for off in range(0, len(data), sb.block_size):
                for ent in iter_entries(data[off:off + sb.block_size]):
                    if ent.inode in nodes:
                        continue  # first link wins (handles hardlinks/loops)
                    child = read_inode(ent.inode)
                    if child is None:
                        continue
                    cranges, cfull = _inode_coverage(child, sb, read_block)
                    nodes[ent.inode] = FileNode(
                        record_no=ent.inode, name=ent.name, is_dir=child.is_dir,
                        parent_no=dnum, ranges=cranges,
                        resident=not cranges, size=child.size,
                        fully_mapped=cfull,
                    )
                    if child.is_dir and ent.inode not in visited:
                        visited.add(ent.inode)
                        queue.append(ent.inode)

    # Link children to parents (parallels build_file_tree).
    for num, node in nodes.items():
        if num == ROOT_INODE:
            continue
        parent = node.parent_no if node.parent_no in nodes else ROOT_INODE
        nodes[parent].children.append(num)

    return FileTree(nodes=nodes, root=ROOT_INODE)
