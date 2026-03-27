"""
SyncFlow - Precision file synchronization tool
Designed for Unreal Engine projects (and any large file trees)
Supports multiple destinations.
PyQt6 version with modern UI
"""

import os
import sys
import hashlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QProgressBar, QTextEdit, QFileDialog, QMessageBox, QCheckBox,
    QFrame, QScrollArea, QSizePolicy, QRadioButton, QButtonGroup
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QFont, QColor, QPalette

# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

class FileStatus(Enum):
    NEW        = "New"        # in source, missing from this dest
    MODIFIED   = "Modified"   # in both, content differs
    UNCHANGED  = "Unchanged"  # in both, identical
    DEST_ONLY  = "Dest Only"  # in dest only, not in source


@dataclass
class DestStatus:
    """Per-destination comparison result for one file."""
    dest_index: int
    status: FileStatus = FileStatus.UNCHANGED
    dst_path: Optional[Path] = None
    dst_size: int = 0
    dst_mtime: float = 0.0
    dst_hash: Optional[str] = None


@dataclass
class FileEntry:
    """A single file's comparison result across all destinations."""
    rel_path: str
    src_path: Optional[Path] = None
    src_size: int = 0
    src_mtime: float = 0.0
    src_hash: Optional[str] = None
    dest_statuses: list = field(default_factory=list)   # list[DestStatus]

    @property
    def overall_status(self) -> FileStatus:
        """Worst-case status across all destinations."""
        statuses = {ds.status for ds in self.dest_statuses}
        for s in (FileStatus.NEW, FileStatus.MODIFIED, FileStatus.DEST_ONLY):
            if s in statuses:
                return s
        return FileStatus.UNCHANGED

    @property
    def needs_sync(self) -> bool:
        return self.overall_status != FileStatus.UNCHANGED

    def dests_needing_sync(self) -> list:
        return [ds.dest_index for ds in self.dest_statuses
                if ds.status != FileStatus.UNCHANGED]

    def dest_label(self) -> str:
        """e.g. 'D1, D3' showing which destinations need this file."""
        idxs = self.dests_needing_sync()
        if not idxs:
            return ""
        return ", ".join(f"D{i+1}" for i in idxs)


# ─────────────────────────────────────────────
# Core engine
# ─────────────────────────────────────────────

def compute_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of file content (1 MB chunks for large UE assets)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except (PermissionError, OSError):
        return ""
    return h.hexdigest()


def build_file_index(root: Path, exclude_patterns: list = None) -> dict:
    """Walk a directory → {rel_path: {path, size, mtime}}.

    Args:
        root: Root directory to index
        exclude_patterns: List of directory patterns to exclude (e.g., ['Intermediate/', '.git/'])
    """
    index = {}
    exclude_patterns = exclude_patterns or []

    for dirpath, dirnames, filenames in os.walk(root):
        # Get relative path of current directory
        rel_dir = Path(dirpath).relative_to(root)
        rel_dir_str = str(rel_dir) + "/" if rel_dir != Path(".") else ""

        # Check if current directory should be excluded
        should_exclude = False
        for pattern in exclude_patterns:
            if rel_dir_str.startswith(pattern) or rel_dir_str == pattern.rstrip('/'):
                should_exclude = True
                break

        if should_exclude:
            dirnames.clear()  # Don't recurse into this directory
            continue

        # Filter out excluded subdirectories
        dirs_to_remove = []
        for dirname in dirnames:
            check_path = rel_dir_str + dirname + "/"
            for pattern in exclude_patterns:
                if check_path == pattern or check_path.startswith(pattern):
                    dirs_to_remove.append(dirname)
                    break

        for dirname in dirs_to_remove:
            dirnames.remove(dirname)

        # Process files
        for fname in filenames:
            full = Path(dirpath) / fname
            try:
                st = full.stat()
            except OSError:
                continue
            rel = str(full.relative_to(root))
            index[rel] = {"path": full, "size": st.st_size, "mtime": st.st_mtime}

    return index


def diff_trees_multi(
    src_root: Path,
    dst_roots: list,          # list[Path]
    progress_cb=None,
    cancel_event=None,
    exclude_patterns: list = None,
) -> list:
    """
    Compare source against all destinations.
    Phase 1: scan all directories in parallel.
    Phase 2: size-compare to find candidates; collect paths needing hashing.
    Phase 3: hash all candidates in parallel, finalize status.
    Returns list[FileEntry].
    """
    workers = min(32, (os.cpu_count() or 4) * 4)

    # ── Phase 1: parallel directory scan ─────────────────────────────
    if progress_cb:
        progress_cb(0, 1, "Scanning directories…")

    def safe_index(root):
        return build_file_index(root, exclude_patterns) if root.exists() else {}

    all_roots = [src_root] + list(dst_roots)
    with ThreadPoolExecutor(max_workers=len(all_roots)) as ex:
        futures = [ex.submit(lambda r, i=i: build_file_index(r, exclude_patterns) if i == 0 else safe_index(r), r)
                   for i, r in enumerate(all_roots)]
        indices = [f.result() for f in futures]

    src_index  = indices[0]
    dst_indices = indices[1:]

    # ── Phase 2: size comparison, collect hash candidates ────────────
    all_keys = set(src_index)
    for idx in dst_indices:
        all_keys |= set(idx)

    results    = []
    hash_pairs = []   # (entry, ds) where both exist and sizes match
    total      = len(all_keys)

    for i, rel in enumerate(sorted(all_keys)):
        if cancel_event and cancel_event.is_set():
            break
        if progress_cb and i % 200 == 0:
            progress_cb(i, total, f"Comparing: {rel}")

        src_info = src_index.get(rel, {})
        in_src   = rel in src_index

        entry = FileEntry(
            rel_path=rel,
            src_path=src_info.get("path"),
            src_size=src_info.get("size", 0),
            src_mtime=src_info.get("mtime", 0.0),
        )

        for dest_i, dst_index in enumerate(dst_indices):
            dst_info = dst_index.get(rel, {})
            in_dst   = rel in dst_index

            ds = DestStatus(
                dest_index=dest_i,
                dst_path=dst_info.get("path"),
                dst_size=dst_info.get("size", 0),
                dst_mtime=dst_info.get("mtime", 0.0),
            )

            if in_src and not in_dst:
                ds.status = FileStatus.NEW
            elif not in_src and in_dst:
                ds.status = FileStatus.DEST_ONLY
            elif entry.src_size != ds.dst_size:
                ds.status = FileStatus.MODIFIED
            else:
                # Same size — defer to hash phase
                ds.status = FileStatus.UNCHANGED   # tentative
                hash_pairs.append((entry, ds))

            entry.dest_statuses.append(ds)

        results.append(entry)

    # ── Phase 3: parallel hashing ─────────────────────────────────────
    if hash_pairs and not (cancel_event and cancel_event.is_set()):
        paths_needed = set()
        for entry, ds in hash_pairs:
            if entry.src_path:
                paths_needed.add(entry.src_path)
            if ds.dst_path:
                paths_needed.add(ds.dst_path)

        hash_cache: dict = {}
        done = 0
        htotal = len(paths_needed)
        if progress_cb:
            progress_cb(0, htotal, f"Hashing {htotal} file(s)…")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(compute_hash, p): p for p in paths_needed}
            for fut in as_completed(fut_map):
                if cancel_event and cancel_event.is_set():
                    break
                hash_cache[fut_map[fut]] = fut.result()
                done += 1
                if progress_cb and done % 50 == 0:
                    progress_cb(done, htotal, f"Hashing… {done}/{htotal}")

        for entry, ds in hash_pairs:
            src_h = hash_cache.get(entry.src_path, "")
            dst_h = hash_cache.get(ds.dst_path, "")
            entry.src_hash = src_h
            ds.dst_hash    = dst_h
            if src_h != dst_h:
                ds.status = FileStatus.MODIFIED

    if progress_cb:
        progress_cb(total, total, "Scan complete.")
    return results


def sync_files_multi(
    entries: list,
    dst_roots: list,          # list[Path]
    copy_new: bool = True,
    copy_modified: bool = True,
    delete_dest_only: bool = False,
    progress_cb=None,
    cancel_event=None,
    log_cb=None,
) -> dict:
    """
    Sync each file to every destination that needs it — in parallel.
    Returns stats dict per destination index.
    """
    stats   = {i: {"copied": 0, "deleted": 0, "errors": 0} for i in range(len(dst_roots))}
    lock    = threading.Lock()
    done    = [0]
    workers = min(32, (os.cpu_count() or 4) * 4)

    to_process = [e for e in entries if e.needs_sync]
    total = len(to_process)

    def process_entry(entry):
        if cancel_event and cancel_event.is_set():
            return

        copied_to    = []
        deleted_from = []

        for ds in entry.dest_statuses:
            if ds.status == FileStatus.UNCHANGED:
                continue

            dst_root   = dst_roots[ds.dest_index]
            dest_label = f"D{ds.dest_index + 1}"

            try:
                if ds.status == FileStatus.DEST_ONLY and delete_dest_only:
                    if ds.dst_path and ds.dst_path.exists():
                        ds.dst_path.unlink()
                    with lock:
                        stats[ds.dest_index]["deleted"] += 1
                    deleted_from.append(dest_label)

                elif ds.status in (FileStatus.NEW, FileStatus.MODIFIED):
                    if copy_new and ds.status == FileStatus.NEW:
                        pass
                    elif copy_modified and ds.status == FileStatus.MODIFIED:
                        pass
                    else:
                        continue

                    dest_path = dst_root / entry.rel_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry.src_path, dest_path)
                    with lock:
                        stats[ds.dest_index]["copied"] += 1
                    copied_to.append(dest_label)

            except Exception as exc:
                with lock:
                    stats[ds.dest_index]["errors"] += 1
                if log_cb:
                    log_cb(f"  ERR [{dest_label}] {entry.rel_path} → {exc}")

        with lock:
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], total, entry.rel_path)

        if copied_to and log_cb:
            tag = "NEW" if entry.overall_status == FileStatus.NEW else "UPD"
            log_cb(f"  {tag}  {entry.rel_path}  → {', '.join(copied_to)}")
        if deleted_from and log_cb:
            log_cb(f"  DEL  {entry.rel_path}  → {', '.join(deleted_from)}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(process_entry, e) for e in to_process]
        for fut in as_completed(futs):
            if cancel_event and cancel_event.is_set():
                if log_cb:
                    log_cb("⚠ Sync cancelled by user.")
                break
            fut.result()

    if progress_cb:
        progress_cb(total, total, "Done.")
    return stats


# ─────────────────────────────────────────────
# Worker Threads
# ─────────────────────────────────────────────

class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)

    def __init__(self, src_root, dst_roots, exclude_patterns=None):
        super().__init__()
        self.src_root = src_root
        self.dst_roots = dst_roots
        self.exclude_patterns = exclude_patterns or []
        self.cancel_event = threading.Event()

    def run(self):
        results = diff_trees_multi(
            self.src_root,
            self.dst_roots,
            progress_cb=self.emit_progress,
            cancel_event=self.cancel_event,
            exclude_patterns=self.exclude_patterns,
        )
        self.finished.emit(results)

    def emit_progress(self, current, total, message):
        self.progress.emit(current, total, message)

    def cancel(self):
        self.cancel_event.set()


class SyncWorker(QThread):
    progress = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, entries, dst_roots):
        super().__init__()
        self.entries = entries
        self.dst_roots = dst_roots
        self.cancel_event = threading.Event()

    def run(self):
        stats = sync_files_multi(
            self.entries,
            dst_roots=self.dst_roots,
            copy_new=True,
            copy_modified=True,
            delete_dest_only=True,
            progress_cb=self.emit_progress,
            cancel_event=self.cancel_event,
            log_cb=self.emit_log,
        )
        self.finished.emit(stats)

    def emit_progress(self, current, total, message):
        self.progress.emit(current, total, message)

    def emit_log(self, message):
        self.log.emit(message)

    def cancel(self):
        self.cancel_event.set()


# ─────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────

class DropLineEdit(QLineEdit):
    """QLineEdit that accepts folder/file drops from Finder."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if os.path.isfile(path):
                path = os.path.dirname(path)
            self.setText(path)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class DestinationRow(QWidget):
    """A single destination row with entry and buttons."""
    removed = pyqtSignal(object)

    def __init__(self, index, is_default=True, path="", is_ue_project=False, parent=None):
        super().__init__(parent)
        self.index = index
        self.is_default = is_default
        self.is_ue_project = is_ue_project

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Label
        if is_ue_project:
            label_text = f"Destination (Node {index}):"
        else:
            label_text = "Destination:" if is_default else f"Destination {index + 1}:"
        self.label = QLabel(label_text)
        self.label.setFixedWidth(180)
        self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.label)

        # Entry
        self.entry = DropLineEdit(path)
        self.entry.setObjectName("pathEntry")
        layout.addWidget(self.entry, stretch=1)

        # Browse button
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setObjectName("secondaryButton")
        self.browse_btn.clicked.connect(self.browse)
        layout.addWidget(self.browse_btn)

        # Remove button (not for default)
        if not is_default:
            self.remove_btn = QPushButton("−")
            self.remove_btn.setObjectName("dangerButton")
            self.remove_btn.setFixedWidth(40)
            self.remove_btn.clicked.connect(lambda: self.removed.emit(self))
            layout.addWidget(self.remove_btn)

    def browse(self):
        # Start with current path or home
        start_dir = self.entry.text() or str(Path.home())

        # Use Qt's custom dialog - shows files and folders, matches dark theme
        dialog = QFileDialog(self)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, False)
        dialog.setWindowTitle("Select Destination Folder")
        dialog.setDirectory(start_dir)
        dialog.setViewMode(QFileDialog.ViewMode.Detail)

        if dialog.exec():
            folder = dialog.selectedFiles()[0]
            self.entry.setText(folder)

    def get_path(self):
        return self.entry.text().strip()

    def set_label(self, text):
        self.label.setText(text)


class FileSyncApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SyncFlow v1.0.1 — Precision Sync")
        self.resize(1000, 750)

        self._entries = []
        self._dest_rows = []
        self._scan_worker = None
        self._sync_worker = None
        self._is_ue_project = False
        self._exclude_patterns = []
        self._loading_settings = True  # Start as True to prevent saves during setup

        self._setup_ui()
        self._apply_styles()
        self._load_settings()  # This will set _loading_settings to False when done

    def _setup_ui(self):
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)

        # ── Folder selection ──────────────────────────────────────
        # Source row
        src_layout = QHBoxLayout()
        src_layout.setSpacing(12)
        self.src_label = QLabel("Source:")
        self.src_label.setFixedWidth(180)
        self.src_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        src_layout.addWidget(self.src_label)

        self.src_entry = DropLineEdit()
        self.src_entry.setObjectName("pathEntry")
        self.src_entry.textChanged.connect(self.on_source_changed)  # Detect UE on text change
        src_layout.addWidget(self.src_entry, stretch=1)

        src_browse_btn = QPushButton("Browse…")
        src_browse_btn.setObjectName("secondaryButton")
        src_browse_btn.clicked.connect(self.browse_source)
        src_layout.addWidget(src_browse_btn)

        main_layout.addLayout(src_layout)

        # Destinations container
        self.dest_container = QWidget()
        self.dest_layout = QVBoxLayout(self.dest_container)
        self.dest_layout.setContentsMargins(0, 0, 0, 0)
        self.dest_layout.setSpacing(8)
        main_layout.addWidget(self.dest_container)

        # Add first destination
        self.add_destination_row()

        # Add destination button
        add_dest_layout = QHBoxLayout()
        add_dest_layout.addStretch()
        self.add_dest_btn = QPushButton("+ Add Destination")
        self.add_dest_btn.setObjectName("secondaryButton")
        self.add_dest_btn.clicked.connect(lambda: self.add_destination_row())
        add_dest_layout.addWidget(self.add_dest_btn)
        main_layout.addLayout(add_dest_layout)

        # ── Direction ──────────────────────────────────────────────
        dir_layout = QHBoxLayout()
        dir_layout.setSpacing(12)
        dir_label = QLabel("Direction:")
        dir_label.setFixedWidth(180)
        dir_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        dir_layout.addWidget(dir_label)

        self._dir_group = QButtonGroup(self)
        self._dir_fwd = QRadioButton("→  Src → Dst")
        self._dir_rev = QRadioButton("←  Dst → Src")
        self._dir_fwd.setChecked(True)
        self._dir_group.addButton(self._dir_fwd, 0)
        self._dir_group.addButton(self._dir_rev, 1)
        self._dir_fwd.toggled.connect(self._on_direction_change)
        dir_layout.addWidget(self._dir_fwd)
        dir_layout.addWidget(self._dir_rev)
        dir_layout.addStretch()
        main_layout.addLayout(dir_layout)

        # ── Action buttons ─────────────────────────────────────────
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)

        self.scan_btn = QPushButton("Scan / Compare")
        self.scan_btn.setObjectName("primaryButton")
        self.scan_btn.clicked.connect(self.start_scan)
        action_layout.addWidget(self.scan_btn)

        self.sync_btn = QPushButton("Sync →")
        self.sync_btn.setObjectName("successButton")
        self.sync_btn.setEnabled(False)
        self.sync_btn.clicked.connect(self.start_sync)
        action_layout.addWidget(self.sync_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerButton")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_operation)
        action_layout.addWidget(self.cancel_btn)

        action_layout.addStretch()
        main_layout.addLayout(action_layout)

        # ── Progress ───────────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        main_layout.addWidget(self.progress_label)

        # ── Filter bar ─────────────────────────────────────────────
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(12)

        filter_label = QLabel("Filter:")
        filter_layout.addWidget(filter_label)

        self.filter_entry = QLineEdit()
        self.filter_entry.setObjectName("pathEntry")
        self.filter_entry.setPlaceholderText("Type to filter files...")
        self.filter_entry.textChanged.connect(self.apply_filter)
        self.filter_entry.setFixedWidth(300)
        filter_layout.addWidget(self.filter_entry)

        self.show_new_cb = QCheckBox("New")
        self.show_new_cb.setChecked(True)
        self.show_new_cb.stateChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.show_new_cb)

        self.show_modified_cb = QCheckBox("Modified")
        self.show_modified_cb.setChecked(True)
        self.show_modified_cb.stateChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.show_modified_cb)

        self.show_unchanged_cb = QCheckBox("Unchanged")
        self.show_unchanged_cb.setChecked(False)
        self.show_unchanged_cb.stateChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.show_unchanged_cb)

        self.show_destonly_cb = QCheckBox("Dest Only")
        self.show_destonly_cb.setChecked(True)
        self.show_destonly_cb.stateChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.show_destonly_cb)

        filter_layout.addStretch()
        main_layout.addLayout(filter_layout)

        # ── File tree ──────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Status", "Relative Path", "Src Size", "Dst Size", "Sync To", "Note"])
        self.tree.setColumnWidth(0, 90)
        self.tree.setColumnWidth(1, 350)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 70)
        self.tree.setColumnWidth(5, 150)
        self.tree.setAlternatingRowColors(True)
        main_layout.addWidget(self.tree, stretch=1)

        # ── Log ────────────────────────────────────────────────────
        log_label = QLabel("Log")
        log_label.setObjectName("sectionLabel")
        main_layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.setObjectName("logText")
        main_layout.addWidget(self.log_text)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QWidget {
                background-color: #1e1e1e;
                color: #e8e8e8;
                font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
                font-size: 11pt;
            }
            QLabel {
                color: #9d9d9d;
                font-size: 11pt;
            }
            QLabel#sectionLabel {
                color: #e8e8e8;
                font-size: 11pt;
                font-weight: bold;
            }
            QLineEdit {
                background-color: #2d2d30;
                border: 1px solid #3e3e42;
                border-radius: 6px;
                padding: 8px 12px;
                color: #e8e8e8;
                font-size: 11pt;
                selection-background-color: #007aff;
            }
            QLineEdit:focus {
                border: 1px solid #007aff;
                outline: none;
            }
            QLineEdit#pathEntry {
                background-color: #2d2d30;
            }
            QPushButton {
                background-color: #007aff;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-size: 11pt;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #0A84FF;
            }
            QPushButton:pressed {
                background-color: #006ED9;
            }
            QPushButton:disabled {
                background-color: #3a3a3c;
                color: #636366;
            }
            QPushButton#primaryButton {
                background-color: #007aff;
            }
            QPushButton#primaryButton:hover {
                background-color: #0A84FF;
            }
            QPushButton#secondaryButton {
                background-color: #2d2d30;
                color: #007aff;
                border: 1px solid #3e3e42;
            }
            QPushButton#secondaryButton:hover {
                background-color: #3a3a3c;
            }
            QPushButton#successButton {
                background-color: #32d74b;
            }
            QPushButton#successButton:hover {
                background-color: #30d158;
            }
            QPushButton#dangerButton {
                background-color: #ff453a;
            }
            QPushButton#dangerButton:hover {
                background-color: #ff5c51;
            }
            QTreeWidget {
                background-color: #252526;
                border: 1px solid #3e3e42;
                border-radius: 8px;
                padding: 4px;
                font-size: 10pt;
                color: #e8e8e8;
            }
            QTreeWidget::item {
                padding: 4px;
                border-bottom: 1px solid #2d2d30;
            }
            QTreeWidget::item:selected {
                background-color: rgba(10, 132, 255, 0.25);
                color: #0A84FF;
            }
            QTreeWidget::item:hover {
                background-color: #2d2d30;
            }
            QHeaderView::section {
                background-color: #252526;
                color: #9d9d9d;
                border: none;
                border-bottom: 1px solid #3e3e42;
                padding: 8px 4px;
                font-weight: 600;
                font-size: 10pt;
            }
            QProgressBar {
                border: none;
                border-radius: 4px;
                background-color: #3a3a3c;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #007aff;
                border-radius: 4px;
            }
            QTextEdit {
                background-color: #252526;
                border: 1px solid #3e3e42;
                border-radius: 8px;
                padding: 8px;
                color: #e8e8e8;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 9pt;
                selection-background-color: #007aff;
            }
            QTextEdit#logText {
                background-color: #1e1e1e;
            }
            QCheckBox {
                spacing: 8px;
                color: #e8e8e8;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #3e3e42;
                background-color: #2d2d30;
            }
            QCheckBox::indicator:hover {
                border-color: #007aff;
            }
            QCheckBox::indicator:checked {
                background-color: #007aff;
                border-color: #007aff;
                image: url(data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTIiIGhlaWdodD0iMTIiIHZpZXdCb3g9IjAgMCAxMiAxMiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEwIDNMNC41IDguNUwyIDYiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPgo=);
            }
            QRadioButton {
                spacing: 8px;
                color: #e8e8e8;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border-radius: 9px;
                border: 1px solid #3e3e42;
                background-color: #2d2d30;
            }
            QRadioButton::indicator:hover {
                border-color: #007aff;
            }
            QRadioButton::indicator:checked {
                background-color: #007aff;
                border-color: #007aff;
            }
            QScrollBar:vertical {
                background: #1e1e1e;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #3e3e42;
                border-radius: 6px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #4e4e52;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background: #1e1e1e;
                height: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: #3e3e42;
                border-radius: 6px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #4e4e52;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
        """)

    # ── Direction ────────────────────────────────────────────────

    def _on_direction_change(self):
        rev = self._dir_rev.isChecked()
        self.sync_btn.setText("← Sync" if rev else "Sync →")
        self.save_settings()

    def get_effective_src_dsts(self):
        """Return (effective_src: Path, effective_dsts: list[Path]) respecting direction."""
        src = self.src_entry.text().strip()
        dst_paths = self.get_dest_paths()
        if self._dir_rev.isChecked() and dst_paths:
            return dst_paths[0], [Path(src)]
        return Path(src), dst_paths

    # ── Destination management ───────────────────────────────────

    def add_destination_row(self, path=""):
        index = len(self._dest_rows)
        is_default = index == 0
        row = DestinationRow(index, is_default, path, self._is_ue_project, self)
        row.removed.connect(self.remove_destination_row)
        row.entry.textChanged.connect(self.save_settings)
        self.dest_layout.addWidget(row)
        self._dest_rows.append(row)
        self.save_settings()

    def remove_destination_row(self, row):
        row.deleteLater()
        self._dest_rows.remove(row)
        self.renumber_destinations()
        self.save_settings()

    def renumber_destinations(self):
        for i, row in enumerate(self._dest_rows):
            if self._is_ue_project:
                text = f"Destination (Node {i}):"
            else:
                text = "Destination:" if i == 0 else f"Destination {i + 1}:"
            row.set_label(text)
            row.index = i
            row.is_ue_project = self._is_ue_project

    def get_dest_paths(self):
        """Return list of non-empty destination Path objects."""
        paths = []
        for row in self._dest_rows:
            p = row.get_path()
            if p:
                paths.append(Path(p))
        return paths

    # ── Browse ───────────────────────────────────────────────────

    def browse_source(self):
        # Start with current path or home
        start_dir = self.src_entry.text() or str(Path.home())

        # Use Qt's custom dialog - shows files and folders, matches dark theme
        dialog = QFileDialog(self)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, False)
        dialog.setWindowTitle("Select Source Folder")
        dialog.setDirectory(start_dir)
        dialog.setViewMode(QFileDialog.ViewMode.Detail)

        if dialog.exec():
            folder = dialog.selectedFiles()[0]
            self.src_entry.setText(folder)
            # Detection happens via textChanged signal
            self.save_settings()

    def on_source_changed(self):
        """Called when source path changes (browse, drag & drop, or manual entry)."""
        folder = self.src_entry.text().strip()
        if folder and Path(folder).is_dir():
            self.detect_ue_project(folder)
        # Save settings after any change (including clearing the field)
        self.save_settings()

    def detect_ue_project(self, folder_path):
        """Detect if the folder is an Unreal Engine project."""
        folder = Path(folder_path)

        # Look for .uproject files
        uproject_files = list(folder.glob("*.uproject"))

        if uproject_files:
            # This is a UE project
            self._is_ue_project = True
            self._exclude_patterns = [
                "Intermediate/",
                "Saved/",
                "DerivedDataCache/",
                "Binaries/",
                ".git/",
                ".vs/",
                ".vscode/",
            ]
            self.src_label.setText("Source (Editor / Control):")
            self.log(f"✓ Unreal Engine project detected: {uproject_files[0].name}")
            self.log(f"  Excluding: {', '.join(self._exclude_patterns)}")
        else:
            # Not a UE project
            self._is_ue_project = False
            self._exclude_patterns = []
            self.src_label.setText("Source:")

        # Update destination labels
        self.renumber_destinations()

    # ── Scan ─────────────────────────────────────────────────────

    def start_scan(self):
        src = self.src_entry.text().strip()
        if not src:
            QMessageBox.warning(self, "Missing source", "Please select a source folder.")
            return
        if not Path(src).is_dir():
            QMessageBox.critical(self, "Invalid source", f"Source folder not found:\n{src}")
            return

        dst_paths = self.get_dest_paths()
        if not dst_paths:
            QMessageBox.warning(self, "No destinations", "Add at least one destination folder.")
            return

        rev = self._dir_rev.isChecked()
        effective_src, effective_dsts = self.get_effective_src_dsts()
        direction_label = "← Dst→Src" if rev else "→ Src→Dst"

        # Ensure all effective destinations exist
        for dp in effective_dsts:
            if not dp.exists():
                reply = QMessageBox.question(
                    self, "Create destination?",
                    f"Destination does not exist:\n{dp}\n\nCreate it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply == QMessageBox.StandardButton.Yes:
                    dp.mkdir(parents=True)
                else:
                    return

        self.set_busy(True)
        self.tree.clear()
        self.log(f"── Scan started {datetime.now():%Y-%m-%d %H:%M:%S} ({direction_label}) ──")
        self.log(f"   From  : {effective_src}")
        for i, dp in enumerate(effective_dsts):
            self.log(f"   To D{i+1} : {dp}")

        self._scan_worker = ScanWorker(effective_src, effective_dsts, self._exclude_patterns)
        self._scan_worker.progress.connect(self.update_progress)
        self._scan_worker.finished.connect(self.scan_done)
        self._scan_worker.start()

    def scan_done(self, results):
        self._entries = results
        self.apply_filter()
        self.set_busy(False)

        new = sum(1 for e in results if e.overall_status == FileStatus.NEW)
        modified = sum(1 for e in results if e.overall_status == FileStatus.MODIFIED)
        unchanged = sum(1 for e in results if e.overall_status == FileStatus.UNCHANGED)
        dest_only = sum(1 for e in results if e.overall_status == FileStatus.DEST_ONLY)

        self.log(f"   New:{new}  Modified:{modified}  Unchanged:{unchanged}  Dest-only:{dest_only}")
        self.log("── Scan complete ──")

        needs_sync = new + modified + dest_only
        self.sync_btn.setEnabled(needs_sync > 0)
        self.save_settings()

    # ── Sync ─────────────────────────────────────────────────────

    def start_sync(self):
        to_sync = [e for e in self._entries if e.needs_sync]
        if not to_sync:
            QMessageBox.information(self, "Nothing to sync", "All destinations are up to date.")
            return

        rev = self._dir_rev.isChecked()
        _, effective_dsts = self.get_effective_src_dsts()
        if not effective_dsts:
            QMessageBox.warning(self, "No destinations", "No destination folders configured.")
            return

        direction_label = "← Dst→Src" if rev else "→ Src→Dst"
        del_count = sum(1 for e in to_sync if e.overall_status == FileStatus.DEST_ONLY)
        dest_labels = [f"D{i+1}: {p}" for i, p in enumerate(effective_dsts)]
        msg = (
            f"Direction: {direction_label}\n"
            f"About to sync {len(to_sync)} file(s) to "
            f"{len(effective_dsts)} destination(s):\n\n"
            + "\n".join(dest_labels)
        )
        if del_count:
            msg += f"\n\n⚠ {del_count} file(s) will be DELETED from destination(s)."
        msg += "\n\nEach file is only copied to destinations that need it."

        reply = QMessageBox.question(
            self, "Confirm Sync", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.set_busy(True)
        self.log(f"── Sync started {datetime.now():%Y-%m-%d %H:%M:%S} ({direction_label}) ──")

        self._sync_worker = SyncWorker(self._entries, effective_dsts)
        self._sync_worker.progress.connect(self.update_progress)
        self._sync_worker.log.connect(self.log)
        self._sync_worker.finished.connect(self.sync_done)
        self._sync_worker.start()

    def sync_done(self, stats):
        self.set_busy(False)

        total_copied = sum(s["copied"] for s in stats.values())
        total_deleted = sum(s["deleted"] for s in stats.values())
        total_errors = sum(s["errors"] for s in stats.values())

        self.log("── Sync complete ──")
        for i, s in stats.items():
            self.log(f"   D{i+1}: copied={s['copied']}  deleted={s['deleted']}  errors={s['errors']}")

        if total_errors:
            QMessageBox.warning(
                self, "Sync finished with errors",
                f"Sync completed with errors.\n\n"
                f"Total copied : {total_copied}\n"
                f"Total deleted: {total_deleted}\n"
                f"Total errors : {total_errors}\n\n"
                "Check the log for details."
            )
        else:
            QMessageBox.information(
                self, "Sync complete",
                f"Sync finished successfully!\n\n"
                f"Files copied/updated : {total_copied}\n"
                f"Files deleted        : {total_deleted}"
            )

        # Re-scan
        self.start_scan()

    # ── Cancel ───────────────────────────────────────────────────

    def cancel_operation(self):
        if self._scan_worker:
            self._scan_worker.cancel()
        if self._sync_worker:
            self._sync_worker.cancel()

    # ── UI helpers ───────────────────────────────────────────────

    def set_busy(self, busy):
        self.scan_btn.setEnabled(not busy)
        self.sync_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)

    def update_progress(self, current, total, message):
        if total > 0:
            pct = int((current / total) * 100)
            self.progress_bar.setValue(pct)
        self.progress_label.setText(message)

    def log(self, message):
        self.log_text.append(message)

    def apply_filter(self):
        self.tree.clear()
        text_filter = self.filter_entry.text().lower()

        show_map = {
            FileStatus.NEW: self.show_new_cb.isChecked(),
            FileStatus.MODIFIED: self.show_modified_cb.isChecked(),
            FileStatus.UNCHANGED: self.show_unchanged_cb.isChecked(),
            FileStatus.DEST_ONLY: self.show_destonly_cb.isChecked(),
        }

        for entry in self._entries:
            status = entry.overall_status
            if not show_map.get(status, True):
                continue
            if text_filter and text_filter not in entry.rel_path.lower():
                continue

            note = ""
            if status == FileStatus.MODIFIED:
                has_hash = any(ds.dst_hash for ds in entry.dest_statuses)
                note = "hash differs" if (has_hash or entry.src_hash) else "size/mtime differs"

            # Dst size: use first destination that has the file
            dst_size = 0
            for ds in entry.dest_statuses:
                if ds.dst_size > 0:
                    dst_size = ds.dst_size
                    break

            item = QTreeWidgetItem([
                status.value,
                entry.rel_path,
                fmt_size(entry.src_size),
                fmt_size(dst_size),
                entry.dest_label() or "—",
                note,
            ])

            # Color code - dark theme optimized
            colors = {
                FileStatus.NEW: QColor("#32d74b"),        # Bright green for dark mode
                FileStatus.MODIFIED: QColor("#ffd60a"),   # Bright yellow for dark mode
                FileStatus.UNCHANGED: QColor("#9d9d9d"),  # Muted gray
                FileStatus.DEST_ONLY: QColor("#ff453a"),  # Bright red for dark mode
            }
            color = colors.get(status, QColor("#1d1d1f"))
            for col in range(6):
                item.setForeground(col, color)

            self.tree.addTopLevelItem(item)

    # ── Settings ─────────────────────────────────────────────────

    def settings_path(self):
        return Path.home() / ".filesync_settings.json"

    def save_settings(self):
        # Don't save if we're currently loading settings
        if self._loading_settings:
            return

        data = {
            "src": self.src_entry.text(),
            "destinations": [row.get_path() for row in self._dest_rows],
            "direction": "rev" if self._dir_rev.isChecked() else "fwd",
        }
        try:
            settings_file = self.settings_path()
            settings_file.write_text(json.dumps(data, indent=2), encoding='utf-8')
            print(f"[OK] Settings saved: {data}")
        except Exception as e:
            print(f"[ERROR] Saving settings: {e}")

    def load_settings(self):
        self._loading_settings = True  # Prevent saves during load
        try:
            settings_file = self.settings_path()
            if not settings_file.exists():
                print("[INFO] No settings file found - first run")
                return

            data = json.loads(settings_file.read_text(encoding='utf-8'))
            print(f"[LOAD] Settings: {data}")

            # Load source path
            src = data.get("src", "")
            if src:
                self.src_entry.setText(src)

            # Load destination paths
            destinations = data.get("destinations", [""])
            if destinations:
                # Set first destination
                if destinations[0]:
                    self._dest_rows[0].entry.setText(destinations[0])
                # Add additional destinations
                for path in destinations[1:]:
                    if path:  # Only add non-empty paths
                        self.add_destination_row(path=path)

            if data.get("direction") == "rev":
                self._dir_rev.setChecked(True)
            else:
                self._dir_fwd.setChecked(True)

            print("[OK] Settings loaded successfully")
        except Exception as e:
            print(f"[ERROR] Loading settings: {e}")
        finally:
            self._loading_settings = False  # Re-enable saves

    def _load_settings(self):
        # Delay loading to ensure UI is ready
        QTimer.singleShot(100, self.load_settings)


def fmt_size(n: float) -> str:
    if n == 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Use Fusion style for better cross-platform appearance
    window = FileSyncApp()
    window.show()
    sys.exit(app.exec())
