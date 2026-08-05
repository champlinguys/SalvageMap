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

"""BitLocker FVE metadata parser.

Reads the metadata a BitLocker boot record points to: the encryption method,
the volume GUID, the key protectors (VMK entries) and the wrapped FVEK. That is
everything the key flow needs:

    recovery password -> stretch key -> unwrap VMK -> unwrap FVEK -> decrypt

Structures follow the libbde layout. Everything here is *plaintext on disk* —
the FVE metadata is not itself encrypted — so it parses straight out of the
ddrescue image with no key.

Unlike the reference implementation this walks all three metadata copies and
takes the first that parses: on a partial rescue of a failing drive the primary
copy is often exactly what didn't come back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

FVE_SIGNATURE = b"-FVE-FS-"

# Offsets in the BitLocker boot record of the three FVE metadata block offsets.
_FVE_BLOCK_OFFSETS = (0xB0, 0xB8, 0xC0)
_METADATA_READ_BYTES = 64 * 1024   # generous: one block is normally 8 KiB

# FVE metadata entry types (libbde "metadata entry type").
ENTRY_VMK = 0x0002
ENTRY_FVEK = 0x0003
ENTRY_DESCRIPTION = 0x0007
ENTRY_VOLUME_HEADER = 0x000F

# FVE value types (libbde "metadata value type").
VALUE_UNICODE = 0x0002
VALUE_STRETCH_KEY = 0x0003
VALUE_AESCCM_KEY = 0x0005

# Encryption methods (the u16 at metadata header + 0x24).
AES_CBC_128_DIFFUSER = 0x8000
AES_CBC_256_DIFFUSER = 0x8001
AES_CBC_128 = 0x8002
AES_CBC_256 = 0x8003
AES_XTS_128 = 0x8004
AES_XTS_256 = 0x8005

METHOD_NAMES = {
    AES_CBC_128_DIFFUSER: "AES-CBC-128 + diffuser",
    AES_CBC_256_DIFFUSER: "AES-CBC-256 + diffuser",
    AES_CBC_128: "AES-CBC-128",
    AES_CBC_256: "AES-CBC-256",
    AES_XTS_128: "AES-XTS-128",
    AES_XTS_256: "AES-XTS-256",
}

# Key-protector types. 0x0800 (recovery password) is the one we can unlock with.
PROTECTION_RECOVERY_PASSWORD = 0x0800
PROTECTION_NAMES = {
    0x0000: "clear key",
    0x0100: "TPM",
    0x0200: "startup key",
    0x0500: "TPM+PIN",
    PROTECTION_RECOVERY_PASSWORD: "recovery password",
    0x2000: "passphrase",
}


def method_name(method: int) -> str:
    return METHOD_NAMES.get(method, f"unknown (0x{method:04X})")


def protection_name(raw: int) -> str:
    return PROTECTION_NAMES.get(raw, f"unknown (0x{raw:04X})")


@dataclass(frozen=True)
class AesCcmKey:
    """A key wrapped with AES-CCM: nonce, then MAC followed by ciphertext."""
    nonce: bytes          # 12 bytes
    mac_and_data: bytes   # 16-byte MAC + ciphertext


def guid_str(raw: bytes) -> str:
    """Format a 16-byte mixed-endian GUID the way Windows prints it."""
    if len(raw) != 16:
        return ""
    a = int.from_bytes(raw[0:4], "little")
    b = int.from_bytes(raw[4:6], "little")
    c = int.from_bytes(raw[6:8], "little")
    return (f"{a:08X}-{b:04X}-{c:04X}-{raw[8:10].hex().upper()}-"
            f"{raw[10:16].hex().upper()}")


@dataclass
class VmkProtector:
    """One Volume Master Key protector (one way of unlocking the volume)."""
    protection_type_raw: int = 0
    guid: bytes = b""                       # the "Identifier" on the key printout
    salt: bytes = b""                       # 16-byte stretch-key salt
    encrypted_vmk: AesCcmKey | None = None  # VMK wrapped by the stretch key

    @property
    def protection_type(self) -> str:
        return protection_name(self.protection_type_raw)

    @property
    def identifier(self) -> str:
        """The GUID Windows prints on the recovery-key file as "Identifier"."""
        return guid_str(self.guid)

    @property
    def is_recovery_password(self) -> bool:
        return self.protection_type_raw == PROTECTION_RECOVERY_PASSWORD

    @property
    def usable(self) -> bool:
        """Whether we have everything needed to try a recovery password here."""
        return (self.is_recovery_password and len(self.salt) == 16
                and self.encrypted_vmk is not None)


@dataclass
class FveMetadata:
    method: int = 0xFFFF
    volume_guid: bytes = b""
    description: str = ""
    vmks: list[VmkProtector] = field(default_factory=list)
    encrypted_fvek: AesCcmKey | None = None
    # BitLocker relocates the volume's first ``header_block_size`` bytes (the
    # original NTFS boot region) to ``header_block_offset``; decrypted reads of
    # the volume start must come from there.
    header_block_offset: int = 0
    header_block_size: int = 0
    # Size of the encrypted volume per the metadata *block* header. This lives
    # inside the volume, so unlike a partition table it can't be stale relative
    # to the volume it describes.
    encrypted_volume_size: int = 0

    @property
    def method_name(self) -> str:
        return method_name(self.method)

    @property
    def can_unlock_with_recovery_password(self) -> bool:
        return any(v.usable for v in self.vmks) and self.encrypted_fvek is not None

    @property
    def recovery_identifiers(self) -> list[str]:
        """GUIDs of the recovery-password protectors, to match the key printout."""
        return [v.identifier for v in self.vmks if v.usable]

    @property
    def volume_guid_str(self) -> str:
        return guid_str(self.volume_guid)


def looks_like_bitlocker(head: bytes) -> bool:
    """Whether ``head`` (a volume's first sector) is a BitLocker boot record."""
    return len(head) >= 11 and head[3:11] == FVE_SIGNATURE


def _u16(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 2], "little")


def _u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 4], "little")


def _u64(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 8], "little")


def _utf16le(data: bytes) -> str:
    text = data.decode("utf-16-le", errors="replace")
    return text.split("\x00", 1)[0]


def _parse_aes_ccm(value: bytes) -> AesCcmKey | None:
    """An AES-CCM encrypted-key value: 12-byte nonce, 16-byte MAC, ciphertext."""
    if len(value) < 12 + 16:
        return None
    return AesCcmKey(nonce=value[:12], mac_and_data=value[12:])


def _walk_entries(data: bytes):
    """Yield ``(entry_type, value_type, value_bytes)`` for each metadata entry."""
    off = 0
    end = len(data)
    while off + 8 <= end:
        size = _u16(data, off)
        entry_type = _u16(data, off + 2)
        value_type = _u16(data, off + 4)
        if size < 8 or off + size > end:
            break
        yield entry_type, value_type, data[off + 8:off + size]
        off += size


def _parse_vmk(value: bytes) -> VmkProtector:
    """GUID(16) + FILETIME(8) + u16 + u16 protection type, then nested entries."""
    vmk = VmkProtector()
    if len(value) < 0x1C:
        return vmk
    vmk.guid = value[0:16]
    vmk.protection_type_raw = _u16(value, 0x1A)
    for _etype, vtype, data in _walk_entries(value[0x1C:]):
        if vtype == VALUE_STRETCH_KEY and len(data) >= 4 + 16:
            # The stretch key carries only the derivation salt (u32 method +
            # salt). Its value also contains decoy nested AES-CCM entries; the
            # VMK-encrypting AES-CCM is a *sibling* of the stretch key, so we
            # deliberately do not descend into it here.
            vmk.salt = data[4:20]
        elif vtype == VALUE_AESCCM_KEY and vmk.encrypted_vmk is None:
            vmk.encrypted_vmk = _parse_aes_ccm(data)
    return vmk


def _parse_block(block: bytes) -> FveMetadata | None:
    """Parse one FVE metadata block (starting at its ``-FVE-FS-`` signature)."""
    if len(block) < 0x50 or block[:8] != FVE_SIGNATURE:
        return None
    # The metadata header sits at block + 0x40; entries follow header_size.
    header = block[0x40:]
    meta_size = _u32(header, 0x00)
    header_size = _u32(header, 0x08)
    if header_size < 0x30 or meta_size < header_size or 0x40 + meta_size > len(block):
        return None

    md = FveMetadata()
    md.encrypted_volume_size = _u64(block, 0x10)  # block header field, not +0x40
    md.volume_guid = header[0x10:0x20]
    md.method = _u16(header, 0x24)

    for entry_type, value_type, data in _walk_entries(
            header[header_size:meta_size]):
        if entry_type == ENTRY_DESCRIPTION and value_type == VALUE_UNICODE:
            md.description = _utf16le(data)
        elif entry_type == ENTRY_VMK:
            md.vmks.append(_parse_vmk(data))
        elif entry_type == ENTRY_FVEK and value_type == VALUE_AESCCM_KEY:
            md.encrypted_fvek = _parse_aes_ccm(data)
        elif entry_type == ENTRY_VOLUME_HEADER and len(data) >= 16:
            # u64 offset of the relocated original boot sectors + u64 size.
            md.header_block_offset = _u64(data, 0)
            md.header_block_size = _u64(data, 8)
    return md


def parse(read: Callable[[int, int], bytes]) -> FveMetadata | None:
    """Parse the FVE metadata of a volume, or None if it isn't BitLocker.

    ``read(offset, length)`` reads *volume-relative* bytes (ciphertext — the FVE
    metadata itself is not encrypted). Returns None if the boot record isn't a
    BitLocker one or if none of the three metadata copies is readable.
    """
    boot = read(0, 512)
    if not looks_like_bitlocker(boot):
        return None
    for pos in _FVE_BLOCK_OFFSETS:
        block_offset = _u64(boot, pos)
        if block_offset == 0:
            continue
        try:
            block = read(block_offset, _METADATA_READ_BYTES)
        except OSError:
            continue
        md = _parse_block(block)
        if md is not None:
            return md
    return None


def metadata_offsets(boot: bytes) -> list[int]:
    """The three FVE metadata block offsets recorded in a boot record."""
    if not looks_like_bitlocker(boot) or len(boot) < 0xC8:
        return []
    return [_u64(boot, pos) for pos in _FVE_BLOCK_OFFSETS if _u64(boot, pos)]
