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

"""BitLocker key flow and sector decryption.

    48-digit recovery password
      -> 16-byte binary  (each group / 11, little-endian u16)
      -> stretch key     (SHA-256 chained 0x100000 times over salt + counter)
      -> VMK             (AES-CCM unwrap, MAC-verified)
      -> FVEK            (AES-CCM unwrap, MAC-verified)
      -> AES-XTS / AES-CBC per-sector decryption

The two AES-CCM unwraps are authenticated, so a wrong recovery password fails
loudly (MAC mismatch) rather than yielding garbage — that MAC check is the real
proof an unlock succeeded, not whether the decrypted bytes "look right".

The Elephant-diffuser CBC variants (0x8000/0x8001, Vista/7-era) are recognised
but not implemented; :func:`method_supported` gates them so we never present a
garbage "decrypted" volume.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from app.bitlocker import fve

# BitLocker's crypto data unit. The reference implementation this is ported from
# hardcodes 512 and was validated that way; a volume whose BitLocker boot record
# declares a different sector size would need this revisited.
SECTOR = 512

_STRETCH_ITERATIONS = 0x100000
# Layout hashed by the stretch-key derivation (packed, little-endian):
#   updated(32) + initial(32) + salt(16) + count(8)
_STRETCH_BUF_SIZE = 88

# The AES-CCM plaintext of a wrapped key is a metadata entry followed by a key
# property: entry header (8) + key header (u16 type + u16 reserved). So the raw
# key material starts 12 bytes in.
_KEY_MATERIAL_OFFSET = 12

_SUPPORTED_METHODS = (
    fve.AES_CBC_128, fve.AES_CBC_256, fve.AES_XTS_128, fve.AES_XTS_256,
)

# FVEK bytes actually used per method (an FVEK entry is padded out beyond this).
_KEY_LENGTHS = {
    fve.AES_CBC_128: 16,
    fve.AES_CBC_256: 32,
    fve.AES_XTS_128: 32,   # key1||key2
    fve.AES_XTS_256: 64,   # key1||key2
}


class UnlockError(Exception):
    """The volume could not be unlocked with the given recovery password."""


@dataclass(frozen=True)
class VolumeKeys:
    """The unlocked key material needed to decrypt a volume."""
    fvek: bytes
    method: int

    @property
    def method_name(self) -> str:
        return fve.method_name(self.method)


def method_supported(method: int) -> bool:
    """Whether we can actually decrypt sectors for this encryption method.

    Parsing and unlocking succeed regardless of method (they only unwrap keys),
    so callers check this before presenting a decrypted volume.
    """
    return method in _SUPPORTED_METHODS


def parse_recovery_password(password: str) -> bytes | None:
    """Convert a 48-digit recovery password to its 16-byte binary form.

    Returns None if malformed: it must be 8 groups of 6 digits, each group
    divisible by 11 and under 2**16 after division (BitLocker's own checksum
    shape, so a mistyped key is usually caught here rather than by the MAC).
    """
    groups: list[int] = []
    current = ""

    def flush() -> bool:
        nonlocal current
        if len(current) != 6:
            return False
        value = int(current)
        if value % 11:
            return False
        value //= 11
        if value > 0xFFFF:
            return False
        groups.append(value)
        current = ""
        return True

    for ch in password.strip():
        if ch in "- ":
            if not flush():
                return None
        elif ch.isdigit() and ch.isascii():
            current += ch
        else:
            return None
    if not flush() or len(groups) != 8:
        return None
    return b"".join(g.to_bytes(2, "little") for g in groups)


def derive_stretch_key(recovery_binary: bytes, salt: bytes) -> bytes:
    """The 32-byte stretch key: SHA-256 chained 0x100000 times over the salt.

    This is deliberately slow (about a second of pure hashing) — it is
    BitLocker's brute-force defence, so it can't be optimised away. Callers
    should run it off the GUI thread or show a busy cursor.
    """
    if len(salt) != 16:
        raise ValueError("BitLocker stretch-key salt must be 16 bytes")
    buf = bytearray(_STRETCH_BUF_SIZE)
    buf[32:64] = hashlib.sha256(recovery_binary).digest()   # initial
    buf[64:80] = salt
    sha256 = hashlib.sha256
    for count in range(_STRETCH_ITERATIONS):
        buf[0:32] = sha256(buf).digest()                     # updated
        buf[80:88] = (count + 1).to_bytes(8, "little")       # count
    return bytes(buf[0:32])


def aes_ccm_decrypt(key: bytes, nonce: bytes, mac_and_data: bytes) -> bytes | None:
    """AES-CCM decrypt with MAC verification. None if the MAC fails.

    ``mac_and_data`` is BitLocker's on-disk order (16-byte MAC first); the AEAD
    API wants the tag last.
    """
    if len(mac_and_data) < 16:
        return None
    mac, ciphertext = mac_and_data[:16], mac_and_data[16:]
    try:
        return AESCCM(key, tag_length=16).decrypt(nonce, ciphertext + mac, None)
    except (InvalidTag, ValueError):
        return None


def _key_material(plaintext: bytes) -> bytes:
    return plaintext[_KEY_MATERIAL_OFFSET:] if len(plaintext) > _KEY_MATERIAL_OFFSET else b""


def unlock_with_recovery_password(md: fve.FveMetadata, password: str) -> VolumeKeys:
    """Unwrap the VMK then the FVEK using a 48-digit recovery password.

    Raises :class:`UnlockError` with a specific reason: a malformed password, no
    recovery-password protector on this volume, or a MAC failure (wrong key).
    """
    binary = parse_recovery_password(password)
    if binary is None:
        raise UnlockError(
            "That doesn't look like a BitLocker recovery key — it must be 8 "
            "groups of 6 digits, e.g. 123456-123456-…"
        )
    protectors = [v for v in md.vmks if v.usable]
    if not protectors:
        kinds = ", ".join(sorted({v.protection_type for v in md.vmks})) or "none"
        raise UnlockError(
            "This volume has no recovery-password protector, so a recovery key "
            f"cannot unlock it. Protectors present: {kinds}."
        )
    if md.encrypted_fvek is None:
        raise UnlockError("The FVE metadata holds no encrypted FVEK.")

    for protector in protectors:
        stretch = derive_stretch_key(binary, protector.salt)
        vmk_plain = aes_ccm_decrypt(
            stretch, protector.encrypted_vmk.nonce,
            protector.encrypted_vmk.mac_and_data)
        if vmk_plain is None:
            continue  # this protector isn't the one this key belongs to
        vmk = _key_material(vmk_plain)
        if len(vmk) < 32:
            continue
        fvek_plain = aes_ccm_decrypt(
            vmk[:32], md.encrypted_fvek.nonce, md.encrypted_fvek.mac_and_data)
        if fvek_plain is None:
            raise UnlockError(
                "The VMK unwrapped but the FVEK did not — the FVE metadata is "
                "damaged. Try another metadata copy or a further rescue pass."
            )
        return VolumeKeys(fvek=_key_material(fvek_plain), method=md.method)

    raise UnlockError(
        "Wrong recovery key for this volume (the key's MAC did not verify). "
        "Check the identifier on the recovery-key file matches this volume."
    )


# --- sector decryption ----------------------------------------------------
class SectorDecryptor:
    """Decrypts whole 512-byte sectors for one unlocked volume.

    ``data_unit`` is the *volume-relative* sector number (byte offset / 512) of
    the ciphertext's physical location — for AES-XTS it is the tweak, for AES-CBC
    it seeds the per-sector IV.
    """

    def __init__(self, keys: VolumeKeys):
        if not method_supported(keys.method):
            raise UnlockError(
                f"{keys.method_name} sectors cannot be decrypted yet "
                "(the Elephant-diffuser variants are not implemented)."
            )
        key_len = _KEY_LENGTHS[keys.method]
        if len(keys.fvek) < key_len:
            raise UnlockError(
                f"FVEK is {len(keys.fvek)} bytes, too short for {keys.method_name}."
            )
        self.method = keys.method
        self._key = keys.fvek[:key_len]
        self._xts = keys.method in (fve.AES_XTS_128, fve.AES_XTS_256)
        # AES-CBC derives each sector's IV by ECB-encrypting its byte offset;
        # one ECB context handles every IV in a batch (ECB is stateless).
        self._ecb_key = self._key if not self._xts else b""

    def decrypt(self, data: bytes, first_data_unit: int) -> bytes:
        """Decrypt a run of whole sectors starting at ``first_data_unit``."""
        if len(data) % SECTOR:
            raise ValueError("BitLocker decryption needs whole 512-byte sectors")
        if not data:
            return b""
        if self._xts:
            return self._decrypt_xts(data, first_data_unit)
        return self._decrypt_cbc(data, first_data_unit)

    # AES-XTS: the FVEK holds key1||key2 and the tweak is the little-endian
    # data-unit number. One data unit per sector, so one context per sector.
    def _decrypt_xts(self, data: bytes, first_data_unit: int) -> bytes:
        out = bytearray(len(data))
        for i in range(len(data) // SECTOR):
            tweak = (first_data_unit + i).to_bytes(16, "little")
            dec = Cipher(algorithms.AES(self._key), modes.XTS(tweak)).decryptor()
            out[i * SECTOR:(i + 1) * SECTOR] = dec.update(
                data[i * SECTOR:(i + 1) * SECTOR]) + dec.finalize()
        return bytes(out)

    # AES-CBC (no diffuser): IV = AES-ECB(FVEK) of the sector's byte offset in a
    # 16-byte little-endian block, then the sector is CBC-decrypted with it.
    #
    # Done as one ECB pass over the whole batch plus an XOR, rather than a CBC
    # context per sector: CBC decryption *is* ECB decryption XOR the previous
    # ciphertext block, and one bulk call over a megabyte beats 2048 small ones.
    def _decrypt_cbc(self, data: bytes, first_data_unit: int) -> bytes:
        n_sectors = len(data) // SECTOR
        iv_blocks = b"".join(
            ((first_data_unit + i) * SECTOR).to_bytes(8, "little") + b"\x00" * 8
            for i in range(n_sectors)
        )
        ecb_enc = Cipher(algorithms.AES(self._key), modes.ECB()).encryptor()
        ivs = ecb_enc.update(iv_blocks) + ecb_enc.finalize()

        ecb_dec = Cipher(algorithms.AES(self._key), modes.ECB()).decryptor()
        decrypted = ecb_dec.update(data) + ecb_dec.finalize()

        # "Previous ciphertext block" for every block: the sector's IV for its
        # first block, the preceding ciphertext block otherwise.
        previous = b"".join(
            ivs[i * 16:(i + 1) * 16] + data[i * SECTOR:(i + 1) * SECTOR - 16]
            for i in range(n_sectors)
        )
        return _xor(decrypted, previous)


def _xor(a: bytes, b: bytes) -> bytes:
    """XOR two equal-length buffers (via one big-int op — bytewise is far slower)."""
    n = len(a)
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")
