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

"""Run and monitor GNU ddrescue via QProcess.

Builds the command line, enforces safety guards (never write to the source),
streams ddrescue's status output into structured fields, and tails the working
mapfile so the sector map can update live.
"""

from __future__ import annotations

import os
import re
import signal
import stat
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from app.core import mapfile
from app.core.terminal import LiveScreen


@dataclass
class RescueSettings:
    sector_size: int = 512
    retry_passes: int = 0
    reverse: bool = False
    complete_only: bool = False
    loose_domain: bool = False
    no_trim: bool = False                # --no-trim   (skip slow trimming phase)
    no_scrape: bool = False              # --no-scrape (skip slow scraping phase)
    # Default --timeout so a rescue can never hang forever when a drive drops off
    # the bus. ddrescue exits if there is no successful read for this long; just
    # re-run to resume from the logfile. Set to None to disable.
    timeout: str | None = "5m"
    min_read_rate: str | None = None     # --min-read-rate, e.g. "64Ki"
    skip_size: str | None = None         # e.g. "64KiB" or "64KiB,1MiB"
    input_position: int | None = None    # bytes
    size: int | None = None              # bytes
    domain_mapfile: str | None = None
    mapfile_interval: str = "1"          # seconds between mapfile saves
    extra_args: list[str] = field(default_factory=list)


def failing_drive_settings(base: "RescueSettings | None" = None) -> "RescueSettings":
    """A fast-first-pass profile for a drive that drops out / hangs on bad areas.

    Skips the slow trim/scrape phases, bails out instead of hanging when the
    drive stops responding, and skips quickly past read errors. Run repeatedly
    (power-cycling the drive between runs); ddrescue resumes from the logfile.
    """
    from dataclasses import replace
    s = base or RescueSettings()
    return replace(
        s,
        no_trim=True,
        no_scrape=True,
        retry_passes=0,
        timeout="30s",
        skip_size="1MiB,64MiB",
    )


# ddrescue redraws its status block using ANSI cursor-movement escapes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Tokens ddrescue prints, e.g. "  rescued:    2048 B,   bad areas:  0,"
_FIELD_RE = re.compile(
    r"(ipos|opos|non-tried|non-trimmed|non-scraped|bad-sector|rescued|"
    r"bad areas|pct rescued|read errors|current rate|average rate|error rate|"
    r"run time|remaining time)\s*:\s*([^,\n]+)"
)


def parse_status_line(text: str) -> dict[str, str]:
    """Extract ddrescue status fields from a chunk of its output."""
    out: dict[str, str] = {}
    for key, value in _FIELD_RE.findall(text):
        out[key.strip()] = value.strip()
    return out


class SafetyError(Exception):
    """Raised when a rescue would be unsafe (e.g. writing to the source)."""


# Filesystems with no sparse-file support: writing a device-sized image at high
# offsets physically allocates everything in between (gigabytes of zeros).
NON_SPARSE_FS = {"exfat", "vfat", "msdos", "fat", "fat12", "fat16", "fat32"}


def filesystem_type(path: str) -> str | None:
    """Filesystem type of the mount containing ``path`` (via /proc/mounts)."""
    target = os.path.abspath(path)
    # Walk up to the nearest existing ancestor (the file may not exist yet).
    while not os.path.exists(target) and target != "/":
        target = os.path.dirname(target)
    best_mount, best_fs = "", None
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].encode().decode("unicode_escape")  # \040 -> space
                fstype = parts[2]
                if (target == mount or target.startswith(mount.rstrip("/") + "/")) \
                        and len(mount) > len(best_mount):
                    best_mount, best_fs = mount, fstype
    except OSError:
        return None
    return best_fs


def non_sparse_destination(outfile: str) -> str | None:
    """Return the fs type if the output lives on a non-sparse filesystem."""
    fs = filesystem_type(os.path.dirname(os.path.abspath(outfile)) or ".")
    if fs and fs.lower() in NON_SPARSE_FS:
        return fs
    return None


def source_size(infile: str) -> int | None:
    """Size in bytes of the source device or file, or None if unknowable."""
    try:
        fd = os.open(infile, os.O_RDONLY)
    except OSError:
        return None
    try:
        return os.lseek(fd, 0, os.SEEK_END) or None
    except OSError:
        return None
    finally:
        os.close(fd)


def presize_image(infile: str, outfile: str) -> int | None:
    """Grow the image to the source's full size. Returns the new size, or None.

    ddrescue writes sparsely, so an interrupted image ends at the highest offset
    written rather than the size of the drive. That matters because a GPT header
    points at the disk's final sectors: when the image is short, those references
    fall outside the file and tools reject the partition table outright, showing
    no partitions and no files even though the filesystem is perfectly intact.

    Only ever grows, and only where holes are free — so it never allocates real
    space and never touches a rescued byte.
    """
    if non_sparse_destination(outfile):
        return None
    size = source_size(infile)
    if not size:
        return None
    try:
        current = os.path.getsize(outfile)
    except OSError:
        current = 0
    if current >= size:
        return None
    # O_CREAT without O_TRUNC: never discards an existing partial image.
    fd = os.open(outfile, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.ftruncate(fd, size)
    except OSError:
        return None
    finally:
        os.close(fd)
    return size


def validate_targets(infile: str, outfile: str) -> None:
    """Refuse configurations that could destroy data.

    - input and output must differ
    - the output must not be a block device (we only ever write image files)
    """
    if os.path.abspath(infile) == os.path.abspath(outfile):
        raise SafetyError("Output must differ from the source.")
    try:
        st = os.stat(outfile)
        if stat.S_ISBLK(st.st_mode):
            raise SafetyError("Refusing to write to a block device as output.")
    except FileNotFoundError:
        pass  # output image will be created — fine


def build_command(
    infile: str,
    outfile: str,
    mapfile_path: str,
    settings: RescueSettings,
    ddrescue: str = "ddrescue",
) -> list[str]:
    """Assemble the ddrescue argv from settings."""
    argv = [ddrescue, f"--sector-size={settings.sector_size}"]
    argv.append(f"--mapfile-interval={settings.mapfile_interval}")
    if settings.retry_passes:
        argv.append(f"--retry-passes={settings.retry_passes}")
    if settings.reverse:
        argv.append("--reverse")
    if settings.complete_only:
        argv.append("--complete-only")
    if settings.no_trim:
        argv.append("--no-trim")
    if settings.no_scrape:
        argv.append("--no-scrape")
    if settings.timeout:
        argv.append(f"--timeout={settings.timeout}")
    if settings.min_read_rate:
        argv.append(f"--min-read-rate={settings.min_read_rate}")
    if settings.skip_size:
        argv.append(f"--skip-size={settings.skip_size}")
    if settings.input_position is not None:
        argv.append(f"--input-position={settings.input_position}")
    if settings.size is not None:
        argv.append(f"--size={settings.size}")
    if settings.domain_mapfile:
        argv.append(f"--domain-mapfile={settings.domain_mapfile}")
        if settings.loose_domain:
            argv.append("--loose-domain")
    argv.extend(settings.extra_args)
    argv.extend([infile, outfile, mapfile_path])
    return argv


class DdrescueRunner(QObject):
    """Drives one ddrescue invocation and reports progress via signals."""

    started = Signal(list)               # argv
    logLine = Signal(str)
    screenUpdated = Signal(str)          # full terminal mirror of ddrescue
    statusUpdated = Signal(dict)         # parsed fields
    mapfileUpdated = Signal(object)      # mapfile.Mapfile
    finished = Signal(int)               # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._mapfile_path: str | None = None
        self._buffer = ""
        self._screen = LiveScreen()
        self._last_mtime = 0.0
        self._unaligned = False

        self._poll = QTimer(self)
        self._poll.setInterval(750)
        self._poll.timeout.connect(self._poll_mapfile)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def take_unaligned_error(self) -> bool:
        """Return (and clear) whether the last pass died on an unaligned read."""
        flag = self._unaligned
        self._unaligned = False
        return flag

    def start(
        self,
        infile: str,
        outfile: str,
        mapfile_path: str,
        settings: RescueSettings,
    ) -> None:
        if self.is_running:
            raise RuntimeError("A rescue is already running.")
        validate_targets(infile, outfile)
        presized = presize_image(infile, outfile)
        argv = build_command(infile, outfile, mapfile_path, settings)

        self._mapfile_path = mapfile_path
        self._buffer = ""
        self._screen.reset()
        self._last_mtime = 0.0
        self._unaligned = False

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_output)
        proc.finished.connect(self._on_finished)
        self._proc = proc

        self.started.emit(argv)
        if presized:
            self.logLine.emit(f"Pre-sized image to source size ({presized} bytes).")
        proc.start(argv[0], argv[1:])
        self._poll.start()

    def stop(self) -> None:
        """Send SIGINT so ddrescue saves its mapfile and exits cleanly."""
        if self._proc and self.is_running:
            pid = int(self._proc.processId())
            if pid > 0:
                os.kill(pid, signal.SIGINT)

    # --- internals --------------------------------------------------------
    def _on_output(self) -> None:
        if not self._proc:
            return
        raw = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        # "Unaligned read error" means the device's real sector size is larger
        # than the one we passed; every read fails, so abort this pass cleanly
        # (SIGINT lets ddrescue save its mapfile) and let the caller retry with a
        # larger --sector-size. Latch so we only act on the first occurrence.
        if not self._unaligned and "Unaligned read error" in raw:
            self._unaligned = True
            self.logLine.emit(
                "ddrescue: unaligned read error — device sector size is larger "
                "than the configured one; stopping this pass to retry larger."
            )
            self.stop()
        # Replay ddrescue's in-place redraw onto a virtual screen and mirror it.
        self._screen.feed(raw)
        screen = self._screen.render()
        self.screenUpdated.emit(screen)
        # Parse the live numeric fields from the mirrored screen.
        status = parse_status_line(screen)
        if status:
            self.statusUpdated.emit(status)

    def _poll_mapfile(self) -> None:
        if not self._mapfile_path:
            return
        try:
            mtime = os.path.getmtime(self._mapfile_path)
        except OSError:
            return
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        try:
            mf = mapfile.parse(self._mapfile_path)
        except OSError:
            return
        self.mapfileUpdated.emit(mf)

    def _on_finished(self, exit_code: int, _status) -> None:
        self._poll.stop()
        self._poll_mapfile()  # final read
        self.finished.emit(int(exit_code))
