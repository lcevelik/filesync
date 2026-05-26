# Project: SyncFlow (filesync)

Precision file synchronization tool — designed for Unreal Engine projects and large file trees. Two GUI frontends: legacy tkinter (filesync.py) and modern PyQt6 (filesync_qt.py).

## Goals

- [ ] Stabilize SyncFlow v1.0.x release with full UE project support — 2026-06-15
- [ ] Add cloud/network sync targets (S3, SMB, SSH) — 2026-08-01
- [ ] Release v1.1.0 with multi-platform improvements — 2026-09-01

## In Progress

- [ ] Polish PyQt6 dark-theme UI for edge cases and multi-monitor setups
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

### Performance
- [ ] Implement mtime-based fast-path: skip hashing if mtime unchanged since last sync
- [ ] Add optional xxhash support (already in requirements.txt but not wired up in code)
- [ ] Implement chunked/buffered copy for very large files with progress per-file
- [ ] Add memory-mapped I/O option for hashing large files

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

## Blocked

_(none currently)_

## Releases

- v1.0.1 — current — SyncFlow with UE detection, SHA-256 verification, multi-dest sync, direction toggle
- v1.1.0 — planned 2026-09-01 — Remote sync targets and cross-platform improvements

## Notes

- Settings persist to ~/.filesync_settings.json
- Standalone .exe via PyInstaller available on Releases page; Python 3.9+ required for source runs
- Two GUI frontends exist: `filesync.py` (tkinter, legacy) and `filesync_qt.py` (PyQt6, primary)
- Core sync engine logic is duplicated between both files — should be extracted
- xxhash is listed in requirements.txt but not actually used in the code
- build.bat only builds the tkinter version; should be updated for PyQt6 version
