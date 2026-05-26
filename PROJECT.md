## Goals

- [ ] Stabilize SyncFlow v1.0.x release with full UE project support — 2026-06-15
- [ ] Add cloud/network sync targets (S3, SMB, SSH) — 2026-08-01
- [ ] Release v1.1.0 with multi-platform improvements — 2026-09-01

## In Progress

- [ ] Polish PyQt6 dark-theme UI for edge cases and multi-monitor setups
- [ ] Improve error handling and logging for failed sync operations

## To Do

- [ ] Add SSH/SMB remote destination support
- [ ] Implement incremental sync with file watcher (inotify/FSEvents)
- [ ] Add sync profile presets for common UE and non-UE workflows
- [ ] Create macOS and Linux executable builds (currently Windows-focused)
- [ ] Add conflict resolution UI when source and destination both modified

## Done

- [x] SHA-256 hash-based file integrity verification
- [x] Multi-destination parallel sync with UE auto-exclusion
- [x] PyQt6 dark-theme UI with drag-and-drop and real-time progress

## Blocked



## Releases

- v1.0.1 — current — SyncFlow with UE detection, SHA-256 verification, multi-dest sync
- v1.1.0 — planned 2026-09-01 — Remote sync targets and cross-platform improvements

## Notes

- Settings persist to ~/.filesync_settings.json
- Standalone .exe via PyInstaller available on Releases page; Python 3.9+ required for source runs
