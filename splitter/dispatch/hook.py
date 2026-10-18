"""
dispatch/hook.py
-----------------
Post-processing hook: runs an external script after each file processed
by pdf-dispatch (success or error).

Contains only _run_post_process_hook(). Called from process_file
(dispatch/processing.py) after each processing run.

Internal dependencies:
  - dispatch.config  (log, DATA_DIR, POST_PROCESS_SCRIPT, POST_PROCESS_TIMEOUT)
  - dispatch.state   (log_event)
"""

import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from dispatch.config import DATA_DIR, POST_PROCESS_SCRIPT, POST_PROCESS_TIMEOUT, log
from dispatch.state import log_event


# pdf-dispatch's own secrets, never passed to the post-processing script.
_HOOK_HIDDEN_ENV = frozenset({"EMAIL_SECRET", "API_KEY", "APP_PASSWORD"})


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the hook's whole process group, then reap the script."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass   # pipes held open by a process outside the group: give up


def _run_post_process_hook(
    source_file: str,
    status: str,
    triggers: list[str] | None = None,
    outputs: list[str] | None  = None,
    docs_count: int = 0,
    error_msg: str  = "",
) -> None:
    """Execute the post-processing hook script defined by POST_PROCESS_SCRIPT.

    Called after every file processed by pdf-dispatch (success or error).
    Runs the script in a subprocess with a timeout; stdout/stderr are forwarded
    line-by-line to the activity log (verbose level).

    Environment variables passed to the script:

      PDF_DISPATCH_SOURCE      — original filename (basename only)
      PDF_DISPATCH_STATUS      — "success" | "error"
      PDF_DISPATCH_TRIGGERS    — comma-separated list of detected trigger values
                                 (empty string when no code was detected)
      PDF_DISPATCH_OUTPUTS     — comma-separated absolute paths of produced files
                                 (empty string on error)
      PDF_DISPATCH_DOCS_COUNT  — number of output documents produced (0 on error)
      PDF_DISPATCH_TIMESTAMP   — processing date/time (ISO 8601, second precision)
      PDF_DISPATCH_ERROR       — error description (empty string on success)
      PDF_DISPATCH_DATA_DIR    — value of DATA_DIR (for path calculations in script)

    The script must be executable (`chmod +x`).  If it exits with a non-zero
    code the event is logged as a warning but processing is unaffected.
    A script that takes longer than POST_PROCESS_TIMEOUT seconds is killed
    together with every process it started (it runs in its own process
    group), and a warning is logged.

    The script inherits the container environment EXCEPT pdf-dispatch's own
    secrets (_HOOK_HIDDEN_ENV: EMAIL_SECRET, API_KEY, APP_PASSWORD), so it
    can still read variables you add for it (e.g. a notification token).
    """
    if not POST_PROCESS_SCRIPT:
        return

    script_path = Path(POST_PROCESS_SCRIPT)
    if not script_path.exists():
        log.warning("POST_PROCESS_SCRIPT not found: %s", POST_PROCESS_SCRIPT)
        return
    if not os.access(script_path, os.X_OK):
        log.warning("POST_PROCESS_SCRIPT not executable: %s", POST_PROCESS_SCRIPT)
        return

    env = {
        **{k: v for k, v in os.environ.items() if k not in _HOOK_HIDDEN_ENV},
        "PDF_DISPATCH_SOURCE":     source_file,
        "PDF_DISPATCH_STATUS":     status,
        "PDF_DISPATCH_TRIGGERS":   ",".join(triggers or []),
        "PDF_DISPATCH_OUTPUTS":    ",".join(outputs or []),
        "PDF_DISPATCH_DOCS_COUNT": str(docs_count),
        "PDF_DISPATCH_TIMESTAMP":  datetime.now().isoformat(timespec="seconds"),
        "PDF_DISPATCH_ERROR":      error_msg,
        "PDF_DISPATCH_DATA_DIR":   str(DATA_DIR),
    }

    try:
        # Own session / process group: on timeout the whole group is killed,
        # including background children the script may have started.
        proc = subprocess.Popen(
            [str(script_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=POST_PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            log_event("warning",
                      f"Post-process hook timed out after {POST_PROCESS_TIMEOUT}s",
                      source_file)
            return
        for line in stdout.splitlines():
            if line.strip():
                log_event("info", f"[hook] {line.strip()}", source_file, verbose=True)
        for line in stderr.splitlines():
            if line.strip():
                log_event("warning", f"[hook] {line.strip()}", source_file, verbose=True)
        if proc.returncode != 0:
            log_event("warning",
                      f"Post-process hook exited with code {proc.returncode}",
                      source_file)
    except Exception as exc:
        log_event("warning", f"Post-process hook error: {exc}", source_file)
