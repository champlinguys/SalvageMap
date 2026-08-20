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

"""Unlock a CoreStorage logical volume with its password, via libfvde.

CoreStorage's key hierarchy is deep — a wiped EncryptedRoot.plist, a
passphrase-wrapped KEK, a KEK-wrapped volume key, then AES-XTS — so the
derivation is delegated to ``libfvde`` (Debian/Ubuntu: ``python3-libfvde``)
rather than reimplemented. Everything *around* it is ours: the image slicing,
the read recording that yields the physical mapping, and the error messages.

Nothing here touches the failing drive. libfvde is handed a read-only window
onto the image ddrescue already produced, and the password lives only as long
as the unlock call.
"""

from __future__ import annotations

import os
from typing import Any

# The volume's HFS+ filesystem is inside the *logical* volume, whose bytes start
# some way into the physical volume (64 MiB on the reference drive, behind the
# CoreStorage metadata). We never assume that figure — it is measured; see
# :mod:`app.corestorage.segments`.

MISSING_LIBRARY_HINT = (
    "libfvde is not installed, so CoreStorage volumes cannot be unlocked.\n\n"
    "Install it with:  sudo apt install python3-libfvde libfvde-utils"
)


class UnlockError(Exception):
    """The volume could not be unlocked (wrong password, or unusable metadata)."""


def _pyfvde():
    """Import pyfvde, or raise :class:`UnlockError` with an actionable message."""
    try:
        import pyfvde                       # noqa: PLC0415 (optional dependency)
    except ImportError as exc:
        raise UnlockError(MISSING_LIBRARY_HINT) from exc
    return pyfvde


def available() -> bool:
    """True if libfvde is installed, so the UI can say so before asking."""
    try:
        import pyfvde                       # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


class ImageSlice:
    """A read-only file object over ``[base, base + size)`` of an image.

    libfvde wants a file object holding exactly the CoreStorage physical volume,
    but our image is the whole disk, so it gets a window rather than a copy.
    Reads are recorded: the physical offsets libfvde touches while serving a
    logical-volume read are how :mod:`app.corestorage.segments` learns the
    mapping without having to guess at CoreStorage's segment descriptors.

    Short reads past the end of a partial image return what there is; a hole
    reads as whatever the sparse image holds (zeros), which libfvde will reject
    as bad metadata rather than silently mis-decrypt.
    """

    def __init__(self, path: str, base: int, size: int, record: bool = False):
        self._fh = open(path, "rb")
        self._base = base
        self._size = max(size, 0)
        self._pos = 0
        self.reads: list[tuple[int, int]] = []
        self._record = record

    # --- file-object protocol libfvde uses --------------------------------
    def read(self, size: int | None = None) -> bytes:
        remaining = self._size - self._pos
        want = remaining if size is None else max(0, min(size, remaining))
        if self._record:
            self.reads.append((self._pos, want))
        try:
            self._fh.seek(self._base + self._pos)
            data = self._fh.read(want)
        except OSError:
            data = b""
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self._size + offset
        self._pos = max(self._pos, 0)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def get_size(self) -> int:
        return self._size

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    # --- read recording ---------------------------------------------------
    def start_recording(self) -> None:
        self.reads.clear()
        self._record = True

    def stop_recording(self) -> list[tuple[int, int]]:
        self._record = False
        recorded, self.reads = list(self.reads), []
        return recorded


class UnlockedVolume:
    """An unlocked CoreStorage logical volume: plaintext reads, and its mapping.

    Offsets in this class are *logical volume* relative — byte 0 is the first
    byte of the volume the filesystem lives in, not of the partition.
    """

    def __init__(self, volume: Any, logical_volume: Any,
                 header_slice: ImageSlice, data_slice: ImageSlice,
                 volume_offset: int):
        self._volume = volume
        self._lv = logical_volume
        self._header_slice = header_slice
        self._data_slice = data_slice
        self.volume_offset = volume_offset      # CS partition start, on disk
        self._limit: int | None = None          # memoised readable_limit()

    @property
    def size(self) -> int:
        return int(self._lv.get_size())

    @property
    def physical_size(self) -> int:
        """Size of the CoreStorage partition the volume sits in."""
        return self._data_slice.get_size()

    @property
    def name(self) -> str:
        return str(self._lv.get_name() or "")

    @property
    def identifier(self) -> str:
        return str(self._lv.get_identifier() or "")

    def read(self, offset: int, length: int) -> bytes:
        """``length`` plaintext bytes at logical-volume ``offset`` (zero-filled)."""
        if length <= 0 or offset >= self.size:
            return b""
        want = min(length, self.size - offset)
        try:
            data = self._lv.read_buffer_at_offset(want, offset)
        except OSError:
            data = b""
        if len(data) < length:
            data += b"\x00" * (length - len(data))
        return data

    def can_read(self, offset: int) -> bool:
        """Whether libfvde can decrypt at ``offset`` at all (errors not swallowed)."""
        if offset < 0 or offset >= self.size:
            return False
        try:
            self._lv.read_buffer_at_offset(512, offset)
        except OSError:
            return False
        return True

    def readable_limit(self) -> int:
        """Memoised :meth:`_compute_readable_limit` (it costs ~40 reads)."""
        if self._limit is None:
            self._limit = self._compute_readable_limit()
        return self._limit

    def _compute_readable_limit(self) -> int:
        """First logical offset libfvde refuses to decrypt (``size`` if none).

        libfvde indexes the volume with a 32-bit element index, so on a volume
        larger than 2^31 sectors every read past 1 TiB fails. That is a hard
        ceiling in the library, not something the image or the password affects,
        and it has to be known before probing the mapping — probing above it
        observes nothing and looks exactly like "the mapping is unmeasurable".

        Imaging is unaffected: ddrescue copies ciphertext, and the HFS+
        structures that drive targeted recovery sit far below the ceiling.
        """
        if self.can_read(max(self.size - 512, 0)):
            return self.size
        low, high = 0, self.size - 512
        if not self.can_read(0):
            return 0
        while high - low > 512:
            mid = ((low + high) // 2) & ~511
            if mid <= low:
                break
            if self.can_read(mid):
                low = mid
            else:
                high = mid
        return low + 512

    @property
    def _slices(self) -> tuple[ImageSlice, ...]:
        return (self._data_slice, self._header_slice)

    def _bust_cache(self, avoid: int) -> None:
        """Read far from ``avoid`` so the next read cannot come from libfvde's cache.

        libbfio caches recently-read blocks, and a cached read never reaches our
        file object — so it would be invisible to :meth:`observe_physical` and
        look like "no reads happened". Evicting first is what makes an
        observation reliable.

        The evicting reads must land *below* :meth:`readable_limit`: above it
        every read fails, nothing is cached, nothing is evicted, and the probe
        that follows is still served from cache — which reads as an unmeasurable
        mapping even though the volume is perfectly ordinary.
        """
        span = self.readable_limit() or self.size
        for fraction in (1, 2, 3, 4):
            position = ((span * fraction) // 5) & ~511
            if abs(position - avoid) > (64 << 20):
                self.read(position, 512)

    def observe_physical(self, offset: int, length: int = 512,
                         bust: bool = True) -> list[tuple[int, int]]:
        """Physical (partition-relative) reads libfvde makes to serve ``offset``.

        This is what makes the logical→physical mapping *measured* rather than
        assumed: rather than parse CoreStorage's segment descriptors (which
        libfvde does not expose), watch where it actually reads from.

        Both file objects are recorded. libfvde serves logical-volume data
        through either the physical-volume pool or the volume's own handle
        depending on what it has open, and watching only one of them is how this
        silently observed nothing at all.
        """
        reads: list[tuple[int, int]] = []
        for attempt in range(2):
            if bust:
                self._bust_cache(offset)
            for image_slice in self._slices:
                image_slice.start_recording()
            try:
                self.read(offset, length)
            finally:
                for image_slice in self._slices:
                    reads += image_slice.stop_recording()
            if reads or not bust:
                break
            # Nothing observed: almost certainly still a cache hit. One retry,
            # since a genuinely unreadable offset costs only the second attempt.
        return reads

    def close(self) -> None:
        for closer in (self._volume.close,):
            try:
                closer()
            except OSError:
                pass
        self._header_slice.close()
        self._data_slice.close()


def _describe(exc: Exception) -> str:
    """Turn libfvde's stacked C error text into something a tech can act on."""
    text = str(exc)
    lowered = text.lower()
    if "wrong password" in lowered or "unable to unwrap" in lowered:
        return ("The password was not accepted. Check it with the customer — a "
                "CoreStorage volume gives no other hint that it is wrong.")
    if "encrypted root plist" in lowered:
        return ("The volume's EncryptedRoot.plist could not be read. It lives on "
                "the Booter partition, so that partition has to be imaged too — "
                "see Tools ▸ Unlock CoreStorage volume… for pointing at the file.")
    return text


def unlock_with_password(image: str, volume_offset: int, volume_size: int,
                         password: str,
                         encrypted_root_plist: str = "") -> UnlockedVolume:
    """Open the CoreStorage volume at ``volume_offset`` and unlock it.

    ``encrypted_root_plist`` optionally points at an ``EncryptedRoot.plist.wipekey``
    extracted from the Booter partition; libfvde usually finds what it needs in
    the volume itself, so this is a fallback rather than the normal path.

    Raises :class:`UnlockError` with a message meant for the tech on any failure.
    """
    pyfvde = _pyfvde()
    header_slice = ImageSlice(image, volume_offset, volume_size)
    data_slice = ImageSlice(image, volume_offset, volume_size)
    volume = pyfvde.volume()
    try:
        volume.open_file_object(header_slice)
        # Reading the logical volume goes through a second handle: libfvde keeps
        # the volume-group metadata and the volume data on separate pools.
        volume.open_physical_volume_files_as_file_objects([data_slice])
        if encrypted_root_plist:
            volume.read_encrypted_root_plist(encrypted_root_plist)

        group = volume.get_volume_group()
        if group.get_number_of_logical_volumes() < 1:
            raise UnlockError(
                "The CoreStorage volume group holds no logical volume — its "
                "metadata is probably damaged. Re-image the start of the "
                "partition and try again.")
        logical_volume = group.get_logical_volume(0)

        volume.set_password(password)
        logical_volume.set_password(password)
        logical_volume.unlock()
    except UnlockError:
        header_slice.close()
        data_slice.close()
        raise
    except Exception as exc:
        header_slice.close()
        data_slice.close()
        raise UnlockError(_describe(exc)) from exc

    return UnlockedVolume(volume, logical_volume, header_slice, data_slice,
                          volume_offset)
