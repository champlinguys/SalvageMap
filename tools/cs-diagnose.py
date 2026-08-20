#!/usr/bin/env python3
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
"""Report why a CoreStorage recovery has no file data to image.

Usage:  sudo python3 tools/cs-diagnose.py /path/to/output.img
Prompts for the volume password (never echoed, never stored).
"""
import getpass
import os
import sys

# Run from a checkout without installing: the package lives one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import decrypt, volume as volmod          # noqa: E402
from app.corestorage import detect, keys, segments      # noqa: E402
from app.corestorage.source import CoreStorageSource    # noqa: E402
from app.hfsplus import catalog                         # noqa: E402
from app.hfsplus.btree import BTree                     # noqa: E402


def main(image):
    print(f"image: {image}  ({os.path.getsize(image):,} bytes apparent, "
          f"{os.stat(image).st_blocks * 512:,} on disk)")

    volumes = detect.find_volumes(image)
    if not volumes:
        print("FAIL: no CoreStorage volume found in this image.")
        print("      The partition table / volume start may not be imaged yet.")
        return 1
    vol = volumes[0]
    print(f"CoreStorage @0x{vol.offset:X}  {vol.description}  ({vol.source})")

    unlocked = keys.unlock_with_password(
        image, vol.offset, vol.size, getpass.getpass("volume password: "))
    mapping = segments.measure(unlocked, header=vol.header)
    print(f"unlocked: {unlocked.name!r}")
    print(f"mapping : trusted={mapping.trusted}  {mapping.summary}")
    if not mapping.trusted:
        print("FAIL: without a trusted mapping every range is imaged blind.")
        return 1
    decrypt.register(image, CoreStorageSource(unlocked, mapping, vol.header))

    print(f"locked?  {volmod.locked_volume_message(image, vol.offset) or 'no'}")
    plan = volmod.detect_filesystem(image, vol.offset)
    print(f"plan    : {plan.name if plan else 'NONE — volume header not imaged'}")

    vh = catalog.load_volume(image, vol.offset)
    if vh is None:
        print("FAIL: HFS+ volume header not readable — run Step 1.")
        return 1
    print(f"HFS+    : {vh.block_size} B/block, {vh.total_blocks:,} blocks")

    ranges = catalog.catalog_ranges(image, vh)
    print(f"catalog : {len(ranges)} extent(s), "
          f"{sum(n for _o, n in ranges):,} bytes")
    for offset, length in ranges[:4]:
        print(f"            disk 0x{offset:012X} + {length:,}")

    bt = BTree(image, ranges)
    print(f"B-tree  : ok={bt.ok} node_size={bt.node_size} "
          f"first_leaf={bt.first_leaf}")
    if not bt.ok:
        print("FAIL: the catalog B-tree is NOT in the image.")
        print("      This is what reports as 'no allocated file data found'.")
        print("      Re-run Step 1 so the catalog extents above get imaged.")
        return 1

    scan = catalog.scan_filedata(image, vh)
    total = sum(n for _o, n in scan.ranges)
    print(f"scan    : {scan.n_files:,} file(s) with data, "
          f"{scan.n_skipped:,} compressed, {len(scan.ranges):,} range(s), "
          f"{total / 2 ** 40:.2f} TiB")
    if not scan.ranges:
        print("FAIL: catalog reads fine but yields no data-fork extents.")
        return 1
    print("OK: this image has everything the file-data phase needs.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    try:
        raise SystemExit(main(sys.argv[1]))
    finally:
        decrypt.clear()
