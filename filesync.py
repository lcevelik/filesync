"""
FileSync - Precision file synchronization tool
Designed for Unreal Engine projects (and any large file trees)
Supports multiple destinations.
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
from typing import Literal, Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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


def build_file_index(root: Path) -> dict:
    """Walk a directory → {rel_path: {path, size, mtime}}."""
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
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
        return build_file_index(root) if root.exists() else {}

    all_roots = [src_root] + list(dst_roots)
    with ThreadPoolExecutor(max_workers=len(all_roots)) as ex:
        futures = [ex.submit(build_file_index if i == 0 else safe_index, r)
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
# GUI
# ─────────────────────────────────────────────

PALETTE = {
    "bg":      "#1e1e2e",
    "surface": "#2a2a3e",
    "border":  "#3a3a5a",
    "accent":  "#7c9ef8",
    "text":    "#cdd6f4",
    "subtext": "#a6adc8",
    "green":   "#a6e3a1",
    "yellow":  "#f9e2af",
    "red":     "#f38ba8",
    "blue":    "#89b4fa",
    "mantle":  "#181825",
    "orange":  "#fab387",
}

STATUS_COLOR = {
    FileStatus.NEW:       PALETTE["green"],
    FileStatus.MODIFIED:  PALETTE["yellow"],
    FileStatus.UNCHANGED: PALETTE["subtext"],
    FileStatus.DEST_ONLY: PALETTE["red"],
}


class FileSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FileSync — Precision Sync")
        self.configure(bg=PALETTE["bg"])
        # Fixed size: 1150×800 aspect ratio capped at 700px per side
        w, h = 700, 487
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.resizable(False, False)

        self._entries: list = []
        self._cancel_event = threading.Event()
        self._worker = None
        self._dest_rows: list = []   # list of {frame, var, label_widget}
        self._src_var: tk.StringVar  # set by _build_folder_row

        self._build_ui()
        self._load_settings()

    # ── UI construction ──────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview",
                     background=PALETTE["surface"],
                     foreground=PALETTE["text"],
                     fieldbackground=PALETTE["surface"],
                     rowheight=22,
                     font=("Consolas", 9))
        s.configure("Treeview.Heading",
                     background=PALETTE["mantle"],
                     foreground=PALETTE["subtext"],
                     font=("Segoe UI", 9, "bold"))
        s.map("Treeview",
              background=[("selected", PALETTE["border"])],
              foreground=[("selected", PALETTE["text"])])
        s.configure("Horizontal.TScrollbar",
                     background=PALETTE["surface"],
                     troughcolor=PALETTE["mantle"])
        s.configure("Vertical.TScrollbar",
                     background=PALETTE["surface"],
                     troughcolor=PALETTE["mantle"])
        s.configure("TProgressbar",
                     troughcolor=PALETTE["mantle"],
                     background=PALETTE["accent"])

    def _build_ui(self):
        self._style()

        top = tk.Frame(self, bg=PALETTE["bg"], padx=12, pady=8)
        top.pack(fill="x")

        # Source row
        self._build_folder_row(top, "Source (Place A):", "src")

        # ── Destination section ──────────────────────────────────────
        self._dest_section = tk.Frame(top, bg=PALETTE["bg"])
        self._dest_section.pack(fill="x")

        # Always add the first (default) destination
        self._add_dest_row()

        # Plus button row
        plus_row = tk.Frame(top, bg=PALETTE["bg"])
        plus_row.pack(fill="x", pady=(2, 0))
        self._btn(plus_row, "+ Add Destination", self._add_dest_row,
                  PALETTE["border"], width=7).pack(side="right", pady=2)

        # Options row
        self._build_options_row(top)

        # ── Action bar ───────────────────────────────────────────────
        bar = tk.Frame(self, bg=PALETTE["surface"], pady=6)
        bar.pack(fill="x", padx=12)
        self._btn_scan   = self._btn(bar, "Scan / Compare", self._on_scan,   PALETTE["accent"])
        self._btn_sync   = self._btn(bar, "Sync →",         self._on_sync,   PALETTE["green"],  state="disabled")
        self._btn_cancel = self._btn(bar, "Cancel",         self._on_cancel, PALETTE["red"],    state="disabled")
        for b in (self._btn_scan, self._btn_sync, self._btn_cancel):
            b.pack(side="left", padx=6, pady=4)

        # ── Progress ─────────────────────────────────────────────────
        prog_frame = tk.Frame(self, bg=PALETTE["bg"], padx=12, pady=2)
        prog_frame.pack(fill="x")
        self._progress_var = tk.DoubleVar()
        self._progress = ttk.Progressbar(prog_frame, variable=self._progress_var,
                                          maximum=100, mode="determinate")
        self._progress.pack(fill="x")
        self._progress_label = tk.Label(prog_frame, text="", bg=PALETTE["bg"],
                                         fg=PALETTE["subtext"], font=("Segoe UI", 9))
        self._progress_label.pack(anchor="w")

        # ── File list ────────────────────────────────────────────────
        list_frame = tk.Frame(self, bg=PALETTE["bg"], padx=12, pady=4)
        list_frame.pack(fill="both", expand=True)

        # Filter bar
        filter_bar = tk.Frame(list_frame, bg=PALETTE["bg"])
        filter_bar.pack(fill="x", pady=(0, 4))
        tk.Label(filter_bar, text="Filter:", bg=PALETTE["bg"],
                 fg=PALETTE["subtext"], font=("Segoe UI", 9)).pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Entry(filter_bar, textvariable=self._filter_var,
                 bg=PALETTE["surface"], fg=PALETTE["text"],
                 insertbackground=PALETTE["text"], relief="flat",
                 font=("Segoe UI", 9), width=30).pack(side="left", padx=4)

        self._show_new       = tk.BooleanVar(value=True)
        self._show_modified  = tk.BooleanVar(value=True)
        self._show_unchanged = tk.BooleanVar(value=False)
        self._show_destonly  = tk.BooleanVar(value=True)
        for text, var in [
            ("New",       self._show_new),
            ("Modified",  self._show_modified),
            ("Unchanged", self._show_unchanged),
            ("Dest Only", self._show_destonly),
        ]:
            tk.Checkbutton(filter_bar, text=text, variable=var,
                           bg=PALETTE["bg"], fg=PALETTE["text"],
                           selectcolor=PALETTE["surface"],
                           activebackground=PALETTE["bg"],
                           command=self._apply_filter,
                           font=("Segoe UI", 9)).pack(side="left", padx=4)

        # Treeview
        cols = ("status", "path", "src_size", "dst_size", "sync_to", "note")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                   selectmode="extended")
        self._tree.heading("status",   text="Status",        anchor="w")
        self._tree.heading("path",     text="Relative Path", anchor="w")
        self._tree.heading("src_size", text="Src Size",      anchor="e")
        self._tree.heading("dst_size", text="Dst Size",      anchor="e")
        self._tree.heading("sync_to",  text="Sync To",       anchor="w")
        self._tree.heading("note",     text="Note",          anchor="w")
        self._tree.column("status",   width=75,  stretch=False)
        self._tree.column("path",     width=290, stretch=True)
        self._tree.column("src_size", width=70,  stretch=False, anchor="e")
        self._tree.column("dst_size", width=70,  stretch=False, anchor="e")
        self._tree.column("sync_to",  width=55,  stretch=False)
        self._tree.column("note",     width=100, stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ── Log ──────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=PALETTE["bg"], padx=12, pady=4)
        log_frame.pack(fill="x")
        tk.Label(log_frame, text="Log", bg=PALETTE["bg"],
                 fg=PALETTE["subtext"], font=("Segoe UI", 8)).pack(anchor="w")
        self._log_text = tk.Text(log_frame, height=6, bg=PALETTE["mantle"],
                                  fg=PALETTE["text"], font=("Consolas", 8),
                                  relief="flat", state="disabled", wrap="none")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                    command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        self._log_text.pack(side="left", fill="x", expand=True)
        log_scroll.pack(side="right", fill="y")

    def _build_folder_row(self, parent, label_text: str, key: str):
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label_text, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=("Segoe UI", 9), width=19, anchor="e").pack(side="left")
        var = tk.StringVar()
        setattr(self, f"_{key}_var", var)
        tk.Entry(row, textvariable=var, bg=PALETTE["surface"],
                 fg=PALETTE["text"], insertbackground=PALETTE["text"],
                 relief="flat", font=("Segoe UI", 9)).pack(
            side="left", fill="x", expand=True, padx=4)
        self._btn(row, "Browse…",
                  lambda k=key: self._browse(k), PALETTE["border"]).pack(side="left")

    def _btn(self, parent, text, command, color=None, state: Literal["normal", "active", "disabled"] = "normal", **kwargs):
        color = color or PALETTE["border"]
        return tk.Button(
            parent, text=text, command=command,
            bg=color, fg=PALETTE["bg"],
            activebackground=PALETTE["accent"],
            font=("Segoe UI", 9, "bold"),
            relief="flat", padx=10, pady=4,
            state=state, **kwargs,
        )

    def _build_options_row(self, parent):
        pass

    # ── Destination rows ─────────────────────────────────────────────

    def _add_dest_row(self, path: str = ""):
        idx = len(self._dest_rows)
        is_default = (idx == 0)

        row_frame = tk.Frame(self._dest_section, bg=PALETTE["bg"])
        row_frame.pack(fill="x", pady=1)

        label_text = "Destination (Place B):" if is_default else f"Destination {idx + 1}:"
        lbl = tk.Label(row_frame, text=label_text, bg=PALETTE["bg"], fg=PALETTE["text"],
                       font=("Segoe UI", 9), width=19, anchor="e")
        lbl.pack(side="left")

        var = tk.StringVar(value=path)
        tk.Entry(row_frame, textvariable=var, bg=PALETTE["surface"],
                 fg=PALETTE["text"], insertbackground=PALETTE["text"],
                 relief="flat", font=("Segoe UI", 9)).pack(
            side="left", fill="x", expand=True, padx=4)

        row_data = {"frame": row_frame, "var": var, "label": lbl}

        self._btn(row_frame, "Browse…",
                  lambda rd=row_data: self._browse_dest(rd),
                  PALETTE["border"]).pack(side="left")

        if not is_default:
            self._btn(row_frame, "−",
                      lambda rd=row_data: self._remove_dest_row(rd),
                      PALETTE["red"]).pack(side="left", padx=(4, 0))

        self._dest_rows.append(row_data)
        self._save_settings()

    def _remove_dest_row(self, row_data: dict):
        row_data["frame"].destroy()
        self._dest_rows.remove(row_data)
        self._renumber_dest_labels()
        self._save_settings()

    def _renumber_dest_labels(self):
        for i, row in enumerate(self._dest_rows):
            text = "Destination (Place B):" if i == 0 else f"Destination {i + 1}:"
            row["label"].config(text=text)

    def _dest_paths(self) -> list:
        """Return list of non-empty destination Path objects."""
        paths = []
        for row in self._dest_rows:
            p = row["var"].get().strip()
            if p:
                paths.append(Path(p))
        return paths

    # ── Browse ────────────────────────────────────────────────────────

    def _browse(self, key: str):
        folder = filedialog.askdirectory(
            title=f"Select {'Source' if key == 'src' else 'Destination'} Folder")
        if folder:
            getattr(self, f"_{key}_var").set(folder)
            self._save_settings()

    def _browse_dest(self, row_data: dict):
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            row_data["var"].set(folder)
            self._save_settings()

    # ── Scan ─────────────────────────────────────────────────────────

    def _on_scan(self):
        src = self._src_var.get().strip()
        if not src:
            messagebox.showwarning("Missing source", "Please select a source folder.")
            return
        if not Path(src).is_dir():
            messagebox.showerror("Invalid source", f"Source folder not found:\n{src}")
            return

        dst_paths = self._dest_paths()
        if not dst_paths:
            messagebox.showwarning("No destinations", "Add at least one destination folder.")
            return

        # Ensure all destinations exist (offer to create)
        for dp in dst_paths:
            if not dp.exists():
                if messagebox.askyesno("Create destination?",
                                        f"Destination does not exist:\n{dp}\n\nCreate it?"):
                    dp.mkdir(parents=True)
                else:
                    return

        self._cancel_event.clear()
        self._set_busy(True)
        self._clear_tree()
        self._log(f"── Scan started {datetime.now():%Y-%m-%d %H:%M:%S} ──")
        self._log(f"   Source : {src}")
        for i, dp in enumerate(dst_paths):
            self._log(f"   D{i+1}     : {dp}")

        def run():
            results = diff_trees_multi(
                Path(src), dst_paths,
                progress_cb=self._update_progress,
                cancel_event=self._cancel_event,
            )
            self.after(0, self._scan_done, results)

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

        self._log(f"   New:{new}  Modified:{modified}  Unchanged:{unchanged}  Dest-only:{dest_only}")
        self._log("── Scan complete ──")

        needs_sync = new + modified + (dest_only if True else 0)
        self._btn_sync.config(state="normal" if needs_sync > 0 else "disabled")
        self._save_settings()

    # ── Sync ─────────────────────────────────────────────────────────

    def _on_sync(self):
        to_sync = [e for e in self._entries if e.needs_sync]
        if not to_sync:
            messagebox.showinfo("Nothing to sync", "All destinations are up to date.")
            return

        dst_paths = self._dest_paths()
        if not dst_paths:
            messagebox.showwarning("No destinations", "No destination folders configured.")
            return

        # Build summary for confirmation dialog
        del_count = sum(
            1 for e in to_sync
            if e.overall_status == FileStatus.DEST_ONLY and True
        )
        dest_labels = [f"D{i+1}: {p}" for i, p in enumerate(dst_paths)]
        msg = (
            f"About to sync {len(to_sync)} file(s) to "
            f"{len(dst_paths)} destination(s):\n\n"
            + "\n".join(dest_labels)
        )
        if del_count:
            msg += f"\n\n⚠ {del_count} file(s) will be DELETED from destination(s)."
        msg += "\n\nEach file is only copied to destinations that need it."
        if not messagebox.askyesno("Confirm Sync", msg):
            return

        self._cancel_event.clear()
        self._set_busy(True)
        self._log(f"── Sync started {datetime.now():%Y-%m-%d %H:%M:%S} ──")

        def run():
            stats = sync_files_multi(
                self._entries,
                dst_roots=dst_paths,
                copy_new=True,
                copy_modified=True,
                delete_dest_only=True,
                progress_cb=self._update_progress,
                cancel_event=self._cancel_event,
                log_cb=lambda msg: self.after(0, self._log, msg),
            )
            self.after(0, self._sync_done, stats, dst_paths)

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _sync_done(self, stats: dict, dst_paths: list):
        self._set_busy(False)

        total_copied  = sum(s["copied"]  for s in stats.values())
        total_deleted = sum(s["deleted"] for s in stats.values())
        total_errors  = sum(s["errors"]  for s in stats.values())

        lines = [f"── Sync complete ──"]
        for i in range(len(dst_paths)):
            s = stats[i]
            lines.append(
                f"   D{i+1}: copied={s['copied']}  deleted={s['deleted']}  errors={s['errors']}"
            )
        for line in lines:
            self._log(line)

        if total_errors:
            messagebox.showwarning(
                "Sync finished with errors",
                f"Sync completed with errors.\n\n"
                f"Total copied : {total_copied}\n"
                f"Total deleted: {total_deleted}\n"
                f"Total errors : {total_errors}\n\n"
                "Check the log for details."
            )
        else:
            messagebox.showinfo(
                "Sync complete",
                f"Sync finished successfully!\n\n"
                f"Files copied/updated : {total_copied}\n"
                f"Files deleted        : {total_deleted}"
            )
        self._on_scan()

    # ── Cancel ────────────────────────────────────────────────────────

    def _on_cancel(self):
        self._cancel_event.set()

    # ── Tree helpers ─────────────────────────────────────────────────

    def _clear_tree(self):
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _apply_filter(self):
        self._clear_tree()
        text_filter = self._filter_var.get().lower()
        show_map = {
            FileStatus.NEW:       self._show_new.get(),
            FileStatus.MODIFIED:  self._show_modified.get(),
            FileStatus.UNCHANGED: self._show_unchanged.get(),
            FileStatus.DEST_ONLY: self._show_destonly.get(),
        }
        shown = 0
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

            tag = status.value.lower().replace(" ", "_")
            self._tree.insert(
                "", "end",
                values=(
                    status.value,
                    entry.rel_path,
                    _fmt_size(entry.src_size),
                    _fmt_size(dst_size),
                    entry.dest_label() or "—",
                    note,
                ),
                tags=(tag,),
            )
            shown += 1

        for status, color in STATUS_COLOR.items():
            self._tree.tag_configure(
                status.value.lower().replace(" ", "_"),
                foreground=color,
            )


    # ── Progress ─────────────────────────────────────────────────────

    def _update_progress(self, current: int, total: int, message: str):
        pct = (current / total * 100) if total > 0 else 0
        self.after(0, self._progress_var.set, pct)
        self.after(0, self._progress_label.config, {"text": message})

    # ── Busy state ───────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        if busy:
            self._btn_scan.config(state="disabled")
            self._btn_sync.config(state="disabled")
            self._btn_cancel.config(state="normal")
        else:
            self._btn_scan.config(state="normal")
            self._btn_cancel.config(state="disabled")

    # ── Log ──────────────────────────────────────────────────────────

    def _log(self, message: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", message + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    # ── Settings persistence ─────────────────────────────────────────

    def _settings_path(self) -> Path:
        return Path.home() / ".filesync_settings.json"

    def _save_settings(self):
        # Guard against being called before all widgets are constructed
        if not hasattr(self, "_dest_rows"):
            return
        data = {
            "src": self._src_var.get(),
            "destinations": [row["var"].get() for row in self._dest_rows],
        }
        try:
            self._settings_path().write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_settings(self):
        try:
            data = json.loads(self._settings_path().read_text())
            self._src_var.set(data.get("src", ""))

            destinations = data.get("destinations", [""])
            # First destination is already created by _add_dest_row() in __init__
            if destinations:
                self._dest_rows[0]["var"].set(destinations[0])
            for path in destinations[1:]:
                self._add_dest_row(path=path)
        except Exception:
            pass


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

def _fmt_size(n: float) -> str:
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
    app = FileSyncApp()
    app.mainloop()
