"""
FileSync - Precision file synchronization tool
Designed for Unreal Engine projects (and any large file trees)
Supports multiple destinations.
GUI: Kivy
"""

import os
import sys
import hashlib
import shutil
import threading
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── Kivy: suppress verbose startup log ────────────────────────────
os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')
os.environ.setdefault('KIVY_LOG_LEVEL', 'warning')

import kivy
kivy.require('2.0.0')

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.checkbox import CheckBox
from kivy.uix.progressbar import ProgressBar
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.properties import StringProperty, ListProperty
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp, sp

# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

class FileStatus(Enum):
    NEW        = "New"
    MODIFIED   = "Modified"
    UNCHANGED  = "Unchanged"
    DEST_ONLY  = "Dest Only"


@dataclass
class DestStatus:
    dest_index: int
    status: FileStatus = FileStatus.UNCHANGED
    dst_path: Optional[Path] = None
    dst_size: int = 0
    dst_mtime: float = 0.0
    dst_hash: Optional[str] = None


@dataclass
class FileEntry:
    rel_path: str
    src_path: Optional[Path] = None
    src_size: int = 0
    src_mtime: float = 0.0
    src_hash: Optional[str] = None
    dest_statuses: list = field(default_factory=list)

    @property
    def overall_status(self) -> FileStatus:
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
        idxs = self.dests_needing_sync()
        if not idxs:
            return ""
        return ", ".join(f"D{i+1}" for i in idxs)


# ─────────────────────────────────────────────
# Core engine
# ─────────────────────────────────────────────

_IO_WORKERS = min(16, (os.cpu_count() or 4) * 2)

try:
    import xxhash as _xxhash
    def compute_hash(path: Path, chunk_size: int = 1 << 20) -> str:
        h = _xxhash.xxh3_64()
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
    _HASH_ALGO = "xxh3_64"
except ImportError:
    def compute_hash(path: Path, chunk_size: int = 1 << 20) -> str:
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
    _HASH_ALGO = "sha256"


def build_file_index(root: Path, excludes: list) -> dict:
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in excludes and not d.startswith(".")
        ]
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
    dst_roots: list,
    excludes: list,
    use_hash: bool,
    progress_cb=None,
    cancel_event=None,
) -> list:
    if progress_cb:
        progress_cb(0, 1, "Scanning directories…")

    scan_targets = [(0, src_root)] + [(i + 1, d) for i, d in enumerate(dst_roots)]
    scan_results = [None] * len(scan_targets)

    def _scan(idx, root):
        return idx, build_file_index(root, excludes) if root.exists() else {}

    with ThreadPoolExecutor(max_workers=min(len(scan_targets), _IO_WORKERS)) as ex:
        for fut in as_completed(ex.submit(_scan, idx, root) for idx, root in scan_targets):
            if cancel_event and cancel_event.is_set():
                break
            idx, result = fut.result()
            scan_results[idx] = result

    src_index   = scan_results[0] or {}
    dst_indices = [scan_results[i + 1] or {} for i in range(len(dst_roots))]

    all_keys = set(src_index)
    for idx in dst_indices:
        all_keys |= set(idx)
    total = len(all_keys)

    hash_cache: dict = {}

    if use_hash:
        if progress_cb:
            progress_cb(0, total, "Pre-hashing files…")

        paths_to_hash: set = set()
        for rel in all_keys:
            src_info = src_index.get(rel)
            if not src_info:
                continue
            for dst_index in dst_indices:
                dst_info = dst_index.get(rel)
                if dst_info and dst_info["size"] == src_info["size"]:
                    paths_to_hash.add(src_info["path"])
                    paths_to_hash.add(dst_info["path"])

        n_hash = len(paths_to_hash)
        done_hash = 0
        with ThreadPoolExecutor(max_workers=_IO_WORKERS) as ex:
            futures = {ex.submit(compute_hash, p): p for p in paths_to_hash}
            for fut in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    break
                path = futures[fut]
                hash_cache[path] = fut.result()
                done_hash += 1
                if progress_cb and done_hash % 50 == 0:
                    progress_cb(done_hash, n_hash,
                                f"Hashing… ({done_hash}/{n_hash}, algo={_HASH_ALGO})")

    if progress_cb:
        progress_cb(0, total, "Comparing…")

    results = []
    for i, rel in enumerate(sorted(all_keys)):
        if cancel_event and cancel_event.is_set():
            break
        if progress_cb and i % 500 == 0:
            progress_cb(i, total, f"Comparing… ({i}/{total})")

        src_info = src_index.get(rel, {})
        in_src   = rel in src_index
        src_path = src_info.get("path")

        entry = FileEntry(
            rel_path=rel,
            src_path=src_path,
            src_size=src_info.get("size", 0),
            src_mtime=src_info.get("mtime", 0.0),
            src_hash=hash_cache.get(src_path) if src_path else None,
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
            else:
                if entry.src_size != ds.dst_size:
                    ds.status = FileStatus.MODIFIED
                elif use_hash:
                    src_h = hash_cache.get(entry.src_path, "")
                    dst_h = hash_cache.get(ds.dst_path, "")
                    ds.dst_hash = dst_h or None
                    ds.status = (FileStatus.MODIFIED
                                 if src_h != dst_h else FileStatus.UNCHANGED)
                else:
                    ds.status = (FileStatus.MODIFIED
                                 if abs(entry.src_mtime - ds.dst_mtime) > 2
                                 else FileStatus.UNCHANGED)

            entry.dest_statuses.append(ds)

        results.append(entry)

    if progress_cb:
        progress_cb(total, total, "Scan complete.")
    return results


def sync_files_multi(
    entries: list,
    dst_roots: list,
    copy_new: bool = True,
    copy_modified: bool = True,
    delete_dest_only: bool = False,
    progress_cb=None,
    cancel_event=None,
    log_cb=None,
) -> dict:
    stats      = {i: {"copied": 0, "deleted": 0, "errors": 0} for i in range(len(dst_roots))}
    stats_lock = threading.Lock()
    done_count = 0

    to_process = [e for e in entries if e.needs_sync]
    total      = len(to_process)

    def _sync_one(entry: FileEntry):
        nonlocal done_count
        if cancel_event and cancel_event.is_set():
            return

        copied_to    = []
        deleted_from = []
        local        = {i: {"copied": 0, "deleted": 0, "errors": 0} for i in range(len(dst_roots))}

        for ds in entry.dest_statuses:
            if ds.status == FileStatus.UNCHANGED:
                continue

            dst_root   = dst_roots[ds.dest_index]
            dest_label = f"D{ds.dest_index + 1}"

            try:
                if ds.status == FileStatus.DEST_ONLY and delete_dest_only:
                    if ds.dst_path and ds.dst_path.exists():
                        ds.dst_path.unlink()
                    local[ds.dest_index]["deleted"] += 1
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
                    local[ds.dest_index]["copied"] += 1
                    copied_to.append(dest_label)

            except Exception as exc:
                local[ds.dest_index]["errors"] += 1
                if log_cb:
                    log_cb(f"  ERR [{dest_label}] {entry.rel_path} → {exc}")

        if copied_to and log_cb:
            tag = "NEW" if entry.overall_status == FileStatus.NEW else "UPD"
            log_cb(f"  {tag}  {entry.rel_path}  → {', '.join(copied_to)}")
        if deleted_from and log_cb:
            log_cb(f"  DEL  {entry.rel_path}  → {', '.join(deleted_from)}")

        with stats_lock:
            for i in range(len(dst_roots)):
                for k in ("copied", "deleted", "errors"):
                    stats[i][k] += local[i][k]
            done_count += 1
            if progress_cb:
                progress_cb(done_count, total, entry.rel_path)

    with ThreadPoolExecutor(max_workers=_IO_WORKERS) as ex:
        futures = [ex.submit(_sync_one, entry) for entry in to_process]
        for fut in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                if log_cb:
                    log_cb("⚠ Sync cancelled by user.")
                break
            fut.result()

    if progress_cb:
        progress_cb(total, total, "Done.")
    return stats


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n == 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ─────────────────────────────────────────────
# GUI — Kivy
# ─────────────────────────────────────────────

UE_DEFAULT_EXCLUDES = [
    "intermediate", "saved", "deriveddatacache",
    "binaries", ".vs", ".git", "__pycache__",
]

PALETTE = {
    "bg":      (0.118, 0.118, 0.180, 1),
    "surface": (0.165, 0.165, 0.243, 1),
    "border":  (0.227, 0.227, 0.353, 1),
    "accent":  (0.486, 0.620, 0.973, 1),
    "text":    (0.804, 0.839, 0.957, 1),
    "subtext": (0.651, 0.678, 0.784, 1),
    "green":   (0.651, 0.890, 0.631, 1),
    "yellow":  (0.976, 0.886, 0.686, 1),
    "red":     (0.953, 0.545, 0.659, 1),
    "blue":    (0.537, 0.706, 0.980, 1),
    "mantle":  (0.094, 0.094, 0.145, 1),
    "orange":  (0.980, 0.702, 0.529, 1),
}

def C(key):
    return PALETTE[key]

STATUS_COLORS = {
    FileStatus.NEW:       C("green"),
    FileStatus.MODIFIED:  C("yellow"),
    FileStatus.UNCHANGED: C("subtext"),
    FileStatus.DEST_ONLY: C("red"),
}

KV = """
<FileRow>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(26)
    spacing: dp(1)
    canvas.before:
        Color:
            rgba: (0.165, 0.165, 0.243, 1)
        Rectangle:
            pos: self.pos
            size: self.size
    Label:
        text: root.status_text
        size_hint_x: None
        width: dp(90)
        color: root.status_color
        font_size: sp(9)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
    Label:
        text: root.path_text
        size_hint_x: 1
        color: (0.804, 0.839, 0.957, 1)
        font_size: sp(9)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
    Label:
        text: root.src_size_text
        size_hint_x: None
        width: dp(80)
        color: (0.804, 0.839, 0.957, 1)
        font_size: sp(9)
        halign: 'right'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
    Label:
        text: root.dst_size_text
        size_hint_x: None
        width: dp(80)
        color: (0.804, 0.839, 0.957, 1)
        font_size: sp(9)
        halign: 'right'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
    Label:
        text: root.sync_to_text
        size_hint_x: None
        width: dp(90)
        color: (0.804, 0.839, 0.957, 1)
        font_size: sp(9)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
    Label:
        text: root.note_text
        size_hint_x: None
        width: dp(140)
        color: (0.651, 0.678, 0.784, 1)
        font_size: sp(9)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding: [dp(4), 0]
"""
Builder.load_string(KV)


class FileRow(RecycleDataViewBehavior, BoxLayout):
    status_text   = StringProperty('')
    path_text     = StringProperty('')
    src_size_text = StringProperty('')
    dst_size_text = StringProperty('')
    sync_to_text  = StringProperty('')
    note_text     = StringProperty('')
    status_color  = ListProperty([1, 1, 1, 1])

    def refresh_view_attrs(self, rv, index, data):
        self.status_text   = data.get('status_text', '')
        self.path_text     = data.get('path_text', '')
        self.src_size_text = data.get('src_size_text', '')
        self.dst_size_text = data.get('dst_size_text', '')
        self.sync_to_text  = data.get('sync_to_text', '')
        self.note_text     = data.get('note_text', '')
        self.status_color  = data.get('status_color', [1, 1, 1, 1])
        return super().refresh_view_attrs(rv, index, data)


def _set_bg(widget, color_key):
    with widget.canvas.before:
        Color(*C(color_key))
        rect = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(
        pos=lambda *_: setattr(rect, 'pos', widget.pos),
        size=lambda *_: setattr(rect, 'size', widget.size),
    )


class FileSyncApp(App):

    def build(self):
        self.title = 'FileSync — Precision Sync'
        Window.size = (1150, 800)
        Window.clearcolor = C('bg')
        Window.bind(on_resize=self._enforce_min_size)

        self._entries      = []
        self._cancel_event = threading.Event()
        self._worker       = None
        self._dest_rows    = []   # list of {'input': TextInput, 'widget': BoxLayout, 'label': Label}

        root = BoxLayout(orientation='vertical', padding=[dp(8), dp(6)], spacing=dp(4))
        _set_bg(root, 'bg')

        # ── Config panel ─────────────────────────────────────────────
        config = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(2))
        config.bind(minimum_height=config.setter('height'))

        config.add_widget(self._make_folder_row('Source (Place A):', is_source=True))

        self._dest_container = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(2))
        self._dest_container.bind(minimum_height=self._dest_container.setter('height'))
        config.add_widget(self._dest_container)

        add_row = BoxLayout(size_hint_y=None, height=dp(34))
        add_row.add_widget(Label(size_hint_x=None, width=dp(164)))
        add_row.add_widget(self._btn('+ Add Destination', self._add_dest_row, 'border',
                                     size_hint_x=None, width=dp(140)))
        add_row.add_widget(Label())
        config.add_widget(add_row)
        config.add_widget(self._make_options_row())
        root.add_widget(config)

        # ── Action bar ───────────────────────────────────────────────
        bar = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8),
                        padding=[dp(8), dp(6)])
        _set_bg(bar, 'surface')
        self._btn_scan   = self._btn('Scan / Compare', self._on_scan,   'accent',
                                     size_hint_x=None, width=dp(140))
        self._btn_sync   = self._btn('Sync →',         self._on_sync,   'green',
                                     size_hint_x=None, width=dp(100))
        self._btn_cancel = self._btn('Cancel',         self._on_cancel, 'red',
                                     size_hint_x=None, width=dp(90))
        self._btn_sync.disabled   = True
        self._btn_cancel.disabled = True
        bar.add_widget(self._btn_scan)
        bar.add_widget(self._btn_sync)
        bar.add_widget(self._btn_cancel)
        bar.add_widget(Label())
        root.add_widget(bar)

        # ── Progress ─────────────────────────────────────────────────
        prog = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(40), spacing=dp(2))
        self._progress_bar = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(14))
        self._progress_lbl = Label(text='', color=C('subtext'), font_size=sp(9),
                                   halign='left', size_hint_y=None, height=dp(16))
        self._progress_lbl.bind(size=self._progress_lbl.setter('text_size'))
        prog.add_widget(self._progress_bar)
        prog.add_widget(self._progress_lbl)
        root.add_widget(prog)

        # ── Filter bar ───────────────────────────────────────────────
        root.add_widget(self._make_filter_bar())

        # ── Column headers ───────────────────────────────────────────
        root.add_widget(self._make_col_headers())

        # ── File list (RecycleView) ───────────────────────────────────
        rv = RecycleView(size_hint_y=1)
        rv.viewclass = 'FileRow'
        rl = RecycleBoxLayout(
            default_size=(None, dp(26)),
            default_size_hint=(1, None),
            size_hint_y=None,
            orientation='vertical',
            spacing=dp(1),
        )
        rl.bind(minimum_height=rl.setter('height'))
        rv.add_widget(rl)
        _set_bg(rv, 'surface')
        self._file_rv = rv
        root.add_widget(rv)

        # ── Log ──────────────────────────────────────────────────────
        self._log_input = TextInput(
            text='',
            readonly=True,
            multiline=True,
            background_color=C('mantle'),
            foreground_color=C('text'),
            font_name='RobotoMono-Regular',
            font_size=sp(8),
            size_hint_y=None,
            height=dp(110),
        )
        root.add_widget(self._log_input)

        self._add_dest_row()
        self._load_settings()
        return root

    def _enforce_min_size(self, win, w, h):
        if w < 850:
            win.width = 850
        if h < 650:
            win.height = 650

    # ── Widget helpers ────────────────────────────────────────────────

    def _btn(self, text, callback, color_key='border', **kwargs):
        b = Button(
            text=text,
            background_normal='',
            background_color=C(color_key),
            color=C('bg'),
            bold=True,
            font_size=sp(10),
            size_hint_y=None,
            height=dp(32),
            **kwargs,
        )
        b.bind(on_release=lambda *_: callback())
        return b

    def _make_folder_row(self, label_text: str, is_source: bool = False):
        row = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(4))
        row.add_widget(Label(
            text=label_text, color=C('text'), font_size=sp(10),
            size_hint_x=None, width=dp(160),
            halign='right', valign='middle', text_size=(dp(155), dp(34)),
        ))
        inp = TextInput(
            background_color=C('surface'), foreground_color=C('text'),
            cursor_color=C('text'), multiline=False, font_size=sp(10),
            padding=[dp(6), dp(6)],
        )
        row.add_widget(inp)
        if is_source:
            self._src_input = inp
        row.add_widget(self._btn('Browse…', lambda i=inp: self._browse_folder(i), 'border',
                                 size_hint_x=None, width=dp(80)))
        return row

    def _add_dest_row(self, path: str = ''):
        idx        = len(self._dest_rows)
        is_default = idx == 0

        row = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(4))
        lbl_text = 'Destination (Place B):' if is_default else f'Destination {idx + 1}:'
        lbl = Label(
            text=lbl_text, color=C('text'), font_size=sp(10),
            size_hint_x=None, width=dp(160),
            halign='right', valign='middle', text_size=(dp(155), dp(34)),
        )
        row.add_widget(lbl)

        inp = TextInput(
            text=path, background_color=C('surface'), foreground_color=C('text'),
            cursor_color=C('text'), multiline=False, font_size=sp(10),
            padding=[dp(6), dp(6)],
        )
        row.add_widget(inp)
        row.add_widget(self._btn('Browse…', lambda i=inp: self._browse_folder(i), 'border',
                                 size_hint_x=None, width=dp(80)))

        row_data = {'input': inp, 'widget': row, 'label': lbl}

        if not is_default:
            def _remove(rd=row_data):
                self._dest_container.remove_widget(rd['widget'])
                self._dest_rows.remove(rd)
                self._renumber_dest_labels()
                self._save_settings()
            row.add_widget(self._btn('−', _remove, 'red', size_hint_x=None, width=dp(30)))

        self._dest_rows.append(row_data)
        self._dest_container.add_widget(row)
        self._save_settings()

    def _renumber_dest_labels(self):
        for i, rd in enumerate(self._dest_rows):
            rd['label'].text = 'Destination (Place B):' if i == 0 else f'Destination {i + 1}:'

    def _make_options_row(self):
        row = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(8))
        row.add_widget(Label(
            text='Excludes:', color=C('text'), font_size=sp(10),
            size_hint_x=None, width=dp(160),
            halign='right', valign='middle', text_size=(dp(155), dp(34)),
        ))
        self._excludes_input = TextInput(
            text=', '.join(UE_DEFAULT_EXCLUDES),
            background_color=C('surface'), foreground_color=C('text'),
            cursor_color=C('text'), multiline=False, font_size=sp(10),
            padding=[dp(6), dp(6)],
        )
        row.add_widget(self._excludes_input)

        self._use_hash_cb = CheckBox(active=True, size_hint_x=None, width=dp(22),
                                     color=C('accent'))
        row.add_widget(self._use_hash_cb)
        row.add_widget(Label(text='Precise (hash)', color=C('text'), font_size=sp(10),
                              size_hint_x=None, width=dp(110)))

        self._del_destonly_cb = CheckBox(active=False, size_hint_x=None, width=dp(22),
                                          color=C('red'))
        row.add_widget(self._del_destonly_cb)
        row.add_widget(Label(text='Delete dest-only', color=C('red'), font_size=sp(10),
                              size_hint_x=None, width=dp(120)))
        return row

    def _make_filter_bar(self):
        bar = BoxLayout(size_hint_y=None, height=dp(32), spacing=dp(6))
        bar.add_widget(Label(text='Filter:', color=C('subtext'), font_size=sp(10),
                              size_hint_x=None, width=dp(42)))
        self._filter_input = TextInput(
            background_color=C('surface'), foreground_color=C('text'),
            cursor_color=C('text'), multiline=False, font_size=sp(10),
            size_hint_x=None, width=dp(200), padding=[dp(6), dp(4)],
        )
        self._filter_input.bind(text=lambda *_: self._apply_filter())
        bar.add_widget(self._filter_input)

        self._filter_new_cb       = CheckBox(active=True,  size_hint_x=None, width=dp(20), color=C('green'))
        self._filter_modified_cb  = CheckBox(active=True,  size_hint_x=None, width=dp(20), color=C('yellow'))
        self._filter_unchanged_cb = CheckBox(active=False, size_hint_x=None, width=dp(20), color=C('subtext'))
        self._filter_destonly_cb  = CheckBox(active=True,  size_hint_x=None, width=dp(20), color=C('red'))

        for cb, text, w in [
            (self._filter_new_cb,       'New',       dp(38)),
            (self._filter_modified_cb,  'Modified',  dp(68)),
            (self._filter_unchanged_cb, 'Unchanged', dp(80)),
            (self._filter_destonly_cb,  'Dest Only', dp(72)),
        ]:
            cb.bind(active=lambda *_: self._apply_filter())
            bar.add_widget(cb)
            bar.add_widget(Label(text=text, color=C('text'), font_size=sp(10),
                                  size_hint_x=None, width=w))

        self._stats_lbl = Label(text='', color=C('subtext'), font_size=sp(9), halign='right')
        self._stats_lbl.bind(size=self._stats_lbl.setter('text_size'))
        bar.add_widget(self._stats_lbl)
        return bar

    def _make_col_headers(self):
        hdr = BoxLayout(size_hint_y=None, height=dp(24), spacing=dp(1), padding=[0, 0])
        _set_bg(hdr, 'mantle')
        for text, fixed_w in [
            ('Status',        dp(90)),
            ('Relative Path', None),
            ('Src Size',      dp(80)),
            ('Dst Size',      dp(80)),
            ('Sync To',       dp(90)),
            ('Note',          dp(140)),
        ]:
            kw = {'size_hint_x': None, 'width': fixed_w} if fixed_w else {'size_hint_x': 1}
            lbl = Label(text=text, color=C('subtext'), font_size=sp(9), bold=True,
                        halign='left', valign='middle', padding=[dp(4), 0], **kw)
            lbl.bind(size=lbl.setter('text_size'))
            hdr.add_widget(lbl)
        return hdr

    # ── Browse folder popup ───────────────────────────────────────────

    def _browse_folder(self, target_input: TextInput):
        start = target_input.text.strip()
        if not start or not Path(start).is_dir():
            start = str(Path.home())

        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(4))
        fc = FileChooserListView(path=start, dirselect=True)
        fc.filters = [lambda folder, fname: os.path.isdir(os.path.join(folder, fname))]

        btn_row = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
        popup   = Popup(title='Select Folder', content=content, size_hint=(0.85, 0.85))

        def _select(*_):
            if fc.selection:
                target_input.text = fc.selection[0]
            popup.dismiss()
            self._save_settings()

        ok  = Button(text='Select', background_normal='', background_color=C('accent'), color=C('bg'))
        can = Button(text='Cancel', background_normal='', background_color=C('border'), color=C('bg'))
        ok.bind(on_release=_select)
        can.bind(on_release=lambda *_: popup.dismiss())
        btn_row.add_widget(ok)
        btn_row.add_widget(can)
        content.add_widget(fc)
        content.add_widget(btn_row)
        popup.open()

    # ── Dialogs ───────────────────────────────────────────────────────

    def _alert(self, title: str, msg: str, callback=None):
        content = BoxLayout(orientation='vertical', padding=dp(14), spacing=dp(10))
        lbl = Label(text=msg, color=C('text'), font_size=sp(10),
                    halign='center', valign='top', size_hint_y=1)
        lbl.bind(size=lbl.setter('text_size'))
        content.add_widget(lbl)
        popup = Popup(title=title, content=content,
                      size_hint=(None, None), size=(dp(400), dp(220)))
        btn = Button(text='OK', background_normal='', background_color=C('accent'),
                     color=C('bg'), size_hint_y=None, height=dp(36))
        def _ok(*_):
            popup.dismiss()
            if callback:
                callback()
        btn.bind(on_release=_ok)
        content.add_widget(btn)
        popup.open()

    def _confirm(self, title: str, msg: str, on_yes, on_no=None):
        content = BoxLayout(orientation='vertical', padding=dp(14), spacing=dp(10))
        lbl = Label(text=msg, color=C('text'), font_size=sp(10),
                    halign='left', valign='top', size_hint_y=1)
        lbl.bind(size=lbl.setter('text_size'))
        content.add_widget(lbl)
        btn_row = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
        popup   = Popup(title=title, content=content,
                        size_hint=(None, None), size=(dp(500), dp(320)))
        yes = Button(text='Yes', background_normal='', background_color=C('green'), color=C('bg'))
        no  = Button(text='No',  background_normal='', background_color=C('red'),   color=C('bg'))
        yes.bind(on_release=lambda *_: (popup.dismiss(), on_yes()))
        no.bind(on_release=lambda *_: (popup.dismiss(), on_no() if on_no else None))
        btn_row.add_widget(yes)
        btn_row.add_widget(no)
        content.add_widget(btn_row)
        popup.open()

    # ── Scan ─────────────────────────────────────────────────────────

    def _on_scan(self):
        src = self._src_input.text.strip()
        if not src:
            self._alert('Missing source', 'Please select a source folder.')
            return
        if not Path(src).is_dir():
            self._alert('Invalid source', f'Source folder not found:\n{src}')
            return

        dst_paths = self._dest_paths()
        if not dst_paths:
            self._alert('No destinations', 'Add at least one destination folder.')
            return

        missing = [p for p in dst_paths if not p.exists()]
        if missing:
            msg = ('These destinations do not exist:\n'
                   + '\n'.join(str(p) for p in missing)
                   + '\n\nCreate them?')
            def _create_and_scan():
                for p in missing:
                    p.mkdir(parents=True)
                self._do_scan(Path(src), dst_paths)
            self._confirm('Create destinations?', msg, on_yes=_create_and_scan)
            return

        self._do_scan(Path(src), dst_paths)

    def _do_scan(self, src: Path, dst_paths: list):
        excludes = [x.strip().lower() for x in self._excludes_input.text.split(',') if x.strip()]
        use_hash = self._use_hash_cb.active

        self._cancel_event.clear()
        self._set_busy(True)
        self._file_rv.data = []
        self._log(f'── Scan started {datetime.now():%Y-%m-%d %H:%M:%S} ──')
        self._log(f'   Source : {src}')
        for i, dpath in enumerate(dst_paths):
            self._log(f'   D{i+1}     : {dpath}')
        self._log(f"   Mode   : {'Precise (hash/' + _HASH_ALGO + ')' if use_hash else 'Fast (size+mtime)'}")

        def run():
            results = diff_trees_multi(
                src, dst_paths, excludes, use_hash,
                progress_cb=self._update_progress,
                cancel_event=self._cancel_event,
            )
            Clock.schedule_once(lambda dt: self._scan_done(results), 0)

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _scan_done(self, results: list):
        self._entries = results
        self._apply_filter()
        self._set_busy(False)

        new       = sum(1 for e in results if e.overall_status == FileStatus.NEW)
        modified  = sum(1 for e in results if e.overall_status == FileStatus.MODIFIED)
        unchanged = sum(1 for e in results if e.overall_status == FileStatus.UNCHANGED)
        dest_only = sum(1 for e in results if e.overall_status == FileStatus.DEST_ONLY)

        self._log(f'   New:{new}  Modified:{modified}  Unchanged:{unchanged}  Dest-only:{dest_only}')
        self._log('── Scan complete ──')

        needs_sync = new + modified + (dest_only if self._del_destonly_cb.active else 0)
        self._btn_sync.disabled = (needs_sync == 0)
        self._save_settings()

    # ── Sync ─────────────────────────────────────────────────────────

    def _on_sync(self):
        to_sync = [e for e in self._entries if e.needs_sync]
        if not to_sync:
            self._alert('Nothing to sync', 'All destinations are up to date.')
            return

        dst_paths = self._dest_paths()
        if not dst_paths:
            self._alert('No destinations', 'No destination folders configured.')
            return

        del_count   = sum(1 for e in to_sync
                          if e.overall_status == FileStatus.DEST_ONLY
                          and self._del_destonly_cb.active)
        dest_lines  = '\n'.join(f'D{i+1}: {p}' for i, p in enumerate(dst_paths))
        msg = (f'About to sync {len(to_sync)} file(s) to {len(dst_paths)} destination(s):\n\n'
               f'{dest_lines}')
        if del_count:
            msg += f'\n\n⚠ {del_count} file(s) will be DELETED from destination(s).'
        msg += '\n\nEach file is only copied to destinations that need it.'

        self._confirm('Confirm Sync', msg, on_yes=lambda: self._do_sync(dst_paths))

    def _do_sync(self, dst_paths: list):
        self._cancel_event.clear()
        self._set_busy(True)
        self._log(f'── Sync started {datetime.now():%Y-%m-%d %H:%M:%S} ──')

        def run():
            stats = sync_files_multi(
                self._entries,
                dst_roots=dst_paths,
                copy_new=True,
                copy_modified=True,
                delete_dest_only=self._del_destonly_cb.active,
                progress_cb=self._update_progress,
                cancel_event=self._cancel_event,
                log_cb=lambda m: Clock.schedule_once(lambda dt: self._log(m), 0),
            )
            Clock.schedule_once(lambda dt: self._sync_done(stats, dst_paths), 0)

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _sync_done(self, stats: dict, dst_paths: list):
        self._set_busy(False)
        total_copied  = sum(s['copied']  for s in stats.values())
        total_deleted = sum(s['deleted'] for s in stats.values())
        total_errors  = sum(s['errors']  for s in stats.values())

        self._log('── Sync complete ──')
        for i in range(len(dst_paths)):
            s = stats[i]
            self._log(f"   D{i+1}: copied={s['copied']}  deleted={s['deleted']}  errors={s['errors']}")

        if total_errors:
            self._alert('Sync finished with errors',
                        f'Errors: {total_errors}\nCopied: {total_copied}\nDeleted: {total_deleted}'
                        '\n\nCheck the log for details.',
                        callback=self._on_scan)
        else:
            self._alert('Sync complete',
                        f'Files copied/updated: {total_copied}\nFiles deleted: {total_deleted}',
                        callback=self._on_scan)

    # ── Cancel ────────────────────────────────────────────────────────

    def _on_cancel(self):
        self._cancel_event.set()

    # ── Filter / display ──────────────────────────────────────────────

    def _apply_filter(self):
        text_filter = self._filter_input.text.lower() if hasattr(self, '_filter_input') else ''
        show_map = {
            FileStatus.NEW:       self._filter_new_cb.active,
            FileStatus.MODIFIED:  self._filter_modified_cb.active,
            FileStatus.UNCHANGED: self._filter_unchanged_cb.active,
            FileStatus.DEST_ONLY: self._filter_destonly_cb.active,
        }

        rows = []
        for entry in self._entries:
            status = entry.overall_status
            if not show_map.get(status, True):
                continue
            if text_filter and text_filter not in entry.rel_path.lower():
                continue

            note = ''
            if status == FileStatus.MODIFIED:
                has_hash = any(ds.dst_hash for ds in entry.dest_statuses)
                note = 'hash differs' if (has_hash or entry.src_hash) else 'size/mtime differs'

            dst_size = next((ds.dst_size for ds in entry.dest_statuses if ds.dst_size > 0), 0)

            rows.append({
                'status_text':   status.value,
                'path_text':     entry.rel_path,
                'src_size_text': _fmt_size(entry.src_size),
                'dst_size_text': _fmt_size(dst_size),
                'sync_to_text':  entry.dest_label() or '—',
                'note_text':     note,
                'status_color':  list(STATUS_COLORS[status]),
            })

        self._file_rv.data = rows

        counts = {s: sum(1 for e in self._entries if e.overall_status == s) for s in FileStatus}
        self._stats_lbl.text = (
            f"New:{counts[FileStatus.NEW]}  "
            f"Mod:{counts[FileStatus.MODIFIED]}  "
            f"Same:{counts[FileStatus.UNCHANGED]}  "
            f"DstOnly:{counts[FileStatus.DEST_ONLY]}  "
            f"| Shown:{len(rows)}"
        )

    # ── Progress ─────────────────────────────────────────────────────

    def _update_progress(self, current: int, total: int, message: str):
        pct = (current / total * 100) if total > 0 else 0
        def _do(dt):
            self._progress_bar.value = pct
            self._progress_lbl.text  = message
        Clock.schedule_once(_do, 0)

    # ── Busy state ────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        def _do(dt=None):
            self._btn_scan.disabled   = busy
            self._btn_sync.disabled   = busy
            self._btn_cancel.disabled = not busy
        Clock.schedule_once(_do, 0)

    # ── Log ───────────────────────────────────────────────────────────

    def _log(self, message: str):
        def _do(dt=None):
            self._log_input.text += message + '\n'
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            Clock.schedule_once(_do, 0)

    # ── Dest paths ────────────────────────────────────────────────────

    def _dest_paths(self) -> list:
        return [Path(rd['input'].text.strip())
                for rd in self._dest_rows if rd['input'].text.strip()]

    # ── Settings ─────────────────────────────────────────────────────

    def _settings_path(self) -> Path:
        return Path.home() / '.filesync_settings.json'

    def _save_settings(self):
        if not hasattr(self, '_excludes_input'):
            return
        data = {
            'src':          self._src_input.text,
            'destinations': [rd['input'].text for rd in self._dest_rows],
            'excludes':     self._excludes_input.text,
            'use_hash':     self._use_hash_cb.active,
            'del_destonly': self._del_destonly_cb.active,
        }
        try:
            self._settings_path().write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_settings(self):
        try:
            data = json.loads(self._settings_path().read_text())
            self._src_input.text         = data.get('src', '')
            self._excludes_input.text    = data.get('excludes', ', '.join(UE_DEFAULT_EXCLUDES))
            self._use_hash_cb.active     = data.get('use_hash', True)
            self._del_destonly_cb.active = data.get('del_destonly', False)

            destinations = data.get('destinations', [''])
            if destinations:
                self._dest_rows[0]['input'].text = destinations[0]
            for path in destinations[1:]:
                self._add_dest_row(path=path)
        except Exception:
            pass


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == '__main__':
    FileSyncApp().run()
