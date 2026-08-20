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

"""The CoreStorage physical-volume header, parsed natively.

Only enough of the structure to *identify* a CoreStorage volume and describe it
to the tech before they type a password: the unlock itself is libfvde's job (see
:mod:`app.corestorage.keys`). Parsing detection ourselves keeps it working on a
partial image and keeps the drives picker honest with no library installed.

Field offsets were read off a real FileVault 2 volume (a 14 TB WD Ultrastar) and
cross-checked against the partition table, which is why ``physical_volume_size``
is trusted: it matched the GPT entry's size exactly, to the byte. The fields
this module does *not* expose — the metadata block contents, the wiped key — are
the ones only libfvde interprets, so guessing at them here would buy nothing.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Callable

# The header lives at the very start of the CoreStorage physical volume.
HEADER_SIZE = 512

# "CS" — the signature that makes this a CoreStorage volume rather than 512
# bytes of coincidence. It sits well inside the block, not at offset 0, because
# the first bytes are a checksum over the rest.
SIGNATURE_OFFSET = 0x58
SIGNATURE = b"CS"

_VERSION_OFFSET = 0x08
_BLOCK_TYPE_OFFSET = 0x0A
_BLOCK_SIZE_OFFSET = 0x30
_VOLUME_SIZE_OFFSET = 0x40
_CHECKSUM_ALGORITHM_OFFSET = 0x5A
_METADATA_BLOCK_SIZE_OFFSET = 0x60
_METADATA_SIZE_OFFSET = 0x64
_METADATA_BLOCKS_OFFSET = 0x70   # four u64 block numbers, unused slots zeroed
_METADATA_BLOCK_SLOTS = 4
_KEY_DATA_SIZE_OFFSET = 0xA8
_ENCRYPTION_METHOD_OFFSET = 0xAC
_UUID_OFFSET = 0x130             # big-endian, i.e. already in RFC 4122 order

# Block type 0x0010 is the volume header itself; the metadata blocks the header
# points at carry other types. Checked so a stray "CS" can't pass for a header.
VOLUME_HEADER_BLOCK_TYPE = 0x0010

# libfvde's encryption-method numbering. A FileVault 2 volume reads 2; 0 means
# CoreStorage without encryption (a plain logical volume group), which is not a
# locked volume and must not be offered for unlocking.
ENCRYPTION_NONE = 0
ENCRYPTION_AES_128_XTS = 2
_METHOD_NAMES = {ENCRYPTION_NONE: "unencrypted",
                 ENCRYPTION_AES_128_XTS: "AES-128-XTS"}


@dataclass(frozen=True)
class VolumeHeader:
    """The parsed CoreStorage volume header."""
    version: int
    block_size: int                 # bytes per physical block (512 here)
    physical_volume_size: int       # bytes; matches the GPT partition size
    encryption_method: int
    identifier: str                 # physical volume UUID, as macOS shows it
    metadata_block_size: int = 0
    metadata_size: int = 0
    metadata_blocks: tuple[int, ...] = field(default_factory=tuple)

    @property
    def is_encrypted(self) -> bool:
        return self.encryption_method != ENCRYPTION_NONE

    @property
    def method_name(self) -> str:
        return _METHOD_NAMES.get(self.encryption_method,
                                 f"unknown method {self.encryption_method}")

    @property
    def description(self) -> str:
        """One line for the UI: what this is, in the tech's words."""
        return f"CoreStorage (FileVault 2), {self.method_name}"


def looks_like_corestorage(head: bytes) -> bool:
    """True if ``head`` (the volume's first sector) is a CoreStorage header.

    Signature *and* block type, because two bytes on their own are weak: this is
    the probe the partition scanner uses to label a volume, and mislabelling a
    volume as encrypted sends the tech looking for a password that doesn't exist.
    """
    if len(head) < HEADER_SIZE:
        return False
    if head[SIGNATURE_OFFSET:SIGNATURE_OFFSET + len(SIGNATURE)] != SIGNATURE:
        return False
    block_type = struct.unpack_from("<H", head, _BLOCK_TYPE_OFFSET)[0]
    return block_type == VOLUME_HEADER_BLOCK_TYPE


def parse(head: bytes) -> VolumeHeader | None:
    """Parse the header out of ``head``, or None if it isn't one."""
    if not looks_like_corestorage(head):
        return None
    blocks = tuple(
        n for n in struct.unpack_from(
            f"<{_METADATA_BLOCK_SLOTS}Q", head, _METADATA_BLOCKS_OFFSET) if n
    )
    return VolumeHeader(
        version=struct.unpack_from("<H", head, _VERSION_OFFSET)[0],
        block_size=struct.unpack_from("<Q", head, _BLOCK_SIZE_OFFSET)[0],
        physical_volume_size=struct.unpack_from("<Q", head, _VOLUME_SIZE_OFFSET)[0],
        encryption_method=struct.unpack_from("<I", head, _ENCRYPTION_METHOD_OFFSET)[0],
        identifier=str(uuid.UUID(bytes=head[_UUID_OFFSET:_UUID_OFFSET + 16])),
        metadata_block_size=struct.unpack_from(
            "<I", head, _METADATA_BLOCK_SIZE_OFFSET)[0],
        metadata_size=struct.unpack_from("<I", head, _METADATA_SIZE_OFFSET)[0],
        metadata_blocks=blocks,
    )


# --- what has to be imaged before an unlock can even be attempted ---------
# The CoreStorage metadata libfvde needs is *not* all at the front of the
# partition: two of the copies this header points at live at the very end of the
# disk. On the reference 14 TB drive they sit 12.733 TiB in, so a rescue that
# only imaged the first few hundred MiB cannot unlock at all — and the failure
# looks like a wrong password rather than a missing region, which is what makes
# it worth computing these ranges explicitly.
#
# Everything ahead of the logical volume is CoreStorage's own metadata. On the
# reference drive the logical volume began exactly 64 MiB in and libfvde's
# deepest front read ended at 56 MiB, so 128 MiB is a doubled bound rather than
# a fitted one. It costs nothing against a multi-terabyte image, and it reaches
# past the logical volume's first bytes — which means the HFS+ volume header
# becomes readable as soon as the volume is unlocked, with no second pass.
FRONT_METADATA_BYTES = 128 << 20

# Imaged around each metadata copy the header points at, on top of the header's
# own ``metadata_size``. libfvde read exactly ``metadata_size`` at each on the
# reference drive; the margin covers a larger metadata area elsewhere.
METADATA_COPY_MARGIN = 4 << 20


def unlock_ranges(header: VolumeHeader) -> list[tuple[int, int]]:
    """Volume-relative ranges that must be imaged before an unlock can succeed.

    These are *raw* ranges — physical offsets within the CoreStorage partition,
    not logical-volume offsets — because they are read before there is any
    unlocked volume to map through.
    """
    ranges = [(0, FRONT_METADATA_BYTES)]
    span = (header.metadata_size or 0) + METADATA_COPY_MARGIN
    block = header.metadata_block_size or header.block_size or 4096
    for number in header.metadata_blocks:
        start = number * block
        if start >= FRONT_METADATA_BYTES:      # the front region already has it
            ranges.append((start, span))
    return ranges


def parse_with(reader: Callable[[int, int], bytes]) -> VolumeHeader | None:
    """Parse using a volume-relative ``reader(offset, length)``."""
    return parse(reader(0, HEADER_SIZE))
