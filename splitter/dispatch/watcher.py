"""
dispatch/watcher.py
--------------------
Monitors the /data/input/ directory via watchdog, manages the processing
thread pool, and prints the startup banner.

Contains:
  - _file_executor     — shared ThreadPoolExecutor (PDFHandler + scan_existing)
  - PDFHandler         — watchdog FileSystemEventHandler
  - handle_non_pdf     — moves non-PDF files to /error
  - scan_existing      — processes files already present at startup
  - start_watcher      — initialises directories, logs active configuration,
                         starts the Observer and runs scan_existing

Internal dependencies:
  - dispatch.config    (DATA_DIR, INPUT_DIR, ERROR_DIR, etc., get_config,
                        ensure_dirs, update_dir_paths, _counter_value,
                        _COUNTER_FILE, CONFIG_FILE, POST_PROCESS_SCRIPT,
                        POST_PROCESS_TIMEOUT, MAX_WORKER_THREADS,
                        SCANNER, DPI, UPSCALE, BARCODE_DPI_SCAN)
  - dispatch.i18n      (t)
  - dispatch.state     (log_event, _task_update)
  - dispatch.processing (process_file, wait_until_stable)
  - dispatch.hook      (_run_post_process_hook)
  - dispatch.webhook   (_fire_webhook)
"""

import os
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from dispatch.config import (
    BARCODE_DPI_SCAN, CONFIG_FILE, DATA_DIR, DPI, ERROR_DIR,
    INPUT_DIR, MAX_WORKER_THREADS, OUTPUT_DIR, POST_PROCESS_SCRIPT,
    POST_PROCESS_TIMEOUT, PROCESSED_DIR, SCANNER, UPSCALE,
    _COUNTER_FILE, _counter_value, get_config,
    update_config, update_dir_paths,
)
from dispatch.processing import (
    process_file, wait_until_stable, ensure_dirs,
)
from dispatch.i18n import t
from dispatch.state import _task_update, log_event
from dispatch.hook    import _run_post_process_hook
from dispatch.webhook import _fire_webhook

# ---------------------------------------------------------------------------
# File monitor (/data/input/ directory watcher)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Worker thread pool — shared between PDFHandler and scan_existing
# ---------------------------------------------------------------------------
# Bounds the number of live threads to MAX_WORKER_THREADS (env var).
# Without a pool, each file event creates a new thread; on restart with 100
# queued files that would be 100 threads consuming ~800 MB of stack.
# The processing semaphore (_processing_semaphore) still gates concurrent
# PDF rendering; the pool only caps thread creation.

_file_executor = ThreadPoolExecutor(
    max_workers=MAX_WORKER_THREADS,
    thread_name_prefix="pdf-worker",
)


class PDFHandler(FileSystemEventHandler):
    """Watchdog event handler for /data/input/.

    On creation or rename (move) of a non-directory file, submits it to
    _file_executor: process_file for .pdf files, handle_non_pdf for
    everything else. Sub-directories are not monitored
    (recursive=False in start_watcher)."""

    def _handle(self, path_str):
        """Submit a file path to process_file (.pdf) or handle_non_pdf
        (any other extension) via the shared _file_executor thread pool.
        Never blocks the watchdog thread and never creates a per-file thread."""
        path = Path(path_str)
        if path.suffix.lower() == ".pdf":
            _file_executor.submit(process_file, path)
        else:
            # Non-PDF file: wait for stabilisation then move to /error
            _file_executor.submit(handle_non_pdf, path)

    def on_created(self, event):
        """Nouveau fichier depose dans /data/input/ (copie, upload, etc.)."""
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        """Fichier renomme ou deplace a l'interieur de /data/input/ (certains
        clients FTP/SFTP ecrivent sous un nom temporaire puis renomment)."""
        if not event.is_directory:
            self._handle(event.dest_path)


def handle_non_pdf(path: Path):
    """Wait for a non-PDF file to stabilise, then move it to /error."""
    # Silently ignore internal files (should no longer occur with the in-memory dict)
    if path.suffix in ('.email_trigger', '.tmp', '.part'):
        try: path.unlink(missing_ok=True)
        except: pass
        return
    fname = path.name
    # Avoid double processing (same mechanism as process_file)
    with processing_lock:
        if fname in processing:
            return
        processing.add(fname)
    try:
        if not wait_until_stable(path):
            return
        if not path.exists():
            return
        ERROR_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = ERROR_DIR / f"{path.stem}_{ts}_NOT_PDF{path.suffix}"
        try:
            shutil.copy2(str(path), str(dest))
            os.chmod(dest, 0o664)
            path.unlink()
            log_event("warning",
                      t("log.non_pdf_moved", filename=fname),
                      fname)
            _task_update(fname, status="error", error="not a PDF file")
            _run_post_process_hook(source_file=fname, status="error", error_msg="not a PDF file")
            _fire_webhook(source_file=fname, status="error", error_msg="not a PDF file")
        except Exception as e:
            log_event("error",
                      t("log.non_pdf_move_failed", filename=fname, message=e),
                      fname)
    finally:
        with processing_lock:
            processing.discard(fname)


def scan_existing():
    """Process files already present in /data/input/ at startup
    (e.g. files deposited while the container was restarted).

    Logs the count of PDFs and non-PDF files found (or an "empty folder"
    message), then submits each one to process_file / handle_non_pdf via
    the thread pool, exactly as PDFHandler would for a newly deposited file."""
    all_files = [p for p in INPUT_DIR.iterdir() if p.is_file()]
    pdfs      = [p for p in all_files if p.suffix.lower() == ".pdf"]
    non_pdfs  = [p for p in all_files if p.suffix.lower() != ".pdf"]

    if not all_files:
        log_event("info", t("log.no_existing_files", path=INPUT_DIR))
        return

    log_event("info", t("log.existing_files_found", pdfs=len(pdfs), non_pdfs=len(non_pdfs)))
    for p in pdfs:
        _file_executor.submit(process_file, p)
    for p in non_pdfs:
        _file_executor.submit(handle_non_pdf, p)


def start_watcher():
    """Initialise the /data/input/ watcher and print the startup banner.

    Creates required directories (ensure_dirs), recomputes
    INPUT_DIR / OUTPUT_DIR / ERROR_DIR / PROCESSED_DIR from the
    configuration (update_dir_paths), then logs a full summary of the
    active configuration: config source (persistent file or defaults),
    directory paths, barcode scanner settings, triggers, options
    (subdirectories, archiving), counter, and EMAIL_SECRET status.

    Removes the legacy .splitter_counter.json file (v2 format, superseded
    by the counter in .splitter_config.json) if it is still present from
    an earlier installation.

    Starts the watchdog Observer (non-recursive) with PDFHandler on
    INPUT_DIR, then calls scan_existing() to process any files already
    present. Stores the started Observer in _observer so that
    _restart_watcher can stop it cleanly."""
    ensure_dirs()
    # Read persistent config (file on volume) or fall back to defaults
    cfg = get_config()
    config_source = t("log.config_source_persistent") if CONFIG_FILE.exists() else t("log.config_source_defaults")
    mode = f"{cfg['split_values']}" if cfg.get("split_values") else t("log.all_barcodes")

    log_event("info", "=" * 55)
    log_event("info", t("log.startup_title"))
    log_event("info", t("log.startup_config_source", value=config_source))
    update_dir_paths()
    log_event("info", t("log.startup_root", value=DATA_DIR))
    log_event("info", t("log.startup_input", value=INPUT_DIR))
    log_event("info", t("log.startup_output", value=OUTPUT_DIR))
    log_event("info", t("log.startup_errors", value=ERROR_DIR))
    log_event("info", t("log.startup_archived", value=PROCESSED_DIR))
    log_event("info", t("log.startup_scanner", scanner=SCANNER, dpi=DPI, upscale=UPSCALE)
    + (f", scan_dpi={BARCODE_DPI_SCAN}" if BARCODE_DPI_SCAN != DPI else ""))
    log_event("info", t("log.startup_triggers", value=mode))
    log_event("info", t("log.startup_subdirs", value=cfg.get('subdirs_by_trigger', False)))
    log_event("info", t("log.startup_delete_source", value=cfg.get('delete_source', True)))
    # Counter is now stored in _COUNTER_FILE; load lazily if not yet initialised.
    _ctr_display = _counter_value if _counter_value is not None else (
        int(_COUNTER_FILE.read_text().strip()) if _COUNTER_FILE.exists()
        else cfg.get("counter", 0))
    log_event("info", t("log.startup_counter", value=_ctr_display))
    config_state = t("common.exists") if CONFIG_FILE.exists() else t("log.will_be_created")
    log_event("info", t("log.startup_config_file", path=CONFIG_FILE, state=config_state))
    # Check email secret at startup
    email_secret_env = os.getenv("EMAIL_SECRET", "")
    if email_secret_env:
        log_event("info", t("log.startup_email_secret_set", chars=len(email_secret_env)))
    else:
        log_event("warning", t("log.startup_email_secret_unset"))
    # Auto-generate API key if none is configured
    env_api_key = os.getenv("API_KEY", "").strip()
    if env_api_key:
        log_event("info", f"API key: set via API_KEY env var ({len(env_api_key)} chars)")
    else:
        cfg2 = get_config()
        if not cfg2.get("api_key"):
            new_key = secrets.token_hex(32)   # 64-char hex string
            update_config({"api_key": new_key})
            log_event("info", f"API key: auto-generated and saved to config")
        else:
            log_event("info", f"API key: loaded from config ({len(cfg2['api_key'])} chars)")
    if POST_PROCESS_SCRIPT:
        hook_ok = Path(POST_PROCESS_SCRIPT).exists() and os.access(POST_PROCESS_SCRIPT, os.X_OK)
        hook_status = "OK" if hook_ok else "NOT FOUND or not executable"
        log_event("info", f"Post-process hook: {POST_PROCESS_SCRIPT} [{hook_status}] (timeout: {POST_PROCESS_TIMEOUT}s)")
    log_event("info", "=" * 55)

    # Remove any stale .tmp from a counter write interrupted by a crash
    _ctr_tmp = _COUNTER_FILE.with_suffix(".tmp")
    if _ctr_tmp.exists():
        try: _ctr_tmp.unlink()
        except Exception: pass

    global _observer
    obs = Observer()
    obs.schedule(PDFHandler(), str(INPUT_DIR), recursive=False)
    obs.start()
    _observer = obs
    log_event("info", t("log.watcher_started"))
    scan_existing()

# ---------------------------------------------------------------------------
# Global Observer — managed here so that both _restart_watcher (called
# from routes) and the entrypoint (app.py) can access it without circular
# imports. The entrypoint calls stop_watcher() for a clean shutdown.
# ---------------------------------------------------------------------------

_observer = None
_observer_lock = threading.Lock()


def _restart_watcher() -> None:
    """Stop the current Observer and start a new one watching INPUT_DIR.
    Called from routes after a configuration change that affects the watched
    directory (api_dirs_rename) or the global config (api_config_update).
    """
    global _observer
    with _observer_lock:
        if _observer:
            _observer.stop()
            _observer.join()
        obs = Observer()
        obs.schedule(PDFHandler(), str(INPUT_DIR), recursive=False)
        obs.start()
        _observer = obs
        log_event("info", t("log.watcher_restarted", path=INPUT_DIR))


def stop_watcher() -> None:
    """Cleanly stop the Observer. Called by the entrypoint on shutdown."""
    with _observer_lock:
        if _observer:
            _observer.stop()
            _observer.join()
