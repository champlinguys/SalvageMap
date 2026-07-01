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

"""Parse the NTFS boot sector (Volume Boot Record).

The VBR at the start of an NTFS volume holds the geometry we need to locate the
``$MFT``. Layout of the BIOS parameter block fields we read:

    0x03  u8[8]  OEM id ("NTFS    ")
    0x0B  u16    bytes per sector
    0x0D  u8     sectors per cluster (signed power-of-two code if > 0x80)
    0x28  u64    total sectors
    0x30  u64    $MFT starting cluster (LCN)
    0x38  u64    $MFTMirr starting cluster (LCN)
    0x40  i8     clusters per MFT record   (negative => 2**-v bytes)
    0x41  i8     clusters per index record (negative => 2**-v bytes)
    0x48  u64    volume serial number
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


def _signed_power_size(code: int, bytes_per_cluster: int) -> int:
    """Decode an NTFS "clusters per record" code.

    Positive: that many clusters. Negative (as signed byte): 2**(-code) bytes.
    """
    if code >= 0:
        return code * bytes_per_cluster
    return 1 << (-code)


@dataclass
class BootSector:
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_lcn: int
    mftmirr_lcn: int
    mft_record_size: int
    index_record_size: int
    volume_serial: int
    volume_offset: int = 0  # byte offset of this volume within the whole disk

    @property
    def bytes_per_cluster(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def mft_offset(self) -> int:
        """Absolute byte offset of the $MFT (incl. volume offset)."""
        return self.volume_offset + self.mft_lcn * self.bytes_per_cluster

    @property
    def mftmirr_offset(self) -> int:
        return self.volume_offset + self.mftmirr_lcn * self.bytes_per_cluster


def parse(data: bytes, volume_offset: int = 0) -> BootSector:
    """Parse a VBR from at least the first 512 bytes of a volume.

    Raises :class:`ValueError` if the structure is not a plausible NTFS VBR
    (important: on a half-recovered image this region may be zeros/garbage).
    """
    if len(data) < 0x50:
        raise ValueError("boot sector too short")

    oem = data[0x03:0x0B]
    bytes_per_sector = struct.unpack_from("<H", data, 0x0B)[0]
    spc_raw = struct.unpack_from("<b", data, 0x0D)[0]  # signed
    total_sectors = struct.unpack_from("<Q", data, 0x28)[0]
    mft_lcn = struct.unpack_from("<Q", data, 0x30)[0]
    mftmirr_lcn = struct.unpack_from("<Q", data, 0x38)[0]
    cpr_mft = struct.unpack_from("<b", data, 0x40)[0]
    cpr_idx = struct.unpack_from("<b", data, 0x41)[0]
    volume_serial = struct.unpack_from("<Q", data, 0x48)[0]

    # Validate geometry.
    if oem != b"NTFS    ":
        raise ValueError(f"not an NTFS volume (OEM={oem!r})")
    if bytes_per_sector < 256 or bytes_per_sector & (bytes_per_sector - 1):
        raise ValueError(f"bad bytes-per-sector {bytes_per_sector}")
    sectors_per_cluster = spc_raw if spc_raw > 0 else (1 << (-spc_raw))
    if sectors_per_cluster <= 0:
        raise ValueError("bad sectors-per-cluster")

    bytes_per_cluster = bytes_per_sector * sectors_per_cluster
    if mft_lcn == 0:
        raise ValueError("bad $MFT LCN")

    return BootSector(
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        total_sectors=total_sectors,
        mft_lcn=mft_lcn,
        mftmirr_lcn=mftmirr_lcn,
        mft_record_size=_signed_power_size(cpr_mft, bytes_per_cluster),
        index_record_size=_signed_power_size(cpr_idx, bytes_per_cluster),
        volume_serial=volume_serial,
        volume_offset=volume_offset,
    )
