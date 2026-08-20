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

"""CoreStorage / FileVault 2 support: find the volume, unlock it, decrypt on read.

macOS wraps an HFS+ volume in a CoreStorage logical volume group and encrypts it
with FileVault 2. From SalvageMap's point of view this is the same shape of
problem as BitLocker — an encrypted volume is a *read transform*, not a
filesystem — so it plugs into the same seam: :mod:`app.core.decrypt` registers a
decrypt-on-read view of the image and the existing HFS+ parser sees plaintext
without knowing encryption is involved.

The split of labour differs from :mod:`app.bitlocker`, deliberately:

* :mod:`app.corestorage.cs` parses the CoreStorage volume header natively, so
  *detection* works on a partial image and needs nothing installed;
* :mod:`app.corestorage.keys` hands the actual key derivation and AES-XTS to
  ``libfvde`` (``python3-libfvde``). CoreStorage's key hierarchy — the wiped
  EncryptedRoot.plist, the passphrase-wrapped KEK, the KEK-wrapped volume key —
  is far more involved than BitLocker's, and libfvde's implementation is
  maintained and validated. Re-implementing it natively is deferred, not ruled
  out; the interface here is what a native backend would have to satisfy.

As always the drive is imaged first and nothing is decrypted *onto* disk: the
image keeps the ciphertext ddrescue read, and the password lives only in memory.
"""
