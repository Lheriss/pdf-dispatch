"""
dispatch/state.py
------------------
In-memory shared state across all pdf-dispatch threads.

Contains:
  - Shared locks (state_lock, processing_lock, _processing_semaphore)
  - Event log and statistics: ``state`` dict
    {"events": deque, "stats": dict, "queue": dict}
  - Async upload task tracking (_tasks, _task_create, _task_update)
  - log_event(): writes to the activity log visible in the web interface

All variables in this module are objects mutated in place (dicts, deques,
locks, sets). They can be safely imported via ``from dispatch.state
import ...`` without stale-binding risk, because none of them is ever
rebound by a ``global x = ...`` statement inside a function.

Internal dependencies:
  - dispatch.config  (MAX_LOG, MAX_CONCURRENT_PROCESSING, _load_stats,
                      get_config, log)
  No dependency on routes (strict one-way import direction).
"""

import threading
import uuid
from collections import OrderedDict, deque
from datetime import datetime

from dispatch.config import (
    MAX_CONCURRENT_PROCESSING,
    MAX_LOG,
    _load_stats,
    get_config,
    log,
)

# ---------------------------------------------------------------------------
# Shared locks
# ---------------------------------------------------------------------------

state_lock            = threading.Lock()
processing_lock       = threading.Lock()

# Semaphore that limits the number of PDFs being rendered concurrently.
# Acquired at the start of process_file, released in its finally block.
_processing_semaphore = threading.Semaphore(MAX_CONCURRENT_PROCESSING)

# Set of filenames currently being processed (duplicate-processing guard).
processing: set = set()

# ---------------------------------------------------------------------------
# Event log, statistics, processing queue
# ---------------------------------------------------------------------------

state: dict = {
    "events": deque(maxlen=MAX_LOG),
    "stats":  _load_stats(),
    "queue":  {},  # fname → True, insertion-ordered; O(1) add/discard
}

# ---------------------------------------------------------------------------
# Async task tracking (uploads via /api/upload)
#
# Tasks are created on upload and updated by the watchdog.
# Files deposited directly in INPUT_DIR (watchdog, email) are not tracked
# — their lookup simply returns None.
# ---------------------------------------------------------------------------

_MAX_TASKS          = 200               # nombre maximum de tâches conservées
_tasks              = OrderedDict()     # task_id → task dict, ordre d'insertion
_filename_to_task: dict = {}            # dest.name → task_id
_tasks_lock         = threading.Lock()


def _task_create(filename: str, config_override: dict | None = None) -> str:
    """Create a pending task for an uploaded file and return its task ID.

    config_override — per-file config dict supplied by the API caller
    (if any); stored in the task for visibility and included in the
    outbound webhook payload. None for files not uploaded via the API.
    """
    task_id = str(uuid.uuid4())
    now     = datetime.now().isoformat(timespec="seconds")
    task    = {
        "id":             task_id,
        "filename":       filename,
        "status":         "pending",
        "created_at":     now,
        "updated_at":     now,
        "triggers":       [],
        "outputs":        [],
        "docs_count":     0,
        "error":          "",
        "config_override": config_override or {},
    }
    with _tasks_lock:
        # Evict oldest tasks when the store is full
        while len(_tasks) >= _MAX_TASKS:
            oldest_id = next(iter(_tasks))
            _tasks.pop(oldest_id)
        _tasks[task_id]             = task
        _filename_to_task[filename] = task_id
    return task_id


def _task_update(filename: str, **fields) -> None:
    """Update the named fields of the task associated with *filename*.
    No-op if no task is associated with the filename (file not uploaded via the API)."""
    with _tasks_lock:
        task_id = _filename_to_task.get(filename)
        if task_id and task_id in _tasks:
            _tasks[task_id].update(fields)
            _tasks[task_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Web interface activity log
# ---------------------------------------------------------------------------

def log_event(level: str, message: str, filename: str = None,
              verbose: bool = False) -> None:
    """Append an entry to the activity log visible in the web interface.

    Also writes to the Python logger (same level) for system logs.
    If verbose=True, the entry only appears in the web interface when
    log_verbose is enabled in the configuration.
    """
    getattr(log, level.lower(), log.info)(message)
    if verbose:
        try:
            if not get_config().get("log_verbose", False):
                return
        except Exception:
            pass
    entry = {
        "ts":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level":    level,
        "message":  message,
        "filename": filename,
    }
    with state_lock:
        state["events"].appendleft(entry)

# ---------------------------------------------------------------------------
# Per-file configuration overrides
# ---------------------------------------------------------------------------
# In-memory mapping: dest.filename → partial config dict.
# Lifecycle: stored by api_upload, consumed (popped) at the start of
# process_file. Lives here in state.py as pure shared state (not
# email-specific), used by both api_upload (routes) and process_file.

_file_config_overrides: dict = {}
_file_config_overrides_lock = threading.Lock()


def _store_file_override(filename: str, override: dict) -> None:
    """Associate a temporary config override with *filename*.

    Called from api_upload after the file is saved to INPUT_DIR.
    The override must already be validated before being stored here.
    """
    with _file_config_overrides_lock:
        _file_config_overrides[filename] = override


def _pop_file_override(filename: str) -> dict | None:
    """Consume and return the config override for *filename*, or None.

    Called once at the start of process_file (after wait_until_stable).
    The entry is removed on the first call — subsequent calls return None,
    which means the global config is used as-is.
    """
    with _file_config_overrides_lock:
        return _file_config_overrides.pop(filename, None)

# ---------------------------------------------------------------------------
# In-memory email triggers
# ---------------------------------------------------------------------------
# Temporary mapping: filename → default_trigger for PDFs received by email
# (no barcode). Populated by _imap_process, consumed by process_file.

_email_triggers: dict = {}
_email_triggers_lock = threading.Lock()
