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

"""Filesystem-agnostic targeted-recovery engine.

The drive is failing and will never image 100%, so we read in *priority order*,
extracting the most valuable data first. ddrescue is the ONLY thing that ever
touches the failing device; every structure (partition table, filesystem
metadata, file data) is parsed from the **output image**, never the device.

This module owns only what is independent of the filesystem:

  * the phased state machine (run one ``ddrescue -m <domain>`` per phase into the
    same image + logfile, then parse the image to plan the next phase),
  * shared partition detection (Phase 0: image the partition table + each
    partition's first blocks, then identify the volume to target),
  * failing-drive ``--sector-size`` escalation, abort, and the Qt signals.

The filesystem-specific part is a :class:`FilesystemPlan` (see
``app/ntfs/plan.py``, ``app/ext/plan.py``): an ordered set of
:class:`PhaseHandler` objects. Each handler's ``build`` decides what byte ranges
to image for its phase; its ``parse`` reads the image afterwards and says where
to go next. The engine selects the plan from the imaged volume (or from an
explicit ``fs_type``) and then just walks it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.core import domain, mapfile
from app.core.ddrescue_runner import DdrescueRunner, RescueSettings

TABLE_SCAN_BYTES = 64 * 1024   # MBR + GPT header + entries
VBR_SCAN_BYTES = 64 * 1024     # enough of each partition to read its VBR/superblock


class Phase(Enum):
    IDLE = auto()
    # Shared partition detection.
    GET_TABLE = auto()
    GET_VBRS = auto()
    # NTFS plan.
    GET_BOOT = auto()
    GET_MFT0 = auto()
    GET_MFT = auto()
    GET_INDEX = auto()
    # ext plan.
    GET_SUPERBLOCK = auto()
    GET_GDT = auto()
    GET_INODES = auto()
    GET_DIRS = auto()
    # HFS+ plan.
    GET_VOLHEADER = auto()
    GET_CATALOG = auto()
    GET_EXTENTS = auto()
    # Shared terminal-ish phase (all plans finish by imaging file data).
    GET_FILEDATA = auto()
    # Filesystem-independent: image a user-chosen set of ranges (e.g. one folder
    # first), driven by the recovered file tree rather than a plan.
    GET_SELECTED = auto()
    DONE = auto()


# --- phase outcomes -------------------------------------------------------
@dataclass(frozen=True)
class Image:
    """Build outcome: image these ``(start, length)`` ranges this phase."""
    ranges: list[tuple[int, int]]


@dataclass(frozen=True)
class Next:
    """Build/parse outcome: skip imaging (build) or advance (parse) to ``phase``."""
    phase: "Phase"


@dataclass(frozen=True)
class Terminal:
    """Build/parse outcome: end the workflow (success or failure) with a message."""
    ok: bool
    message: str


@dataclass
class PhaseHandler:
    """One filesystem phase: build the domain, then parse the imaged result."""
    phase: Phase
    label: str
    build: Callable[["RecoveryState"], Any]   # -> Image | Next | Terminal
    parse: Callable[["RecoveryState"], Any]    # -> Next | Terminal


@dataclass
class RecoveryState:
    """Mutable run state shared with a plan's phase handlers."""
    infile: str
    outfile: str
    logfile: str
    workdir: str
    size: int                        # bytes of the source image/device
    volume_offset: int               # byte offset of the target volume on disk
    settings: RescueSettings
    stop_after_catalog: bool = False  # stop once the file catalog ($MFT/inodes) is in
    include_filedata: bool = False    # continue past metadata into file data
    fs: Any = None                    # plan-owned parsed bag (boot/superblock, ranges)
    log: Callable[[str], None] = lambda _m: None


class FilesystemPlan:
    """Per-filesystem phase plan. Subclassed by NtfsPlan / ExtPlan."""

    name: str = "?"
    first_phase: Phase = Phase.IDLE   # first phase after the volume is located
    catalog_phase: Phase = Phase.IDLE  # the "stop_after_catalog" phase
    index_phase: Phase = Phase.IDLE   # first phase after the catalog (dir metadata)

    def steps(self) -> list[tuple[Phase, str]]:
        """Ordered (phase, label) pairs for the workflow checklist."""
        raise NotImplementedError

    def handler(self, phase: Phase) -> PhaseHandler:
        """The handler for ``phase`` (KeyError if this plan has no such phase)."""
        raise NotImplementedError

    def prepare_existing(self, st: RecoveryState) -> str | None:
        """Load the catalog from an already-imaged volume into ``st.fs``.

        Returns ``None`` on success, or an error message if the catalog isn't in
        the image yet (so callers can resume mid-plan without re-imaging it).
        """
        raise NotImplementedError

    def build_tree(self, image: str, volume_offset: int):
        """Build a browseable FileTree from the image, or None."""
        raise NotImplementedError

    def filedata_domain(self, image: str, volume_offset: int, total: int,
                        sector_size: int):
        """Build the all-file-data domain mapfile from the image, or None."""
        raise NotImplementedError

    def metadata_ranges(self, image: str, volume_offset: int):
        """On-disk ranges of the metadata that resolves file extents.

        Re-imaging these on a "final completeness pass" can turn files whose
        extent map was incomplete (their tail extents live in metadata we hadn't
        fully recovered) into fully-mappable ones. Default: nothing extra.
        """
        return []


def get_source_size(path: str) -> int:
    """Size in bytes of a regular file or block device."""
    st = os.stat(path)
    if os.path.isfile(path):
        return st.st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def read_image(outfile: str, offset: int, length: int) -> bytes:
    """Read from the output image (all parsing reads the image, never the device)."""
    with open(outfile, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


@dataclass
class RecoveryContext:
    infile: str
    outfile: str
    logfile: str                       # ddrescue logfile (recovery progress map)
    workdir: str
    settings: RescueSettings
    volume_offset: int | None = None   # None => auto-detect from the imaged table
    fs_type: str = ""                  # "ntfs" | "ext" | "" (=> default NTFS)


class TargetedRecovery(QObject):
    """State machine sequencing a filesystem plan's phases (ddrescue + parse)."""

    phaseChanged = Signal(str)            # human-readable phase name
    progress = Signal(str)                # log/status messages
    finished = Signal(bool, str)          # success, summary
    sectorSizeChanged = Signal(int)       # escalated --sector-size (bytes)
    phaseStep = Signal(object)            # current Phase enum, drives the checklist
    workflowReset = Signal()              # a new run is starting; clear the checklist
    planSelected = Signal(object)         # list[(Phase, label)] for the active plan
    domainSize = Signal(int)              # bytes targeted by the current domain file

    def __init__(self, runner: DdrescueRunner, parent=None):
        super().__init__(parent)
        self._runner = runner
        self._phase = Phase.IDLE
        self._ctx: RecoveryContext | None = None
        self._st: RecoveryState | None = None
        self._plan: FilesystemPlan | None = None
        self._current_ranges: list[tuple[int, int]] = []
        self._selected_ranges: list[tuple[int, int]] = []
        self._selected_summary: str = ""
        # Built-in (filesystem-independent) handlers: partition detection, plus a
        # user-chosen range set (e.g. "image this folder first").
        self._builtin = {
            Phase.GET_TABLE: PhaseHandler(
                Phase.GET_TABLE, "Detecting partitions: imaging partition table",
                self._build_table, self._parse_table),
            Phase.GET_VBRS: PhaseHandler(
                Phase.GET_VBRS, "Detecting partitions: imaging volume boot records",
                self._build_vbrs, self._parse_vbrs),
            Phase.GET_SELECTED: PhaseHandler(
                Phase.GET_SELECTED, "Imaging selected files",
                self._build_selected, self._parse_selected),
        }
        self._candidate_starts: list[int] = []

    @property
    def active(self) -> bool:
        return self._phase not in (Phase.IDLE, Phase.DONE)

    def abort(self) -> None:
        """Stop the workflow: detach so the running phase's exit won't advance."""
        if not self.active:
            return
        self._phase = Phase.DONE
        self._disconnect()
        self.progress.emit("Recovery stopped by user. Re-run a step to resume.")
        self.finished.emit(False, "Stopped by user (progress saved to logfile).")

    # --- public API -------------------------------------------------------
    def start(self, ctx: RecoveryContext, stop_after_mft: bool = False,
              include_filedata: bool = False) -> None:
        if self.active:
            raise RuntimeError("A targeted recovery is already running.")
        self._begin_session(ctx)
        self._st.stop_after_catalog = stop_after_mft
        self._st.include_filedata = include_filedata
        if ctx.volume_offset is None:
            self._enter(Phase.GET_TABLE)
        else:
            self._st.volume_offset = ctx.volume_offset
            self._select_plan(self._plan_for(ctx.fs_type))
            self._enter(self._plan.first_phase)

    def run_from_existing_mft(self, ctx: RecoveryContext) -> None:
        """Skip to imaging index/dir regions from an already-recovered catalog."""
        if self._prepare_from_existing(ctx):
            self._enter(self._plan.index_phase)

    def run_filedata_from_existing_mft(self, ctx: RecoveryContext) -> None:
        """Skip to imaging all file data from an already-recovered catalog."""
        if self._prepare_from_existing(ctx):
            self._enter(Phase.GET_FILEDATA)

    def run_ranges(self, ctx: RecoveryContext, ranges: list[tuple[int, int]],
                   summary: str) -> None:
        """Image an explicit set of byte ranges (e.g. one folder's data first).

        Filesystem-independent: the ranges come from the recovered file tree, so
        no plan is selected. Reuses the normal phase machinery (domain build,
        ddrescue, unaligned-sector retry) via the built-in GET_SELECTED handler.
        """
        if self.active:
            raise RuntimeError("A targeted recovery is already running.")
        self._begin_session(ctx)
        self._selected_ranges = ranges
        self._selected_summary = summary
        self._enter(Phase.GET_SELECTED)

    def _prepare_from_existing(self, ctx: RecoveryContext) -> bool:
        if self.active:
            raise RuntimeError("A targeted recovery is already running.")
        self._begin_session(ctx)
        self._st.volume_offset = ctx.volume_offset or 0
        self._select_plan(
            self._detect_plan(self._st.volume_offset) or self._plan_for(ctx.fs_type))
        err = self._plan.prepare_existing(self._st)
        if err:
            self._fail(err)
            return False
        return True

    # --- plan selection ---------------------------------------------------
    def _plan_for(self, fs_type: str) -> FilesystemPlan:
        from app.core import volume
        return volume.plan_for_fs(fs_type)

    def _detect_plan(self, offset: int) -> FilesystemPlan | None:
        from app.core import volume
        return volume.detect_filesystem(self._ctx.outfile, offset)

    def _select_plan(self, plan: FilesystemPlan) -> None:
        self._plan = plan
        self.planSelected.emit(plan.steps())

    def _handler(self, phase: Phase) -> PhaseHandler:
        if phase in self._builtin:
            return self._builtin[phase]
        return self._plan.handler(phase)

    # --- session lifecycle ------------------------------------------------
    def _begin_session(self, ctx: RecoveryContext) -> None:
        self._ctx = ctx
        self._plan = None
        self._candidate_starts = []
        self._st = RecoveryState(
            infile=ctx.infile, outfile=ctx.outfile, logfile=ctx.logfile,
            workdir=ctx.workdir, size=get_source_size(ctx.infile),
            volume_offset=ctx.volume_offset or 0, settings=ctx.settings,
            log=self.progress.emit,
        )
        self.workflowReset.emit()
        self._runner.finished.connect(self._on_phase_finished)

    def _enter(self, phase: Phase) -> None:
        """Set the active phase, announce it, and run its build step."""
        self._phase = phase
        # Guard the plan code: a parser bug, a truncated/garbage image, or a plan
        # that targets a phase it doesn't define (KeyError from _handler) must end
        # the workflow cleanly (which disconnects the runner), never escape this
        # slot and leave the state machine wedged "active" with a live signal.
        try:
            handler = self._handler(phase)
            self.phaseChanged.emit(handler.label)
            self.phaseStep.emit(phase)
            outcome = handler.build(self._st)
            if isinstance(outcome, Image):
                self._run_domain(outcome.ranges)
                return
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            self._fail(f"Internal error during {phase.name}: {exc}")
            return
        if isinstance(outcome, Next):
            self._enter(outcome.phase)
        else:  # Terminal
            self._finish(outcome)

    def _on_phase_finished(self, exit_code: int) -> None:
        if not self.active:
            return
        self.progress.emit(f"ddrescue phase exited with code {exit_code}.")
        # An unaligned read error means our --sector-size is smaller than the
        # device's real sector; retry the *same* phase at a larger size before
        # giving up. (Nothing useful was read, so this loses no recovered data.)
        if self._runner.take_unaligned_error():
            if self._retry_with_larger_sector():
                return
            self._fail(
                "Unaligned read errors persist even at a 8 KiB sector size — the "
                "drive may be failing reads entirely. Imaging stopped."
            )
            return
        # A non-zero code is normal on a damaged drive; we proceed with whatever
        # was recovered and let the handler's parse decide if it is enough.
        try:
            outcome = self._handler(self._phase).parse(self._st)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            self._fail(f"Internal error parsing {self._phase.name}: {exc}")
            return
        if isinstance(outcome, Next):
            self._enter(outcome.phase)
        else:  # Terminal
            self._finish(outcome)

    # --- shared partition-detection phases --------------------------------
    def _build_table(self, st: RecoveryState):
        return Image([(0, TABLE_SCAN_BYTES)])

    def _parse_table(self, st: RecoveryState):
        from app.core import partition
        parts = partition.scan_device(st.outfile)
        if not parts:
            st.log("No partition table found — treating disk as a bare volume.")
            st.volume_offset = 0
            self._select_plan(self._detect_plan(0) or self._plan_for(""))
            return Next(self._plan.first_phase)
        self._candidate_starts = [p.start for p in parts]
        st.log(f"Partition table: {len(parts)} partition(s) found.")
        return Next(Phase.GET_VBRS)

    def _build_vbrs(self, st: RecoveryState):
        return Image([(s, VBR_SCAN_BYTES) for s in self._candidate_starts])

    def _parse_vbrs(self, st: RecoveryState):
        from app.core import partition
        parts = partition.scan_device(st.outfile)
        target = partition.best_recoverable(parts)
        for p in parts:
            if p is target:
                # fs_type is empty when we picked it from the partition table
                # (its boot sector wasn't readable) — show the table's role.
                what = p.fs_type.upper() or f"{p.type_name}, VBR unread"
                tag = f"  <- TARGET ({what})"
            elif p.is_recovery:
                tag = "  (recovery — skipped)"
            elif p.is_recoverable:
                tag = f"  <- {p.fs_type.upper()}"
            else:
                tag = ""
            st.log(f"  #{p.index} {p.scheme} @0x{p.start:X} {p.type_name} "
                   f"{p.label}{tag}")
        if target is None:
            st.log("No recoverable filesystem detected — using offset 0.")
            st.volume_offset = 0
            self._select_plan(self._detect_plan(0) or self._plan_for(""))
        else:
            st.volume_offset = target.start
            self._select_plan(self._plan_for(target.fs_type))
            st.log(f"Targeting {self._plan.name} volume at offset 0x{target.start:X}.")
            if not target.fs_type:
                st.log("  (identified from the partition table; its boot sector "
                       "wasn't readable — defaulting to NTFS.)")
        return Next(self._plan.first_phase)

    # --- user-selected ranges (folder prioritization) ---------------------
    def _build_selected(self, st: RecoveryState):
        if not self._selected_ranges:
            return Terminal(True, "Nothing to image — the selection has no "
                                  "on-disk data (already captured with the catalog).")
        return Image(self._selected_ranges)

    def _parse_selected(self, st: RecoveryState):
        return Terminal(True, self._selected_summary)

    # --- domain build + ddrescue ------------------------------------------
    def _run_domain(self, ranges: list[tuple[int, int]]) -> None:
        st = self._st
        self._current_ranges = ranges
        dmap = domain.build_domain_mapfile(ranges, st.size, st.settings.sector_size)
        self.domainSize.emit(domain.covered_bytes(dmap))
        dmap_path = os.path.join(st.workdir, f"domain_{self._phase.name.lower()}.dmap")
        mapfile.write(dmap_path, dmap)
        settings = replace(st.settings, domain_mapfile=dmap_path, loose_domain=True)
        self._runner.start(st.infile, st.outfile, st.logfile, settings)

    # --- failing-drive sector-size escalation ----------------------------
    @staticmethod
    def _next_sector_size(current: int) -> int | None:
        """Next larger sector size to try, or None if we've exhausted them."""
        for size in (4096, 8192):
            if size > current:
                return size
        return None

    def _retry_with_larger_sector(self) -> bool:
        """Re-run the current phase at a larger sector size. False if none left."""
        current = self._st.settings.sector_size
        nxt = self._next_sector_size(current)
        if nxt is None:
            return False
        self._st.settings = replace(self._st.settings, sector_size=nxt)
        # Keep the existing logfile. ddrescue reads a mapfile written at a
        # different sector size without complaint, so any prior progress — which
        # may be a long rescue sharing this logfile — is preserved. The regions
        # that just failed are still marked non-tried, so they get retried at the
        # larger sector size.
        self.progress.emit(
            f"Unaligned reads at sector-size {current} B — retrying this phase "
            f"at {nxt} B (existing progress preserved)."
        )
        self.sectorSizeChanged.emit(nxt)
        # Same defensive boundary as _enter: a failure starting the retry must end
        # cleanly, not escape the Qt slot. We still return True (handled) so the
        # caller doesn't also emit the generic unaligned-give-up failure.
        try:
            self._run_domain(self._current_ranges)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            self._fail(f"Internal error during {self._phase.name}: {exc}")
        return True

    # --- terminal ---------------------------------------------------------
    def _finish(self, outcome: Terminal) -> None:
        self._phase = Phase.DONE
        self._disconnect()
        self.finished.emit(outcome.ok, outcome.message)

    def _fail(self, message: str) -> None:
        self._finish(Terminal(False, message))

    def _disconnect(self) -> None:
        try:
            self._runner.finished.disconnect(self._on_phase_finished)
        except (RuntimeError, TypeError):
            pass
