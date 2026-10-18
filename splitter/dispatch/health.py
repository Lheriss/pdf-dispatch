"""
dispatch/health.py
------------------
Real health check behind GET /healthz (public, used by the Docker
HEALTHCHECK and by external monitors such as Uptime Kuma).

Each check returns one of:
  "ok"        working
  "failed"    broken → /healthz answers 503
  "disabled"  feature not configured (e.g. no enabled email account)
  "n/a"       background service not started in this process (only in the
              test suite, which uses the Flask app without app.py's
              entrypoint); never a failure

Checks:
  watcher       the watchdog Observer on the input folder is alive
  input_dir     the input folder exists
  data_dir      a file can be created and deleted in DATA_DIR (volume
                mounted, writable, not read-only)
  disk          free space in DATA_DIR ≥ HEALTHZ_MIN_FREE_MB (default 100)
  email_poller  when an email account is enabled: the IMAP thread is alive
                and its loop ran recently (not stuck)
  retention     when retention is enabled: the retention thread is alive

The response only carries the check names and their status — no paths,
versions or error details (the endpoint is public). Details go to the log,
once per change of state.
"""

import os
import shutil
import tempfile
import time

from dispatch import config as _config
from dispatch.config import DATA_DIR, get_config, log

MIN_FREE_MB = int(os.getenv("HEALTHZ_MIN_FREE_MB", "100"))
# The IMAP loop wakes every 30 s; a single cycle can take a while on a large
# mailbox, so only a loop silent for this long counts as stuck.
EMAIL_STALL_SECONDS = 30 * 60

_last_failed: frozenset = frozenset()


def _check_watcher() -> str:
    from dispatch import watcher
    # A restart (configuration change) swaps the Observer under the lock:
    # wait for it rather than catch the brief stopped window.
    if not watcher._observer_lock.acquire(timeout=2):
        return "ok"   # restart in progress
    try:
        obs = watcher._observer
        if obs is None:
            return "n/a"
        return "ok" if obs.is_alive() else "failed"
    finally:
        watcher._observer_lock.release()


def _check_input_dir() -> str:
    # Read at call time: update_dir_paths() rebinds INPUT_DIR after a rename.
    return "ok" if _config.INPUT_DIR.is_dir() else "failed"


def _check_data_dir() -> str:
    try:
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, prefix=".healthz-", delete=True) as f:
            f.write(b"ok")
            f.flush()
        return "ok"
    except OSError as exc:
        log.warning("healthz: DATA_DIR not writable: %s", exc)
        return "failed"


def _check_disk() -> str:
    try:
        free_mb = shutil.disk_usage(DATA_DIR).free // (1024 * 1024)
    except OSError as exc:
        log.warning("healthz: disk usage unavailable: %s", exc)
        return "failed"
    if free_mb < MIN_FREE_MB:
        log.warning("healthz: only %d MB free in DATA_DIR (minimum %d)", free_mb, MIN_FREE_MB)
        return "failed"
    return "ok"


def _check_email_poller() -> str:
    if not any(ec.get("enabled") for ec in get_config().get("email_configs", [])):
        return "disabled"
    from dispatch import email_poller
    thread = email_poller._email_thread
    if thread is None:
        return "n/a"
    if not thread.is_alive() or email_poller._email_stop.is_set():
        return "failed"
    beat = email_poller._email_heartbeat
    if beat and time.time() - beat > EMAIL_STALL_SECONDS:
        log.warning("healthz: IMAP poller loop silent for %d s", int(time.time() - beat))
        return "failed"
    return "ok"


def _check_retention() -> str:
    from dispatch import retention
    if retention.RETENTION_DAYS_PROCESSED <= 0 and retention.RETENTION_DAYS_ERROR <= 0:
        return "disabled"
    thread = retention._retention_thread
    if thread is None:
        return "n/a"
    return "ok" if thread.is_alive() else "failed"


CHECKS = {
    "watcher":      _check_watcher,
    "input_dir":    _check_input_dir,
    "data_dir":     _check_data_dir,
    "disk":         _check_disk,
    "email_poller": _check_email_poller,
    "retention":    _check_retention,
}


def run_checks() -> dict:
    """Run every check. Returns {"ok": bool, "checks": {...}, "failed": [...]}."""
    global _last_failed
    checks = {}
    for name, fn in CHECKS.items():
        try:
            checks[name] = fn()
        except Exception as exc:   # a broken check must not break /healthz
            log.warning("healthz: check %s raised %s: %s", name, type(exc).__name__, exc)
            checks[name] = "failed"
    failed = [n for n, s in checks.items() if s == "failed"]
    now_failed = frozenset(failed)
    if now_failed != _last_failed:   # log state changes only
        if failed:
            log.warning("healthz: unhealthy — failed checks: %s", ", ".join(failed))
        elif _last_failed:
            log.info("healthz: healthy again")
        _last_failed = now_failed
    return {"ok": not failed, "checks": checks, "failed": failed}
