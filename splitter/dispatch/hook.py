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
import subprocess
from datetime import datetime
from pathlib import Path

from dispatch.config import DATA_DIR, POST_PROCESS_SCRIPT, POST_PROCESS_TIMEOUT, log
from dispatch.state import log_event


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
    and a warning is logged.
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
        **os.environ,
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
        result = subprocess.run(
            [str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=POST_PROCESS_TIMEOUT,
        )
        for line in result.stdout.splitlines():
            if line.strip():
                log_event("info", f"[hook] {line.strip()}", source_file, verbose=True)
        for line in result.stderr.splitlines():
            if line.strip():
                log_event("warning", f"[hook] {line.strip()}", source_file, verbose=True)
        if result.returncode != 0:
            log_event("warning",
                      f"Post-process hook exited with code {result.returncode}",
                      source_file)
    except subprocess.TimeoutExpired:
        log_event("warning",
                  f"Post-process hook timed out after {POST_PROCESS_TIMEOUT}s",
                  source_file)
    except Exception as exc:
        log_event("warning", f"Post-process hook error: {exc}", source_file)
