"""
dispatch/retention.py
----------------------
Automatic retention: a background daemon thread that periodically deletes
old files from the output sub-folders so /data does not fill up silently
over months of use.

Two independent retentions, each in days, read from the environment at
startup (see dispatch.config):
  - RETENTION_DAYS_PROCESSED → /data/output/processed/
  - RETENTION_DAYS_ERROR     → /data/output/error/

A value of 0 disables cleanup for that folder. Files whose modification
time is older than the threshold are deleted; empty sub-directories left
behind (e.g. per-trigger sub-folders) are pruned too. The scan runs every
RETENTION_SCAN_INTERVAL seconds (default 6h). The thread is only started
when at least one retention is > 0, so the default install behaviour
("keep everything") is unchanged unless the operator opts in.

Public API mirrors the email poller for consistency:
  - start_retention()  — start the daemon thread (no-op if nothing enabled)
  - stop_retention()   — signal the thread to stop after the current cycle
"""
import threading
import time
from datetime import datetime
from pathlib import Path

from dispatch.config import (
    ERROR_DIR, PROCESSED_DIR,
    RETENTION_DAYS_ERROR, RETENTION_DAYS_PROCESSED, RETENTION_SCAN_INTERVAL,
    get_dirs,
)
from dispatch.i18n import t
from dispatch.state import log_event

_retention_thread: threading.Thread | None = None
_retention_stop = threading.Event()


def _prune_dir(root: Path, max_age_days: int, now: float) -> tuple[int, int]:
    """Delete files under `root` older than `max_age_days`, then remove any
    empty sub-directories left behind. Returns (files_deleted, bytes_freed).

    Directory paths are resolved fresh from the current configuration by the
    caller, so a runtime folder rename is honoured on the next cycle. Never
    deletes `root` itself, only its contents.
    """
    if max_age_days <= 0 or not root.exists():
        return 0, 0

    cutoff = now - max_age_days * 86400
    files_deleted = 0
    bytes_freed = 0

    # Delete old files first (deepest paths handled naturally by rglob).
    for path in root.rglob("*"):
        if _retention_stop.is_set():
            break
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            try:
                size = st.st_size
                path.unlink()
                files_deleted += 1
                bytes_freed += size
            except OSError as e:
                log_event("warning",
                          t("log.retention_delete_failed",
                            path=str(path), message=e))

    # Prune now-empty sub-directories (bottom-up), but never `root` itself.
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            try:
                path.rmdir()
            except OSError:
                pass
        except OSError:
            pass

    return files_deleted, bytes_freed


def _run_cleanup_cycle() -> None:
    """Run one retention pass over both folders and log a summary when
    anything was actually removed. Paths are re-read from the live config
    each cycle so a directory rename via the UI/API is picked up."""
    now = time.time()
    dirs = get_dirs()
    targets = (
        (dirs.get("processed", PROCESSED_DIR), RETENTION_DAYS_PROCESSED, "processed"),
        (dirs.get("error", ERROR_DIR),         RETENTION_DAYS_ERROR,     "error"),
    )
    for root, days, label in targets:
        if days <= 0:
            continue
        deleted, freed = _prune_dir(Path(root), days, now)
        if deleted:
            log_event("info",
                      t("log.retention_cleaned",
                        folder=label, count=deleted,
                        mb=round(freed / (1024 * 1024), 1), days=days))


def _retention_loop() -> None:
    """Daemon loop: run a cleanup cycle immediately, then every
    RETENTION_SCAN_INTERVAL seconds until signalled to stop. The wait is
    interruptible so shutdown is prompt."""
    log_event("info",
              t("log.retention_started",
                processed=RETENTION_DAYS_PROCESSED,
                error=RETENTION_DAYS_ERROR,
                hours=round(RETENTION_SCAN_INTERVAL / 3600, 1)))
    while not _retention_stop.is_set():
        try:
            _run_cleanup_cycle()
        except Exception as e:  # never let the thread die on a transient error
            log_event("error", t("log.retention_cycle_failed", message=e))
        # Interruptible sleep: wakes immediately when stop is set.
        _retention_stop.wait(RETENTION_SCAN_INTERVAL)


def start_retention() -> None:
    """Start the retention daemon thread. No-op (with an info log) when both
    retentions are disabled, so the default install keeps every file."""
    global _retention_thread
    if RETENTION_DAYS_PROCESSED <= 0 and RETENTION_DAYS_ERROR <= 0:
        log_event("info", t("log.retention_disabled"))
        return
    _retention_stop.clear()
    _retention_thread = threading.Thread(
        target=_retention_loop, daemon=True, name="retention")
    _retention_thread.start()


def stop_retention() -> None:
    """Signal the retention thread to stop after the current cycle."""
    _retention_stop.set()
    if _retention_thread is not None:
        _retention_thread.join(timeout=5)
