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

"""Main application window: menu bar, sector map, status/log docks."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core import config, mapfile
from app.core.ddrescue_runner import (
    DdrescueRunner,
    RescueSettings,
    SafetyError,
    non_sparse_destination,
)
from app.core import partition
from app.core import tree_export
from app.core.recovery import (
    Phase,
    RecoveryContext,
    TargetedRecovery,
    get_source_size,
)
from app.core.volume import detect_filesystem
from app.ui.ddrescue_view import DdrescueView
from app.ui.device_dialog import DeviceDialog
from app.ui.file_tree_panel import FileTreePanel
from app.ui.html_export_dialog import HtmlExportDialog
from app.ui.import_logfile_dialog import ImportLogfileDialog
from app.ui.log_panel import LogPanel
from app.ui.output_dialog import OutputDialog
from app.ui.partition_dialog import PartitionDialog
from app.ui.sector_map import SectorMapWidget
from app.ui.sector_map_window import SectorMapWindow
from app.ui.settings_dialog import SettingsDialog
from app.ui.status_panel import StatusPanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SalvageMap")
        self.resize(1100, 720)

        # Session state.
        self.source: str | None = None        # input device (/dev/sdX)
        self.output: str | None = None         # output image file
        self.logfile: str | None = None         # ddrescue logfile (progress map)
        self.volume_offset: int | None = None   # volume start; None = auto-detect
        self.volume_fs_type: str = ""            # "ntfs"/"ext"/"" of the chosen volume
        self.settings = RescueSettings()
        self._last_mapfile = None                # latest mapfile, for the pop-out
        self._map_window = None                  # lazily-created pop-out window
        self._current_phase = None               # latest targeted-recovery Phase
        self._paused = False                     # rescue paused via "Show Files"
        self._resume_launcher = None             # how to relaunch on Resume
        self._suppress_finish_dialog = False     # skip the modal when pausing
        saved_sector = config.load().get("sector_size")
        if isinstance(saved_sector, int) and saved_sector > 0:
            self.settings.sector_size = saved_sector
        self._sparse_warning_ack = False         # warned about non-sparse dest once

        # Core engine.
        self.runner = DdrescueRunner(self)
        self.targeted = TargetedRecovery(self.runner, self)

        self.sector_map = SectorMapWidget()
        self.status_panel = StatusPanel()
        self.file_tree = FileTreePanel()
        self.log_panel = LogPanel()
        self.ddrescue_view = DdrescueView()

        self._build_central()
        self._build_docks()
        self._build_toolbar()  # creates _stop_action, referenced by the menu
        self._build_menus()
        self.setStatusBar(QStatusBar())

        self.sector_map.cellHovered.connect(self._on_cell_hovered)
        self._connect_engine()
        self._update_run_controls()

    # --- layout -----------------------------------------------------------
    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        self._map_caption = QLabel("No source selected.  File ▸ Choose Block Device…")
        layout.addWidget(self._map_caption)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.sector_map)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)
        self.setCentralWidget(central)

    def _build_docks(self) -> None:
        status_dock = QDockWidget("Status", self)
        status_dock.setWidget(self.status_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, status_dock)
        self._status_dock = status_dock

        tree_dock = QDockWidget("Recovered files", self)
        # The file browser parses the $MFT and recolours against the mapfile —
        # too heavy to do while a rescue is hammering the CPU, so it is only
        # shown when the rescue is paused. While running, show a hint instead.
        self._tree_placeholder = QLabel(
            "Show Files is only available when the rescue is paused.\n\n"
            "Click “Show Files” to pause the rescue and view the recovered "
            "filesystem and per-file recovery status.\n\n"
            "Click “Resume” to continue the rescue from where it left off."
        )
        self._tree_placeholder.setAlignment(Qt.AlignCenter)
        self._tree_placeholder.setWordWrap(True)
        self._tree_placeholder.setMargin(16)
        # Tree page: the browser plus a bottom bar with the customer exports.
        # Living inside this page means the buttons only show when the files
        # view is up (i.e. the rescue is stopped/paused) — the placeholder page
        # covers the running case, so the gating comes for free.
        tree_page = QWidget()
        tree_layout = QVBoxLayout(tree_page)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(4)
        tree_layout.addWidget(self.file_tree)
        export_bar = QHBoxLayout()
        export_bar.setContentsMargins(4, 0, 4, 4)
        export_bar.addStretch(1)
        self._btn_export_txt = QPushButton("Export to TXT")
        self._btn_export_html = QPushButton("Export to HTML")
        self._btn_export_txt.setEnabled(False)
        self._btn_export_html.setEnabled(False)
        self._btn_export_txt.clicked.connect(self._export_tree_txt)
        self._btn_export_html.clicked.connect(self._export_tree_html)
        export_bar.addWidget(self._btn_export_txt)
        export_bar.addWidget(self._btn_export_html)
        tree_layout.addLayout(export_bar)

        self._tree_page = tree_page
        self._tree_stack = QStackedWidget()
        self._tree_stack.addWidget(tree_page)              # index 0: the tree
        self._tree_stack.addWidget(self._tree_placeholder)  # index 1: the hint
        tree_dock.setWidget(self._tree_stack)
        self.addDockWidget(Qt.BottomDockWidgetArea, tree_dock)
        self._tree_dock = tree_dock

        live_dock = QDockWidget("ddrescue (live)", self)
        live_dock.setWidget(self.ddrescue_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, live_dock)
        self._live_dock = live_dock

        log_dock = QDockWidget("Event Log", self)
        log_dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, log_dock)
        self._log_dock = log_dock

        # Bottom row: file tree on the left, terminal to its right (tabbed with
        # the event log), so the recovered-files browser sits in the lower-left.
        self.splitDockWidget(tree_dock, live_dock, Qt.Horizontal)
        self.tabifyDockWidget(live_dock, log_dock)
        live_dock.raise_()

    # --- menus ------------------------------------------------------------
    def _build_menus(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        self._add_action(file_menu, "Choose &Block Device…", self._open_device, "Ctrl+D")
        self._add_action(file_menu, "&Import previous logfile + image…", self._import_logfile, "Ctrl+O")
        self._add_action(file_menu, "&Export file-data Domain File…", self._export_domain_file)
        file_menu.addSeparator()
        self._add_action(file_menu, "E&xit", self.close, "Ctrl+Q")

        opt_menu = mb.addMenu("&Options")
        self._add_action(opt_menu, "Start &full-device rescue", self._run_full_device, "Ctrl+R")
        targeted_menu = opt_menu.addMenu("Targeted &Recovery")
        self._add_action(targeted_menu, "&Run full workflow (metadata + file data)", self._run_full_workflow)
        targeted_menu.addSeparator()
        self._add_action(targeted_menu, "Step &1: Recover filesystem metadata", self._run_step1)
        self._add_action(targeted_menu, "Step &2/3: Map && rescue directory structure", self._run_step23)
        self._add_action(targeted_menu, "Step &4: Image all file data", self._run_filedata)
        targeted_menu.addSeparator()
        self._add_action(targeted_menu, "Final &completeness pass (retry incomplete files)", self._run_completeness_pass)
        opt_menu.addSeparator()
        opt_menu.addAction(self._stop_action)  # shares the toolbar Stop (Ctrl+.)
        self._add_action(opt_menu, "ddrescue &Settings…", self._edit_settings)

        view_menu = mb.addMenu("&View")
        self._add_action(view_menu, "Zoom &In", self.sector_map.zoom_in, "Ctrl++")
        self._add_action(view_menu, "Zoom &Out", self.sector_map.zoom_out, "Ctrl+-")
        self._add_action(view_menu, "&Fit", self.sector_map.zoom_fit, "Ctrl+0")
        view_menu.addSeparator()
        self._add_action(view_menu, "&Pop out whole-disk map…", self._popout_sector_map, "Ctrl+M")
        view_menu.addSeparator()
        view_menu.addAction(self._status_dock.toggleViewAction())
        view_menu.addAction(self._tree_dock.toggleViewAction())
        view_menu.addAction(self._live_dock.toggleViewAction())
        view_menu.addAction(self._log_dock.toggleViewAction())

        tools_menu = mb.addMenu("&Tools")
        self._add_action(tools_menu, "Rebuild file &tree from image", lambda: self._rebuild_file_tree(announce=True))
        self._add_action(tools_menu, "&Partition scan (MBR/GPT)…", self._partition_scan)
        self._add_action(tools_menu, "&MFT Browser…", self._not_implemented)

        help_menu = mb.addMenu("&Help")
        self._add_action(help_menu, "&About", self._about)

    def _add_action(self, menu, text, slot, shortcut: str | None = None) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _build_toolbar(self) -> None:
        bar = QToolBar("Controls", self)
        bar.setMovable(False)
        self.addToolBar(bar)

        self._show_files_action = QAction("📁  Show Files", self)
        self._show_files_action.setToolTip(
            "Pause the rescue (progress is saved) and show the recovered files "
            "with their recovery status."
        )
        self._show_files_action.triggered.connect(self._show_files)
        bar.addAction(self._show_files_action)

        self._resume_action = QAction("▶  Resume", self)
        self._resume_action.setToolTip(
            "Resume the paused rescue from where it left off."
        )
        self._resume_action.triggered.connect(self._resume_rescue)
        bar.addAction(self._resume_action)

        self._stop_action = QAction("■  Stop", self)
        self._stop_action.setShortcut(QKeySequence("Ctrl+."))
        self._stop_action.setToolTip(
            "Stop ddrescue and halt the workflow (progress is saved; re-run a "
            "step to resume)."
        )
        self._stop_action.triggered.connect(self._stop_rescue)
        bar.addAction(self._stop_action)

    def _update_run_controls(self) -> None:
        """Enable/disable the Show Files / Resume / Stop buttons by state."""
        running = self.runner.is_running or self.targeted.active
        self._stop_action.setEnabled(running)
        self._show_files_action.setEnabled(running and not self._paused)
        self._resume_action.setEnabled(self._paused and not running)

    # --- engine wiring ----------------------------------------------------
    def _connect_engine(self) -> None:
        self.runner.started.connect(self.log_panel.append_command)
        self.runner.started.connect(lambda _argv: self.ddrescue_view.clear_screen())
        # `started` fires before QProcess actually launches, so is_running is
        # briefly still False — flip the controls to the running state directly.
        self.runner.started.connect(lambda _argv: self._on_rescue_started())
        self.runner.logLine.connect(self.log_panel.append_line)
        self.runner.screenUpdated.connect(self.ddrescue_view.set_screen)
        self.runner.statusUpdated.connect(self.status_panel.update_from_status)
        self.runner.mapfileUpdated.connect(self._on_mapfile_updated)
        self.runner.finished.connect(
            lambda code: self.log_panel.append_line(f"[ddrescue exited: {code}]")
        )
        self.runner.finished.connect(lambda _code: self._update_run_controls())
        self.targeted.phaseChanged.connect(self._on_phase_changed)
        self.targeted.phaseStep.connect(self.status_panel.checklist.set_active)
        self.targeted.phaseStep.connect(self._on_phase_step)
        self.targeted.workflowReset.connect(self._on_workflow_reset)
        self.targeted.planSelected.connect(self.status_panel.checklist.set_steps)
        self.targeted.domainSize.connect(self.status_panel.set_domain_size)
        self.targeted.progress.connect(self.log_panel.append_line)
        self.targeted.finished.connect(self._on_workflow_finished)
        self.targeted.finished.connect(lambda _ok, _m: self._update_run_controls())
        self.targeted.sectorSizeChanged.connect(self._on_sector_size_discovered)
        self.file_tree.prioritizeRequested.connect(self._on_prioritize)

    def _on_rescue_started(self) -> None:
        """A ddrescue process (full-device or a workflow phase) just launched."""
        self._paused = False
        self._set_tree_running_view()
        # `started` fires before is_running flips true, so force the running
        # control state directly rather than deriving it here.
        self._stop_action.setEnabled(True)
        self._show_files_action.setEnabled(True)
        self._resume_action.setEnabled(False)

    def _on_phase_step(self, phase) -> None:
        self._current_phase = phase
        self._update_run_controls()

    def _on_sector_size_discovered(self, size: int) -> None:
        """The workflow escalated --sector-size; keep and persist the new value.

        Updating ``self.settings`` means later steps and re-runs (e.g. after a
        power-cycle) start at the working size; persisting it carries that across
        app restarts too.
        """
        self.settings.sector_size = size
        self.sector_map.set_sector_size(size)
        config.save({"sector_size": size})
        self.log_panel.append_line(
            f"Sector size set to {size} B and saved for future runs."
        )

    @staticmethod
    def _mapfile_is_transient(mf, last) -> bool:
        """True if ``mf`` looks like a partial read of the logfile.

        ddrescue rewrites its logfile in place on every save, so a poll can catch
        it truncated/half-written. A real rescue domain never shrinks, so an
        empty map — or one whose domain collapsed to under half the last known
        size — is a transient we should ignore rather than blank the map for.
        """
        if not mf.blocks:
            return True
        return last is not None and mf.domain_end * 2 < last.domain_end

    def _on_mapfile_updated(self, mf) -> None:
        if self._mapfile_is_transient(mf, self._last_mapfile):
            return
        self._last_mapfile = mf
        self.sector_map.set_sector_size(self.settings.sector_size)
        self.sector_map.set_mapfile(mf)
        self.status_panel.update_from_mapfile(mf)
        # The recovered-files tree is NOT refreshed live: parsing the $MFT and
        # rolling up per-file status is too heavy to run while ddrescue is busy
        # (it froze the UI). It is built on demand via "Show Files" (paused).
        if self._map_window is not None and self._map_window.isVisible():
            self._map_window.set_sector_size(self.settings.sector_size)
            self._map_window.set_mapfile(mf)

    def _popout_sector_map(self) -> None:
        """Open (or raise) the scrollable whole-disk sector-map window."""
        if self._map_window is None:
            self._map_window = SectorMapWindow(self)
        self._map_window.set_sector_size(self.settings.sector_size)
        if self._last_mapfile is not None:
            self._map_window.set_mapfile(self._last_mapfile)
        self._map_window.show()
        self._map_window.raise_()
        self._map_window.activateWindow()

    def _on_workflow_reset(self) -> None:
        """A targeted workflow is starting: reveal and clear the checklist."""
        self.status_panel.set_checklist_visible(True)
        self.status_panel.checklist.reset()

    def _on_phase_changed(self, name: str) -> None:
        self.statusBar().showMessage(name)
        self.log_panel.append_line(f"=== {name} ===")

    def _set_tree_running_view(self) -> None:
        self._tree_stack.setCurrentWidget(self._tree_placeholder)

    def _set_tree_files_view(self) -> None:
        self._tree_stack.setCurrentWidget(self._tree_page)

    def _locate_plan(self):
        """Find the recoverable volume in the image. Returns (plan_or_None, offset).

        Tries the known volume offset first, then (for a whole-disk image whose
        volume isn't at offset 0) auto-detects it from the imaged partition
        table — exactly what an imported, pre-tree-feature image needs.
        """
        vol = self.volume_offset or 0
        plan = detect_filesystem(self.output, vol)
        if plan is not None:
            return plan, vol
        try:
            target = partition.best_recoverable(partition.scan_device(self.output))
        except OSError:
            target = None
        if target is not None:
            plan = detect_filesystem(self.output, target.start)
            if plan is not None:
                return plan, target.start
        return None, vol

    def _rebuild_file_tree(self, announce: bool = False) -> None:
        """Parse the image's filesystem metadata and (re)populate the file tree."""
        if not (self.output and os.path.exists(self.output)):
            if announce:
                QMessageBox.information(
                    self, "No image",
                    "Nothing imaged yet — recover the filesystem metadata first.",
                )
            return
        try:
            plan, vol = self._locate_plan()
            tree = plan.build_tree(self.output, vol) if plan else None
            if tree is None:
                if announce:
                    QMessageBox.information(
                        self, "No filesystem metadata in image",
                        "Couldn't find recovered filesystem metadata in this image."
                        "\n\nIf this image is from an older run, image the "
                        "filesystem metadata first (Targeted Recovery ▸ Step 1) "
                        "using the same image + logfile, then it will populate.",
                    )
                return
            self.volume_offset = vol  # remember the detected offset for later steps
        except (OSError, ValueError) as exc:
            self.log_panel.append_line(f"[file tree] could not build: {exc}")
            return
        self.file_tree.set_tree(tree)
        self._btn_export_txt.setEnabled(tree is not None)
        self._btn_export_html.setEnabled(tree is not None)
        if self._last_mapfile is not None:
            self.file_tree.refresh_status(self._last_mapfile)
        self._update_volume_status()
        self.log_panel.append_line(
            f"[file tree] {len(tree.nodes)} entries ({plan.name}) "
            f"at offset 0x{vol:X}."
        )

    def _on_workflow_finished(self, ok: bool, message: str) -> None:
        self.status_panel.checklist.mark_finished(ok)
        # When the user paused via "Show Files", the abort() that triggers this
        # is not a real end-of-workflow: skip the modal and the rebuild (the
        # Show Files handler builds and displays the tree itself).
        if self._suppress_finish_dialog:
            self._suppress_finish_dialog = False
            return
        self._rebuild_file_tree()
        self._set_tree_files_view()
        self.log_panel.append_line(f"[workflow {'OK' if ok else 'FAILED'}] {message}")
        icon = QMessageBox.information if ok else QMessageBox.warning
        icon(self, "Targeted Recovery", message)

    # --- File slots -------------------------------------------------------
    def _open_device(self) -> None:
        dev_dlg = DeviceDialog(self)
        if not dev_dlg.exec():
            return
        source = dev_dlg.selected_device()
        if not source:
            return

        # Attaching a device to an imported session: keep the image+logfile we
        # imported (don't re-prompt for an output path, which would wrongly warn
        # about overwriting the very image we're resuming from).
        if self.output and self.logfile and not self.source:
            self.source = source
            self.status_panel.set_field("source", source)
            self._refresh_caption()
            self.log_panel.append_line(
                f"Attached source {source} to the imported image/logfile."
            )
            return

        out_dlg = OutputDialog(source, self)
        if not out_dlg.exec():
            return
        output, logfile = out_dlg.result_paths()

        self.source = source
        self.output = output
        self.logfile = logfile
        self.volume_offset = None  # auto-detect from the image during the workflow
        self.volume_fs_type = ""   # detected during the workflow
        self._last_mapfile = None  # new session: don't compare domains across drives
        self._sparse_warning_ack = False
        self._refresh_caption()
        self.log_panel.append_line(
            f"Source:  {source}\nImage:   {output}\nLogfile: {logfile}"
        )
        self.status_panel.set_field("source", source)
        self.status_panel.set_field("output", output)
        self.status_panel.set_field("logfile", logfile)
        self._update_volume_status()
        # If a logfile already exists, show its current state.
        if os.path.exists(self.logfile):
            self._on_mapfile_updated(mapfile.parse(self.logfile))

    def _partition_scan(self) -> None:
        """Scan the partition table from the IMAGE (read-only; never the device).

        Requires the disk's start to have been imaged already (the workflow's
        Phase 0 does this, or run a full-device rescue first).
        """
        if not self.output:
            QMessageBox.information(
                self, "No image", "Choose a source/image first: File ▸ Choose Block Device…"
            )
            return
        if not os.path.exists(self.output):
            QMessageBox.information(
                self, "Nothing imaged yet",
                "The image file doesn't exist yet. Image the disk's start first "
                "(run the targeted workflow or a full-device rescue), then scan.",
            )
            return
        parts = partition.scan_device(self.output)  # reading the image is safe
        if not parts:
            QMessageBox.information(
                self, "Partition scan",
                "No partition table found in the imaged region — either the disk "
                "is a bare volume (offset 0) or its start isn't imaged yet.",
            )
            return
        dlg = PartitionDialog(parts, self, device=self.output)
        if dlg.exec():
            self.volume_offset = dlg.selected_offset()
            self.volume_fs_type = dlg.selected_fs_type()
            self._update_volume_status()
            fs = f" [{self.volume_fs_type}]" if self.volume_fs_type else ""
            self.log_panel.append_line(
                f"Volume offset set to 0x{self.volume_offset:X} "
                f"({self.volume_offset} bytes){fs}."
            )

    def _update_volume_status(self) -> None:
        if self.volume_offset is None:
            text = "auto-detect"
        elif self.volume_offset == 0:
            text = "whole device (offset 0)"
        else:
            text = f"0x{self.volume_offset:X} ({self.volume_offset:,} B)"
        self.status_panel.set_field("volume", text)

    def _import_logfile(self) -> None:
        """Resume from an existing logfile, auto-selecting the matching image."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import ddrescue logfile", "",
            "ddrescue logfiles (*.log *.map);;All files (*)",
        )
        if not path:
            return
        dlg = ImportLogfileDialog(self, logfile=path)
        if not dlg.exec():
            return
        image, logfile = dlg.result_paths()

        self.source = None         # imported session has no attached device yet
        self.output = image
        self.logfile = logfile
        self.volume_offset = None  # auto-detect from the image
        self.volume_fs_type = ""
        self._last_mapfile = None  # new session: don't compare domains across drives
        self._sparse_warning_ack = False
        self._refresh_caption()
        self.status_panel.set_field("source", "not attached — File ▸ Choose Block Device…")
        self.status_panel.set_field("output", image)
        self.status_panel.set_field("logfile", logfile)
        self.log_panel.append_line(f"Imported logfile: {logfile}\nImage:   {image}")
        try:
            mf = mapfile.parse(logfile)
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Could not read logfile:\n{exc}")
            return
        self._on_mapfile_updated(mf)
        self._rebuild_file_tree(announce=True)
        self._set_tree_files_view()

    # --- Options slots ----------------------------------------------------
    def _require_session(self) -> bool:
        if not (self.source and self.output and self.logfile):
            QMessageBox.information(
                self, "No source", "Choose a source device first: File ▸ Choose Block Device…"
            )
            return False
        if self.runner.is_running or self.targeted.active:
            QMessageBox.information(self, "Busy", "A rescue is already running.")
            return False
        if not os.access(self.source, os.R_OK):
            QMessageBox.critical(
                self,
                "Permission denied",
                f"Cannot read {self.source}.\n\n"
                "Reading a raw block device needs privileges. Either:\n\n"
                "  • Add yourself to the 'disk' group (recommended — keeps the\n"
                "    GUI running as your user):\n"
                "        sudo usermod -aG disk $USER\n"
                "    then log out and back in.\n\n"
                "  • Or launch the app with sudo:\n"
                "        sudo -E python3 -m app.main",
            )
            return False
        fs = non_sparse_destination(self.output)
        if fs and not self._sparse_warning_ack:
            resp = QMessageBox.warning(
                self,
                "Non-sparse destination",
                f"The output image is on a {fs.upper()} filesystem, which has NO "
                "sparse-file support.\n\n"
                "Targeted imaging writes at the data's real disk offsets, so the "
                "image will physically allocate the FULL device size in zeros "
                "(potentially hundreds of GB), filling the drive and thrashing "
                "memory.\n\n"
                "Strongly recommended: use an ext4 / xfs / btrfs destination.\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return False
            self._sparse_warning_ack = True  # don't nag again this session
        return True

    def _make_context(self) -> RecoveryContext:
        return RecoveryContext(
            infile=self.source,
            outfile=self.output,
            logfile=self.logfile,
            workdir=os.path.dirname(self.output) or ".",
            settings=self.settings,
            volume_offset=self.volume_offset,
            fs_type=self.volume_fs_type,
        )

    def _run_full_device(self) -> None:
        """Plain ddrescue of the whole source into the output image."""
        if not self._require_session():
            return
        self._resume_launcher = self._run_full_device
        self.status_panel.set_checklist_visible(False)  # phases don't apply here
        self.status_panel.set_domain_size(0)             # no domain file in a full run
        try:
            self.runner.start(
                self.source, self.output, self.logfile, self.settings
            )
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _run_full_workflow(self) -> None:
        if not self._require_session():
            return
        self._resume_launcher = self._run_full_workflow
        try:
            self.targeted.start(self._make_context(), include_filedata=True)
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _run_step1(self) -> None:
        if not self._require_session():
            return
        self._resume_launcher = self._run_step1
        try:
            self.targeted.start(self._make_context(), stop_after_mft=True)
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _run_step23(self) -> None:
        if not self._require_session():
            return
        self._resume_launcher = self._run_step23
        try:
            self.targeted.run_from_existing_mft(self._make_context())
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _run_filedata(self) -> None:
        if not self._require_session():
            return
        self._resume_launcher = self._run_filedata
        try:
            self.targeted.run_filedata_from_existing_mft(self._make_context())
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _on_prioritize(self, record_no: int) -> None:
        """Image one chosen file/folder's data first (right-click in the tree)."""
        from app.ntfs.filetree import subtree_incomplete_count, subtree_ranges
        tree = self.file_tree.tree
        if tree is None:
            return
        node = tree.nodes.get(record_no)
        name = node.name if node else "selection"
        ranges = subtree_ranges(tree, record_no)
        if not ranges:
            QMessageBox.information(
                self, "Image selection first",
                f"'{name}' has no on-disk data to image — its content was already "
                "captured with the filesystem metadata.",
            )
            return
        # Files whose extent map is incomplete can't come out whole yet. Fold in
        # the metadata that resolves them (the $MFT / Extents Overflow File /
        # extent-tree) so a rebuild after this pass can locate the missing tail.
        n_incomplete = subtree_incomplete_count(tree, record_no)
        note = ""
        if n_incomplete:
            plan, vol = self._locate_plan()
            meta = plan.metadata_ranges(self.output, vol) if plan else []
            ranges = ranges + list(meta)
            note = (f"\n\n⚠  {n_incomplete} file(s) here are too fragmented to "
                    "map fully yet — their scattered tail lives in metadata that "
                    "isn't fully recovered. This also re-images that metadata; "
                    "rebuild the file tree afterwards, then run this again to pick "
                    "up the rest.")
        total = sum(length for _start, length in ranges)
        resp = QMessageBox.question(
            self, "Image selection first",
            f"Image the data under '{name}' now?\n\n"
            f"{len(ranges):,} region(s), about {total / (1024 * 1024):,.1f} MiB, "
            f"imaged ahead of the rest of the drive (existing progress is kept).{note}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if resp != QMessageBox.Yes:
            return
        if not self._require_session():
            return
        summary = f"Imaged the data under '{name}' ({len(ranges):,} region(s))."
        self._resume_launcher = lambda: self.targeted.run_ranges(
            self._make_context(), ranges, summary)
        self.status_panel.set_checklist_visible(False)  # plan phases don't apply
        self._set_tree_running_view()
        try:
            self.targeted.run_ranges(self._make_context(), ranges, summary)
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _run_completeness_pass(self) -> None:
        """Retry every file that isn't fully recovered yet, in one final pass.

        Springboards off the incompletely-mapped report: it images the union of
        the not-yet-complete files' data ranges (ddrescue retries only their
        unfinished/bad sectors, keeping existing progress) together with the
        filesystem metadata that resolves extents — so heavily-fragmented files
        whose tail extents we couldn't place get another chance once more of the
        Extents Overflow File is recovered.
        """
        tree = self.file_tree.tree
        if tree is None or self._last_mapfile is None:
            QMessageBox.information(
                self, "Final completeness pass",
                "Recover the filesystem metadata and file data first — then this "
                "retries whatever came up short.",
            )
            return
        ranges, n_unfinished, n_unmapped = self.file_tree.incomplete_report(
            self._last_mapfile)
        plan, vol = self._locate_plan()
        meta = plan.metadata_ranges(self.output, vol) if plan else []
        ranges = list(ranges) + list(meta)
        if not ranges:
            QMessageBox.information(
                self, "Final completeness pass",
                "Every mapped file is already fully recovered — nothing left to "
                "retry.",
            )
            return
        note = ""
        if n_unmapped:
            note = (f"\n\n{n_unmapped} file(s) are too fragmented to map fully; "
                    "this re-images the catalog and extents-overflow metadata so "
                    "their missing extents may be located on a later rebuild.")
        resp = QMessageBox.question(
            self, "Final completeness pass",
            f"Retry {n_unfinished} not-yet-complete file(s) now?\n\n"
            f"{len(ranges):,} region(s) re-imaged; already-recovered sectors are "
            f"kept and only the unfinished/bad ones are re-read.{note}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if resp != QMessageBox.Yes or not self._require_session():
            return
        summary = f"Final completeness pass over {n_unfinished} file(s)."
        self._resume_launcher = lambda: self.targeted.run_ranges(
            self._make_context(), ranges, summary)
        self.status_panel.set_checklist_visible(False)
        self._set_tree_running_view()
        try:
            self.targeted.run_ranges(self._make_context(), ranges, summary)
        except (SafetyError, RuntimeError, OSError) as exc:
            QMessageBox.critical(self, "Cannot start", str(exc))

    def _export_domain_file(self) -> None:
        """Write the best file-data domain file (from the image's $MFT) to disk."""
        if not (self.output and os.path.exists(self.output)):
            QMessageBox.information(
                self, "Nothing imaged yet",
                "Image the boot sector and $MFT first (Step 1), then export.",
            )
            return
        size = get_source_size(self.source) if self.source else os.path.getsize(self.output)
        plan, vol = self._locate_plan()
        dmap = plan.filedata_domain(
            self.output, vol, size, self.settings.sector_size) if plan else None
        if dmap is None:
            QMessageBox.warning(
                self, "Export domain file",
                "Could not build a file-data domain — the filesystem metadata "
                "isn't recovered yet, or no allocated file data was found. "
                "Run Step 1 first.",
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export file-data domain file",
            (os.path.splitext(self.output)[0] + "-filedata.dmap"),
            "Domain files (*.dmap);;All files (*)",
        )
        if not path:
            return
        mapfile.write(path, dmap)
        covered = sum(b.size for b in dmap.blocks if b.status == "+")
        self.log_panel.append_line(
            f"Exported file-data domain file: {path}  ({covered:,} bytes marked +)"
        )
        QMessageBox.information(
            self, "Export domain file",
            f"Wrote {path}\n\nUse it with:\n  ddrescue -m {os.path.basename(path)} "
            f"{self.source or '<device>'} {os.path.basename(self.output)} "
            f"{os.path.basename(self.logfile or '<logfile>')}",
        )

    def _default_export_path(self, suffix: str) -> str:
        """Default export filename derived from the output image path."""
        base = os.path.splitext(self.output)[0] if self.output else "recovered-files"
        return base + suffix

    def _export_tree_txt(self) -> None:
        """Write the recovered-file tree as a plain, customer-readable TXT."""
        tree = self.file_tree.tree
        if tree is None:
            QMessageBox.information(
                self, "No files to export",
                "Show and refresh the recovered files first.",
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export recovered files to TXT",
            self._default_export_path("-recovered-files.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            n_ok, n_total = tree_export.export_txt(tree, self._last_mapfile, path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.log_panel.append_line(
            f"Exported recovered-file list (TXT): {path}  "
            f"({n_ok}/{n_total} entries recovered)"
        )
        QMessageBox.information(self, "Export to TXT", f"Wrote {path}")

    def _export_tree_html(self) -> None:
        """Write a self-contained, brandable HTML report of the file tree."""
        tree = self.file_tree.tree
        if tree is None:
            QMessageBox.information(
                self, "No files to export",
                "Show and refresh the recovered files first.",
            )
            return
        start_dir = os.path.dirname(self.output) if self.output else None
        dlg = HtmlExportDialog(self, start_dir=start_dir)
        if not dlg.exec():
            return
        logo_uri = self._logo_data_uri(dlg.logo_path())
        hide_hidden = dlg.hide_hidden_files()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export recovered files to HTML",
            self._default_export_path("-recovered-files.html"),
            "HTML files (*.html);;All files (*)",
        )
        if not path:
            return
        try:
            n_ok, n_total = tree_export.export_html(
                tree, self._last_mapfile, path, logo_data_uri=logo_uri,
                hide_hidden=hide_hidden)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.log_panel.append_line(
            f"Exported recovered-file report (HTML): {path}  "
            f"({n_ok}/{n_total} entries recovered)"
        )
        QMessageBox.information(self, "Export to HTML", f"Wrote {path}")

    def _logo_data_uri(self, logo_path: str | None) -> str | None:
        """Read ``logo_path`` and encode it as an embeddable ``data:`` URI."""
        if not logo_path:
            return None
        import base64
        import mimetypes
        try:
            with open(logo_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            QMessageBox.warning(
                self, "Logo not embedded",
                f"Could not read the logo ({exc}); exporting without it.",
            )
            return None
        mime = mimetypes.guess_type(logo_path)[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def _edit_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            self.sector_map.set_sector_size(self.settings.sector_size)
            self.log_panel.append_line(
                "ddrescue settings updated: "
                f"no_trim={self.settings.no_trim} no_scrape={self.settings.no_scrape} "
                f"timeout={self.settings.timeout} retries={self.settings.retry_passes} "
                f"skip_size={self.settings.skip_size}"
            )

    def _stop_rescue(self) -> None:
        if not (self.runner.is_running or self.targeted.active):
            return
        # Detach the workflow FIRST so the phase exit from the SIGINT below does
        # not roll on to the next phase, then signal ddrescue to save and quit.
        if self.targeted.active:
            self.targeted.abort()
        self.runner.stop()
        self._paused = False          # a full stop, not a pause — nothing to resume
        self._resume_launcher = None
        self._update_run_controls()
        self.statusBar().showMessage("Stopped — progress saved to the logfile", 3000)

    def _show_files(self) -> None:
        """Pause the running rescue and show the recovered-files browser.

        Parsing the $MFT and colouring the tree is expensive, so we only do it
        while ddrescue is paused. SIGINT makes ddrescue save its mapfile and
        exit; progress is preserved and "Resume" picks up from the logfile.
        """
        if self.runner.is_running or self.targeted.active:
            self._suppress_finish_dialog = True   # this is a pause, not an abort
            if self.targeted.active:
                self.targeted.abort()
            self.runner.stop()
            self._paused = True
            self.statusBar().showMessage("Paused — building file list…", 3000)
        self._rebuild_file_tree()
        self._set_tree_files_view()
        self._update_run_controls()

    def _resume_rescue(self) -> None:
        """Relaunch the paused command; ddrescue resumes from the logfile."""
        if not self._paused or self._resume_launcher is None:
            return
        self._paused = False
        launcher = self._resume_launcher
        self._set_tree_running_view()
        self.statusBar().showMessage("Resuming rescue…", 3000)
        launcher()

    # --- misc -------------------------------------------------------------
    def _refresh_caption(self) -> None:
        src = self.source or "(imported image — choose a device to resume imaging)"
        self._map_caption.setText(f"{src}  →  {self.output}")

    def _on_cell_hovered(self, idx: int, offset: int, status: str) -> None:
        self.statusBar().showMessage(f"cell {idx}  offset 0x{offset:X}  status '{status}'")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About SalvageMap",
            f"SalvageMap v{__version__}\n\n"
            "A GUI wrapper over GNU ddrescue with a live sector map and "
            "targeted NTFS / ext4 / HFS+ recovery.",
        )

    def _not_implemented(self) -> None:
        self.statusBar().showMessage("Not implemented yet", 2000)
