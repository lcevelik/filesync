# Project: SyncFlow (filesync)

Precision file synchronization tool — designed for Unreal Engine projects and large file trees. Two GUI frontends: legacy tkinter (filesync.py) and modern PyQt6 (filesync_qt.py).

## Goals

- [x] Stabilize SyncFlow v1.0.x release with full UE project support — 2026-06-15
- [ ] Add cloud/network sync targets (S3, SMB, SSH) — 2026-08-01
- [ ] Release v1.2.0 with remote sync targets — 2026-09-01

## In Progress

- [x] Polish PyQt6 dark-theme UI for edge cases and multi-monitor setups
- [ ] Improve error handling and logging for failed sync operations

## To Do

### Architecture & Code Quality
- [ ] Extract shared core engine (data model, hashing, diff, sync) into a separate `engine.py` module — currently duplicated across both .py files
- [ ] Add type hints throughout and run mypy for static analysis
- [ ] Replace raw `threading.Thread` in tkinter version with proper QThread-like worker pattern
- [ ] Add unit tests for core engine functions (compute_hash, build_file_index, diff_trees_multi, sync_files_multi)
- [ ] Add integration tests for end-to-end sync scenarios
- [ ] Add CI/CD pipeline (GitHub Actions) with linting, tests, and automated builds
- [ ] Create a LICENSE file (README claims MIT but no LICENSE file exists)

### Features
- [ ] Add SSH/SMB remote destination support
- [ ] Implement incremental sync with file watcher (inotify/FSEvents)
- [ ] Add sync profile presets for common UE and non-UE workflows
- [ ] Create macOS and Linux executable builds (currently Windows-focused via build.bat)
- [ ] Add conflict resolution UI when source and destination both modified
- [ ] Add dry-run mode that shows what would be synced without copying
- [ ] Add selective sync — let users check/uncheck individual files before syncing
- [ ] Add bandwidth throttling for network destinations
- [ ] Add symlink handling options (follow, skip, or copy as-is)
- [ ] Add file size summary in scan results (total bytes to sync)
- [ ] Add "Open in Explorer/Finder" context menu on file list items
- [ ] Add resume support for interrupted syncs (track partially copied files)

### Performance (mostly done in v1.1.0)
- [x] xxhash support — 9.5x faster than SHA-256, with automatic fallback
- [x] ProcessPoolExecutor for CPU-bound hashing (bypasses GIL)
- [x] Persistent hash cache to skip re-hashing unchanged files
- [x] os.scandir() for faster directory traversal
- [x] os.sendfile() for kernel-level file copy
- [ ] Add optional GPU-accelerated hashing for 100MB+ files (CUDA detected but not yet used)
- [ ] Implement chunked/buffered copy for very large files with progress per-file

### Security
- [ ] Validate and sanitize all file paths to prevent path traversal attacks
- [ ] Add integrity verification after copy (re-hash destination file)
- [ ] Warn when syncing to/from system directories (e.g., /, C:\Windows)
- [ ] Encrypt settings file if it will store remote credentials

## Done

- [x] SHA-256 hash-based file integrity verification
- [x] Multi-destination parallel sync with UE auto-exclusion
- [x] PyQt6 dark-theme UI with drag-and-drop and real-time progress
- [x] Sync direction toggle (Src→Dst / Dst→Src) with persistence
- [x] Unreal Engine project detection (.uproject files)
- [x] Auto-exclude UE build artifacts (Intermediate, Saved, DerivedDataCache, Binaries)
- [x] Settings persistence to ~/.filesync_settings.json
- [x] Qt file browser with folder+file display (non-native dialog)
- [x] Cancel support for long-running scan/sync operations
- [x] App renamed from FileSync to SyncFlow
- [x] xxhash support (9.5x faster hashing) with SHA-256 automatic fallback
- [x] ProcessPoolExecutor for hashing — bypasses GIL, linear core scaling
- [x] Persistent hash cache (~/.filesync_hashcache.json) — skip re-hashing unchanged files
- [x] os.scandir() directory scan — fewer syscalls than os.walk
- [x] os.sendfile() kernel-level file copy (2.85x faster on Linux)
- [x] 4MB hash chunks for large UE assets (was 1MB)
- [x] HASH_ERROR sentinel for read failures (was empty string)
- [x] Hardware auto-detection on startup (CPU cores, xxhash, CUDA/GPU)
- [x] Hash algorithm selector in UI (xxhash / SHA-256) with settings persistence
- [x] Precision guarantee: same-size files always hashed, never skipped by mtime

## Blocked

_(none currently)_

## Releases

- v1.1.0 — current — Performance overhaul: xxhash, ProcessPool, hash cache, sendfile, scandir, hardware detection
- v1.0.1 — SyncFlow with UE detection, SHA-256 verification, multi-dest sync, direction toggle
- v1.2.0 — planned 2026-09-01 — Remote sync targets and cross-platform improvements

## Notes

- Settings persist to ~/.filesync_settings.json
- Standalone .exe via PyInstaller available on Releases page; Python 3.9+ required for source runs
- Two GUI frontends exist: `filesync.py` (tkinter, legacy) and `filesync_qt.py` (PyQt6, primary)
- Core sync engine logic is duplicated between both files — should be extracted
- xxhash now used by default (9.5x faster than SHA-256), falls back to SHA-256 if not installed
- build.bat only builds the tkinter version; should be updated for PyQt6 version
