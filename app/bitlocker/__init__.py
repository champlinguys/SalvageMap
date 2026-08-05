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

"""BitLocker (FVE) support: parse the metadata, unlock, decrypt on read.

A BitLocker volume is never decrypted *into* the image — the image keeps the
ciphertext exactly as ddrescue read it. Instead :mod:`app.bitlocker.source`
registers a decrypt-on-read view of the image, so every existing filesystem
parser (NTFS/ext/HFS+) sees plaintext without knowing encryption exists.

Ported from the C++ implementation in ``data-extractor-pro`` (``src/bitlocker/``),
which was validated byte-for-byte against a reference tool on a real volume.
"""
