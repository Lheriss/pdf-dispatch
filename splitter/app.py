#!/usr/bin/env python3
"""
pdf-dispatch
=============

Flask service that monitors a folder (`/data/input/`), receives PDFs via
web upload or email (IMAP), and splits them into separate documents based
on codes (1D barcodes or QR codes) detected on specific pages ("triggers").
Output files are named and organised according to a configuration editable
entirely from the web interface (port 5000, exposed as 5880 by default in
docker-compose.yml).

Flask application creation logic lives in dispatch/__init__.py
(create_app). This file is the service entry point and re-exports
the symbols required by the Python unit tests (test_python_core.py).
"""

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Application Factory
# ---------------------------------------------------------------------------

from dispatch import create_app   # noqa: E402

app = create_app()

# ---------------------------------------------------------------------------
# Re-exports — symbols expected by test_python_core.py and external tooling
# ---------------------------------------------------------------------------

from dispatch.i18n import (
    SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, I18N_DIR, TRANSLATIONS, t,
)

from dispatch.crypto import (
    _EMAIL_SECRET_KEY, _get_email_secret, _get_fernet,
    encrypt_password, decrypt_password,
)

from dispatch.config import (
    DATA_DIR, APP_VERSION, FORBIDDEN_CHARS,
    INPUT_DIR, OUTPUT_DIR, ERROR_DIR, PROCESSED_DIR,
    DPI, BARCODE_DPI_SCAN, UPSCALE, SCANNER,
    MAX_LOG, FILE_STABLE_TIMEOUT, FILE_STABLE_INTERVAL,
    MAX_UPLOAD_MB, MAX_PAGES,
    MAX_CONCURRENT_PROCESSING, MAX_WORKER_THREADS,
    API_TASK_TIMEOUT, POST_PROCESS_SCRIPT, POST_PROCESS_TIMEOUT,
    SSRF_PROTECTION,
    CONFIG_FILE, _COUNTER_FILE,
    CONFIG_DEFAULTS, config_lock,
    get_dirs, update_dir_paths, _safe_relative_path,
    _validate_and_sanitize_config,
    _is_glob_pattern, _match_trigger,
    _seed_language,
    load_config, save_config, get_config, update_config,
    _counter_lock, _counter_value,
    _counter_persist, _counter_load, next_counter,
    _load_stats, _save_stats,
    log,
)

from dispatch.state import (
    state_lock, processing_lock, _processing_semaphore, processing,
    state,
    _MAX_TASKS, _tasks, _filename_to_task, _tasks_lock,
    _task_create, _task_update,
    log_event,
    _file_config_overrides, _store_file_override, _pop_file_override,
)

from dispatch.processing import (
    MAX_FILENAME_LEN, validate_filename_tokens, build_filename,
    NO_CODE_TRIGGER, ensure_dirs, _pattern_to_dirname, get_output_dir,
    wait_until_stable, is_valid_pdf, move_to_error,
    decode_zxing, decode_pyzbar, decode_page, find_split_pages,
    add_pdf_metadata,
    generate_separator_pdf, process_file,
)

from dispatch.email_poller import (
    _email_config_signature, _email_find_duplicate, _email_find_name_conflict,
    _proc_ids_load, _proc_ids_save, _proc_ids_delete,
    start_email_poller, stop_email_poller,
)

from dispatch.hook    import _run_post_process_hook
from dispatch.webhook import (
    _build_webhook_payload, _ssrf_safe, _ssrf_blocked_response,
    _deliver_webhook, _fire_webhook,
)

from dispatch.watcher import (
    _file_executor,
    PDFHandler,
    start_watcher,
)
from dispatch.retention import start_retention, stop_retention

from dispatch.routes.upload import _parse_upload_override  # noqa: F401 (tests)

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    start_watcher()
    start_email_poller()
    start_retention()
    try:
        from waitress import serve
        log.info("Demarrage du serveur (waitress, 4 threads)")
        serve(app, host="0.0.0.0", port=5000, threads=4)
    finally:
        stop_email_poller()
        stop_retention()
        from dispatch.watcher import stop_watcher
        stop_watcher()
        _file_executor.shutdown(wait=False)
