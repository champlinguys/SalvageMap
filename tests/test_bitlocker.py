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

"""BitLocker parsing, unlocking and decrypt-on-read.

The fixtures here build a synthetic BitLocker volume — FVE metadata, a real
recovery-password protector, and sectors encrypted with an independent
implementation (OpenSSL via ``cryptography``, driven directly) — so the tests
check our parsing and decryption against the algorithm, not against themselves.
"""

import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from app.bitlocker import detect, fve, keys
from app.bitlocker.source import BitLockerSource
from app.core import decrypt, partition
from app.core.recovery import read_image

# A well-formed recovery password: every group is divisible by 11.
RECOVERY_PASSWORD = "-".join(f"{11 * (1000 + i):06d}" for i in range(8))
FVEK = bytes(range(32))
VMK = bytes(range(100, 132))
SALT = bytes(range(16, 32))
# Byte-palindromic so the mixed-endian formatting is still exercised, and
# obviously synthetic: never put a real volume's identifiers in a fixture.
VMK_GUID = bytes.fromhex("11111111222233334444555555555555")
VOLUME_GUID = bytes.fromhex("aaaaaaaabbbbccccddddeeeeeeeeeeee")

HEADER_BLOCK_SIZE = 8192
METADATA_OFFSET = 0x10000
HEADER_BLOCK_OFFSET = 0x40000
VOLUME_OFFSET = 1 << 20
VOLUME_SIZE = 1 << 21


# --- fixture construction -------------------------------------------------
def _entry(entry_type: int, value_type: int, value: bytes) -> bytes:
    return struct.pack("<HHHH", len(value) + 8, entry_type, value_type, 1) + value


def _ccm_wrap(key: bytes, nonce: bytes, key_material: bytes) -> bytes:
    """An AES-CCM key value as BitLocker stores it: nonce, MAC, ciphertext."""
    plaintext = b"\x00" * 12 + key_material   # entry header + key header
    ct_and_tag = AESCCM(key, tag_length=16).encrypt(nonce, plaintext, None)
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    return nonce + tag + ct


def _vmk_entry(stretch_key: bytes) -> bytes:
    nested = _entry(0x0000, fve.VALUE_STRETCH_KEY,
                    struct.pack("<I", 0x00000001) + SALT)
    nested += _entry(0x0000, fve.VALUE_AESCCM_KEY,
                     _ccm_wrap(stretch_key, b"\x01" * 12, VMK))
    value = VMK_GUID + b"\x00" * 8 + struct.pack("<HH", 1,
                                                 fve.PROTECTION_RECOVERY_PASSWORD)
    return _entry(fve.ENTRY_VMK, 0x0000, value + nested)


def build_metadata_block(stretch_key: bytes, method: int = fve.AES_CBC_256,
                         volume_size: int = VOLUME_SIZE) -> bytes:
    entries = _entry(fve.ENTRY_DESCRIPTION, fve.VALUE_UNICODE,
                     "Test Drive".encode("utf-16-le"))
    entries += _vmk_entry(stretch_key)
    entries += _entry(fve.ENTRY_FVEK, fve.VALUE_AESCCM_KEY,
                      _ccm_wrap(VMK, b"\x02" * 12, FVEK))
    entries += _entry(fve.ENTRY_VOLUME_HEADER, 0x000F,
                      struct.pack("<QQ", HEADER_BLOCK_OFFSET, HEADER_BLOCK_SIZE))

    header_size = 0x30
    header = bytearray(header_size)
    struct.pack_into("<I", header, 0x00, header_size + len(entries))  # metadata size
    struct.pack_into("<I", header, 0x08, header_size)
    header[0x10:0x20] = VOLUME_GUID
    struct.pack_into("<I", header, 0x24, method)

    block = bytearray(0x40 + header_size + len(entries))
    block[0:8] = fve.FVE_SIGNATURE
    struct.pack_into("<Q", block, 0x10, volume_size)
    block[0x40:] = bytes(header) + entries
    return bytes(block)


def build_boot_record() -> bytes:
    boot = bytearray(512)
    boot[0:3] = b"\xeb\x58\x90"
    boot[3:11] = fve.FVE_SIGNATURE
    for i, pos in enumerate((0xB0, 0xB8, 0xC0)):
        # Two decoy copies that aren't in the image, then the real one: the
        # parser must fall through to whichever copy actually reads back.
        struct.pack_into("<Q", boot, pos, 0 if i < 2 else METADATA_OFFSET)
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


def cbc_encrypt_sector(key: bytes, byte_offset: int, plain: bytes) -> bytes:
    """Reference AES-CBC BitLocker sector encryption (IV = ECB(offset block))."""
    iv_block = struct.pack("<Q", byte_offset) + b"\x00" * 8
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    iv = enc.update(iv_block) + enc.finalize()
    cbc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return cbc.update(plain) + cbc.finalize()


def xts_encrypt_sector(key: bytes, data_unit: int, plain: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key),
                 modes.XTS(data_unit.to_bytes(16, "little"))).encryptor()
    return enc.update(plain) + enc.finalize()


@pytest.fixture(scope="module")
def stretch_key() -> bytes:
    """The real derivation (about a second — it's meant to be slow)."""
    binary = keys.parse_recovery_password(RECOVERY_PASSWORD)
    return keys.derive_stretch_key(binary, SALT)


@pytest.fixture(scope="module")
def metadata(stretch_key) -> fve.FveMetadata:
    block = build_metadata_block(stretch_key)
    boot = build_boot_record()

    def read(offset, length):
        if offset == 0:
            return boot[:length]
        if offset == METADATA_OFFSET:
            return block[:length]
        return b""

    md = fve.parse(read)
    assert md is not None
    return md


# --- recovery password ----------------------------------------------------
def test_parse_recovery_password_roundtrip():
    binary = keys.parse_recovery_password(RECOVERY_PASSWORD)
    assert binary is not None and len(binary) == 16
    assert int.from_bytes(binary[0:2], "little") == 1000


@pytest.mark.parametrize("bad", [
    "",
    "123456-123456-123456-123456-123456-123456-123456",      # only 7 groups
    "011000-011000-011000-011000-011000-011000-011000-01100",  # short group
    "123457-011000-011000-011000-011000-011000-011000-011000",  # not /11
    "01100a-011000-011000-011000-011000-011000-011000-011000",  # non-digit
])
def test_parse_recovery_password_rejects(bad):
    assert keys.parse_recovery_password(bad) is None


# --- FVE metadata ---------------------------------------------------------
def test_parse_metadata_fields(metadata):
    assert metadata.method == fve.AES_CBC_256
    assert metadata.method_name == "AES-CBC-256"
    assert metadata.description == "Test Drive"
    assert metadata.header_block_offset == HEADER_BLOCK_OFFSET
    assert metadata.header_block_size == HEADER_BLOCK_SIZE
    assert metadata.encrypted_volume_size == VOLUME_SIZE
    assert metadata.can_unlock_with_recovery_password


def test_parse_metadata_identifier(metadata):
    # The GUID Windows prints on the recovery-key file, mixed-endian.
    assert metadata.recovery_identifiers == ["11111111-2222-3333-4444-555555555555"]
    assert metadata.volume_guid_str == "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


def test_parse_rejects_non_bitlocker():
    assert fve.parse(lambda o, n: b"\x00" * n) is None


def test_looks_like_bitlocker():
    assert fve.looks_like_bitlocker(build_boot_record())
    assert not fve.looks_like_bitlocker(b"\x00" * 512)
    assert not fve.looks_like_bitlocker(b"")


# --- unlocking ------------------------------------------------------------
def test_unlock_with_correct_key(metadata):
    unlocked = keys.unlock_with_recovery_password(metadata, RECOVERY_PASSWORD)
    assert unlocked.fvek == FVEK
    assert unlocked.method == fve.AES_CBC_256


def test_unlock_with_wrong_key_fails(metadata, monkeypatch):
    # Any other 16 bytes derive a different stretch key, so the MAC must fail.
    monkeypatch.setattr(keys, "derive_stretch_key", lambda binary, salt: b"\x07" * 32)
    with pytest.raises(keys.UnlockError, match="Wrong recovery key"):
        keys.unlock_with_recovery_password(metadata, RECOVERY_PASSWORD)


def test_unlock_with_malformed_key(metadata):
    with pytest.raises(keys.UnlockError, match="8 groups of 6 digits"):
        keys.unlock_with_recovery_password(metadata, "not-a-key")


def test_unlock_without_recovery_protector(metadata):
    passphrase_only = fve.FveMetadata(
        method=fve.AES_CBC_256,
        vmks=[fve.VmkProtector(protection_type_raw=0x2000)],
    )
    with pytest.raises(keys.UnlockError, match="no recovery-password protector"):
        keys.unlock_with_recovery_password(passphrase_only, RECOVERY_PASSWORD)


# --- sector decryption ----------------------------------------------------
def test_decrypt_cbc_matches_reference():
    plain = bytes(range(256)) * 4          # 2 sectors
    cipher = (cbc_encrypt_sector(FVEK, 40 * 512, plain[:512])
              + cbc_encrypt_sector(FVEK, 41 * 512, plain[512:]))
    dec = keys.SectorDecryptor(keys.VolumeKeys(FVEK, fve.AES_CBC_256))
    assert dec.decrypt(cipher, 40) == plain


def test_decrypt_xts_matches_reference():
    key = bytes(range(16)) + bytes(range(100, 116))   # key1||key2, 16 bytes each
    plain = bytes(range(256)) * 4
    cipher = (xts_encrypt_sector(key, 7, plain[:512])
              + xts_encrypt_sector(key, 8, plain[512:]))
    dec = keys.SectorDecryptor(keys.VolumeKeys(key, fve.AES_XTS_128))
    assert dec.decrypt(cipher, 7) == plain


def test_decrypt_rejects_partial_sector():
    dec = keys.SectorDecryptor(keys.VolumeKeys(FVEK, fve.AES_CBC_256))
    with pytest.raises(ValueError):
        dec.decrypt(b"\x00" * 500, 0)


def test_diffuser_methods_are_refused():
    assert not keys.method_supported(fve.AES_CBC_128_DIFFUSER)
    with pytest.raises(keys.UnlockError, match="not implemented"):
        keys.SectorDecryptor(keys.VolumeKeys(FVEK, fve.AES_CBC_256_DIFFUSER))


def test_short_fvek_is_refused():
    with pytest.raises(keys.UnlockError, match="too short"):
        keys.SectorDecryptor(keys.VolumeKeys(b"\x00" * 16, fve.AES_CBC_256))


# --- decrypt-on-read source ----------------------------------------------
def make_source(**kwargs) -> BitLockerSource:
    params = dict(volume_offset=VOLUME_OFFSET, volume_size=VOLUME_SIZE,
                  keys=keys.VolumeKeys(FVEK, fve.AES_CBC_256),
                  header_block_offset=HEADER_BLOCK_OFFSET,
                  header_block_size=HEADER_BLOCK_SIZE)
    params.update(kwargs)
    return BitLockerSource(**params)


def test_physical_ranges_relocates_the_boot_region():
    src = make_source()
    # The volume's first bytes physically live in the relocated header block.
    assert src.physical_ranges([(VOLUME_OFFSET, 4096)]) == [
        (VOLUME_OFFSET + HEADER_BLOCK_OFFSET, 4096)]


def test_physical_ranges_split_at_the_header_boundary():
    src = make_source()
    got = src.physical_ranges([(VOLUME_OFFSET, HEADER_BLOCK_SIZE + 512)])
    assert got == [
        (VOLUME_OFFSET + HEADER_BLOCK_OFFSET, HEADER_BLOCK_SIZE),
        (VOLUME_OFFSET + HEADER_BLOCK_SIZE, 512),
    ]


def test_physical_ranges_are_identity_for_file_data():
    src = make_source()
    # Everything past the header block maps 1:1 — which is why prioritised
    # imaging of a folder targets exactly the sectors the file tree named.
    start = VOLUME_OFFSET + (1 << 20)
    assert src.physical_ranges([(start, 4096)]) == [(start, 4096)]


def test_physical_ranges_pass_through_outside_the_volume():
    src = make_source()
    assert src.physical_ranges([(0, 512)]) == [(0, 512)]
    assert src.physical_ranges([(VOLUME_OFFSET + VOLUME_SIZE, 512)]) == [
        (VOLUME_OFFSET + VOLUME_SIZE, 512)]


def build_encrypted_image(tmp_path, body_plain: bytes, boot_plain: bytes):
    """An image with a partition table region, a relocated boot block and body."""
    size = VOLUME_OFFSET + VOLUME_SIZE
    image = bytearray(size)
    image[0:512] = b"\xAA" * 512            # untouched, outside the volume
    body_offset = VOLUME_OFFSET + HEADER_BLOCK_SIZE
    image[body_offset:body_offset + len(body_plain)] = cbc_encrypt_sector(
        FVEK, HEADER_BLOCK_SIZE, body_plain)
    hdr = VOLUME_OFFSET + HEADER_BLOCK_OFFSET
    image[hdr:hdr + len(boot_plain)] = cbc_encrypt_sector(
        FVEK, HEADER_BLOCK_OFFSET, boot_plain)
    path = tmp_path / "encrypted.img"
    path.write_bytes(bytes(image))
    return str(path)


def test_source_reads_plaintext(tmp_path):
    boot_plain = bytes(range(256)) * 2
    body_plain = bytes(range(255, -1, -1)) * 2
    path = build_encrypted_image(tmp_path, body_plain, boot_plain)
    src = make_source()

    def raw(offset, length):
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    # Volume start comes from the relocated header block…
    assert src.read(raw, VOLUME_OFFSET, 512) == boot_plain
    # …the body from its own offset…
    assert src.read(raw, VOLUME_OFFSET + HEADER_BLOCK_SIZE, 512) == body_plain
    # …and a mid-sector read still lands on the right bytes.
    assert src.read(raw, VOLUME_OFFSET + 100, 50) == boot_plain[100:150]
    # Outside the volume nothing is touched.
    assert src.read(raw, 0, 512) == b"\xAA" * 512


def test_source_zero_fills_holes(tmp_path):
    path = build_encrypted_image(tmp_path, b"", b"")
    src = make_source()
    raw = lambda o, n: b""      # noqa: E731 — an entirely unrescued image
    assert len(src.read(raw, VOLUME_OFFSET, 4096)) == 4096
    assert src.read(raw, 0, 512) == b"\x00" * 512


# --- registry wiring ------------------------------------------------------
def test_read_image_decrypts_registered_images(tmp_path):
    boot_plain = bytes(range(256)) * 2
    path = build_encrypted_image(tmp_path, b"", boot_plain)
    assert read_image(path, VOLUME_OFFSET, 512) != boot_plain   # ciphertext
    decrypt.register(path, make_source())
    try:
        assert read_image(path, VOLUME_OFFSET, 512) == boot_plain
        assert decrypt.physical_ranges(path, [(VOLUME_OFFSET, 512)]) == [
            (VOLUME_OFFSET + HEADER_BLOCK_OFFSET, 512)]
    finally:
        decrypt.unregister(path)
    assert decrypt.source_for(path) is None
    # Unregistered images are untouched, ranges included.
    assert decrypt.physical_ranges(path, [(VOLUME_OFFSET, 512)]) == [
        (VOLUME_OFFSET, 512)]


# --- prioritised imaging through the engine ------------------------------
def test_run_ranges_images_the_physical_sectors(tmp_path):
    """"Image this folder first" must target ciphertext, not plaintext offsets."""
    from PySide6.QtCore import QObject, Signal

    from app.core import mapfile as mapfile_mod
    from app.core.ddrescue_runner import RescueSettings
    from app.core.recovery import RecoveryContext, TargetedRecovery

    src = str(tmp_path / "src.img")
    with open(src, "wb") as fh:
        fh.write(b"\x00" * (VOLUME_OFFSET + VOLUME_SIZE))
    out = str(tmp_path / "out.img")

    class FakeRunner(QObject):
        finished = Signal(int)

        def start(self, infile, outfile, logfile, settings):
            self.settings = settings
            self.finished.emit(0)

        def take_unaligned_error(self):
            return False

    decrypt.register(out, make_source())
    try:
        runner = FakeRunner()
        engine = TargetedRecovery(runner)
        ctx = RecoveryContext(
            infile=src, outfile=out, logfile=str(tmp_path / "out.log"),
            workdir=str(tmp_path), settings=RescueSettings(sector_size=512),
            volume_offset=VOLUME_OFFSET,
        )
        # A file living in the volume's first sector — the relocated region.
        engine.run_ranges(ctx, [(VOLUME_OFFSET, 512)], "Imaged 'boot'.")
        domain = mapfile_mod.parse(runner.settings.domain_mapfile)
    finally:
        decrypt.unregister(out)

    targeted = [(b.pos, b.size) for b in domain.blocks if b.status == "+"]
    assert targeted == [(VOLUME_OFFSET + HEADER_BLOCK_OFFSET, 512)]


# --- partition identification --------------------------------------------
def test_partition_identifies_bitlocker():
    assert partition.identify_filesystem(build_boot_record()) == "bitlocker"
    assert "bitlocker" not in partition.RECOVERABLE   # not recoverable while locked


def test_locked_volume_is_the_preferred_target():
    locked = partition.Partition(1, 1 << 20, 900 << 30, "BitLocker (locked)",
                                 "bitlocker", "gpt")
    recovery = partition.Partition(2, 0, 500 << 20, "Windows Recovery", "ntfs",
                                   "gpt", is_recovery=True)
    assert partition.best_recoverable([recovery, locked]) is locked
    assert locked.is_locked and not locked.is_recoverable


def test_locked_volume_message(tmp_path):
    path = tmp_path / "locked.img"
    path.write_bytes(b"\x00" * VOLUME_OFFSET + build_boot_record())
    from app.core import volume
    message = volume.locked_volume_message(str(path), VOLUME_OFFSET)
    assert message and "BitLocker" in message
    assert volume.locked_volume_message(str(path), 0) is None
    # A locked volume has no plan: it must be unlocked before anything applies.
    assert volume.detect_filesystem(str(path), VOLUME_OFFSET) is None


# --- volume discovery -----------------------------------------------------
def test_find_volumes_without_a_partition_table(tmp_path, stretch_key):
    """The case this feature exists for: sector 0 never came back."""
    image = bytearray(VOLUME_OFFSET + VOLUME_SIZE)
    image[VOLUME_OFFSET:VOLUME_OFFSET + 512] = build_boot_record()
    block = build_metadata_block(stretch_key)
    at = VOLUME_OFFSET + METADATA_OFFSET
    image[at:at + len(block)] = block
    path = tmp_path / "no-table.img"
    path.write_bytes(bytes(image))

    found = detect.find_volumes(str(path))
    assert len(found) == 1
    assert found[0].offset == VOLUME_OFFSET
    assert found[0].source == "signature scan"
    assert found[0].size == VOLUME_SIZE          # from the FVE metadata
    assert found[0].method_name == "AES-CBC-256"


def test_find_volumes_ignores_a_plain_image(tmp_path):
    path = tmp_path / "plain.img"
    path.write_bytes(b"\x00" * (4 << 20))
    assert detect.find_volumes(str(path)) == []
