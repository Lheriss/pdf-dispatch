"""
dispatch/config.py
-------------------
Environment constants, persistent configuration and derived state.

Contains:
  - Constants read from os.getenv() at startup (DATA_DIR, MAX_PAGES,
    DPI, FILE_STABLE_TIMEOUT, etc.).
  - CONFIG_DEFAULTS and the persistent configuration machinery:
    load_config / save_config / get_config / update_config,
    _validate_and_sanitize_config.
  - Path helpers: get_dirs(), update_dir_paths(), _safe_relative_path().
  - Trigger matching: _is_glob_pattern(), _match_trigger().
  - Document counter: _counter_persist/load/next.
  - Persisted statistics: _load_stats(), _save_stats().

Internal dependencies: dispatch.i18n (t()).
No dependency on state.py or routes — this module is imported by almost
everything else and must remain free of circular imports.
"""

import fnmatch
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path

from dispatch.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, t

log = logging.getLogger("pdf-dispatch")

# ---------------------------------------------------------------------------
# Base paths (from environment variables)
# ---------------------------------------------------------------------------

# Single root folder mounted via docker-compose
DATA_DIR    = Path(os.getenv("DATA_DIR",    "/data"))
APP_VERSION = os.getenv("APP_VERSION", "unknown").strip()  # injected at build time via ARG GIT_SHA → ENV APP_VERSION in the Dockerfile (release tag or `git describe` dev version)

# Characters forbidden in file/folder names
FORBIDDEN_CHARS = '[<>:"/\\|?*\\x00-\\x1f]'

# Effective paths are read from persistent config
# These variables are updated dynamically via get_dirs()
INPUT_DIR     = DATA_DIR / "input"
OUTPUT_DIR    = DATA_DIR / "output"
ERROR_DIR     = DATA_DIR / "output" / "error"
PROCESSED_DIR = DATA_DIR / "output" / "processed"

DPI          = int(os.getenv("BARCODE_DPI",      "300"))
# BARCODE_DPI_SCAN — DPI used for the fast first pass over all pages.
# Pages where a code is detected are re-decoded at DPI (full resolution)
# for maximum accuracy. Content pages with no code are never rasterised
# at full DPI, making large all-content PDFs ~4x faster to process.
# Set to the same value as BARCODE_DPI to disable two-pass mode.
# BARCODE_DPI_SCAN — DPI for Pass 1 fast scan. Raised from 150 to 200 to
# reliably detect QR codes in email attachments where the QR code image
# occupies a smaller area of the page than in dedicated separator sheets.
# At 200 DPI: 2.25× less pixels than 300 DPI, 2s per A4 page vs ~5s.
# A 12-page content-only PDF still completes in ~24 s (well within 60 s limit).
BARCODE_DPI_SCAN = int(os.getenv("BARCODE_DPI_SCAN", "200"))
UPSCALE      = float(os.getenv("BARCODE_UPSCALE", "1.0"))
SCANNER      = os.getenv("BARCODE_SCANNER", "ZXING").upper()
MAX_LOG = int(os.getenv("MAX_LOG_ENTRIES", "200"))

FILE_STABLE_TIMEOUT  = int(os.getenv("FILE_STABLE_TIMEOUT",  "60"))
FILE_STABLE_INTERVAL = int(os.getenv("FILE_STABLE_INTERVAL", "2"))

# Resource limits — protect against OOM when processing very large PDFs.
# pdf-dispatch renders each page at BARCODE_DPI to detect barcodes; at 300 DPI
# an A4 page occupies ~26 MB of RAM.  Without limits a 200-page PDF can exhaust
# all available memory and crash the container (and potentially Docker itself).
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))   # max file size accepted
MAX_PAGES     = int(os.getenv("MAX_PAGES",     "100"))  # max pages processed

# Concurrency limit — number of PDFs rendered simultaneously at BARCODE_DPI.
# Each render can consume ~26 MB RAM per A4 page; limiting concurrency prevents
# resource starvation that makes Flask unresponsive under concurrent uploads.
MAX_CONCURRENT_PROCESSING = int(os.getenv("MAX_CONCURRENT_PROCESSING", "2"))

# Worker thread pool size — limits threads created by PDFHandler and scan_existing.
# Default: MAX_CONCURRENT_PROCESSING + 4 (headroom for non-PDF and edge cases).
# Increase on hardware with more cores and RAM; decrease on constrained devices.
# See README for guidance.
MAX_WORKER_THREADS = int(os.getenv(
    "MAX_WORKER_THREADS",
    str(max(8, MAX_CONCURRENT_PROCESSING + 4))
))

# Hard deadline for a single PDF processing task (seconds from start).
# If find_split_pages + splitting exceeds this limit, the file is moved to
# /error/ and the task is marked as failed.
API_TASK_TIMEOUT = int(os.getenv("API_TASK_TIMEOUT", "120"))

# Post-processing hook (optional script executed after each file is processed)
POST_PROCESS_SCRIPT  = os.getenv("POST_PROCESS_SCRIPT",  "").strip()
POST_PROCESS_TIMEOUT = int(os.getenv("POST_PROCESS_TIMEOUT", "30"))

# SSRF protection — blocks webhook/email-test requests to private/loopback
# addresses when set to "block" (#13).  Default "off": all URLs accepted.
# Enable with SSRF_PROTECTION=block; document in README.
SSRF_PROTECTION = os.getenv("SSRF_PROTECTION", "off").strip().lower() == "block"

# Persistent configuration file (counter + UI preferences)
# Stored at DATA_DIR root — protected from subfolder renames
# (named .splitter_config.json for historical reasons)
CONFIG_FILE    = DATA_DIR / ".splitter_config.json"
_COUNTER_FILE  = DATA_DIR / ".splitter_counter"   # dedicated counter file (#5)


# ---------------------------------------------------------------------------
# Persistent configuration
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "counter":          0,
    "language":         DEFAULT_LANGUAGE,
    "split_values":     [{"value": "NEWDOC", "page_handling": "keep", "case_sensitive": True}],
    "subdirs_by_trigger": False,          # sort into per-trigger subfolders
    "delete_source":    False,            # archive source file (False = do not archive = delete)
    "separator_placement": "before",      # "before" | "after" — global: where the separator sits relative to content
    # Outbound webhook
    "webhook_enabled": False,
    "webhook_url":     "",
    "webhook_secret":  "",
    "webhook_events":  "all",             # "all" | "success" | "error"
    # API key (auto-generated; overridable via API_KEY env var)
    "api_key": "",
    # filename tokens: order = list, each token = dict
    # type: trigger | string | timestamp | counter
    # Tokens of type "string" may appear multiple times (unique id required)
    "filename_tokens": [
        {"type": "trigger",   "enabled": True},
        {"type": "timestamp", "enabled": True,  "format": "%Y%m%d-%H%M%S"},
        {"type": "counter",   "enabled": True,  "digits": 6},
    ],
    # Note: "string" tokens are added dynamically via the interface
    # Separator between tokens: "_" | "-" | "." | ""
    "filename_separator": "_",
    # Log verbosity displayed in the interface: False = info (concise), True = debug (all, including IMAP retrieval)
    "log_verbose": False,
    # Persistent statistics
    "stats": {
        "processed":  0,
        "split_docs": 0,
        "errors":     0,
        "last_file":  None,
        "last_time":  None,
    },
    # Folders (paths relative to DATA_DIR)
    "dirs": {
        "input":     "input",
        "output":    "output",
        "error":     "output/error",
        "processed": "output/processed",
        "no_code":   "output/no_code",
    },
    # IMAP email configurations (list — multiple accounts/folders/filters supported)
    "email_configs": [],
}

config_lock = threading.Lock()

# In-memory configuration cache (thread-safe via config_lock).
_config_cache: dict | None = None

def get_dirs() -> dict:
    """Return the absolute paths of all output directories from the current config."""
    cfg  = load_config()
    dirs = cfg.get("dirs", CONFIG_DEFAULTS["dirs"])
    return {k: DATA_DIR / v for k, v in dirs.items()}


def update_dir_paths():
    """Update the global path variables from the current configuration."""
    global INPUT_DIR, OUTPUT_DIR, ERROR_DIR, PROCESSED_DIR
    dirs = get_dirs()
    INPUT_DIR     = dirs["input"]
    OUTPUT_DIR    = dirs["output"]
    ERROR_DIR     = dirs["error"]
    PROCESSED_DIR = dirs["processed"]


def _safe_relative_path(path_str: str) -> tuple[bool, str]:
    """
    Validate that a relative path is safe:
    - Stays within DATA_DIR
    - No dangerous characters
    - No path traversal (../)
    Returns (ok, error_message)
    """
    # Characters forbidden in folder names.
    # The single quote and backtick are included not because they are
    # invalid on any filesystem, but because folder paths are echoed into
    # inline HTML event handlers in the frontend; forbidding them here is a
    # defence-in-depth complement to escapeJsStr() on the client side.
    FORBIDDEN = r'[<>:"|?*\x00-\x1f\\\'`]'
    if re.search(FORBIDDEN, path_str):
        return False, t("dirs.error_forbidden_chars", path=path_str)
    # Normalise and check for path traversal
    try:
        resolved = (DATA_DIR / path_str).resolve()
    except Exception as e:
        return False, t("dirs.error_invalid_path", message=e)
    try:
        resolved.relative_to(DATA_DIR.resolve())   # raises ValueError if escapes
    except ValueError:
        return False, t("dirs.error_outside_root")
    # Reject empty paths
    if not path_str.strip("/"):
        return False, t("dirs.error_empty_path")
    return True, ""


# ---------------------------------------------------------------------------
# IMAP password encryption  →  dispatch/crypto.py
# ---------------------------------------------------------------------------

from dispatch.crypto import (
    _EMAIL_SECRET_KEY,
    _get_email_secret,
    _get_fernet,
    encrypt_password,
    decrypt_password,
)

def _validate_and_sanitize_config(cfg: dict) -> dict:
    """Validate and sanitise the configuration dict on load."""
    import re as _re
    CTRL   = _re.compile(r'[\x01-\x1f\x7f]')
    FORBID = _re.compile(r'[<>:"|?*]')  # backslash excluded to avoid ambiguity
    issues = []

    sep = cfg.get("filename_separator", "_")
    if sep not in ("_", "-", ".", ""):
        issues.append("filename_separator invalid -> reset to '_'")
        cfg["filename_separator"] = "_"

    ctr = cfg.get("counter", 0)
    if not isinstance(ctr, int) or ctr < 0:
        issues.append(f"counter invalid -> 0")
        cfg["counter"] = 0

    tokens = cfg.get("filename_tokens", [])
    if not isinstance(tokens, list):
        cfg["filename_tokens"] = list(CONFIG_DEFAULTS["filename_tokens"])
        issues.append("filename_tokens invalid -> reset")
    else:
        valid_types = {"trigger", "string", "timestamp", "counter"}
        san = []
        for t in tokens:
            if not isinstance(t, dict) or t.get("type") not in valid_types:
                issues.append(f"Invalid token ignored: {t}")
                continue
            if t["type"] == "counter":
                d = t.get("digits", 6)
                if not isinstance(d, int) or not (3 <= d <= 8):
                    issues.append(f"digits invalid -> 6")
                    t["digits"] = 6
            if t["type"] == "string":
                v = str(t.get("value", ""))
                c = CTRL.sub("", v)
                if c != v:
                    issues.append("Control characters in string token -> stripped")
                    t["value"] = c
                # String tokens have no UI toggle — always enabled. Enforced
                # here (not just client-side) so a config pushed directly via
                # the REST API can't disable one.
                t["enabled"] = True
            san.append(t)
        cfg["filename_tokens"] = san

    dirs = cfg.get("dirs", {})
    if isinstance(dirs, dict):
        san_dirs = {}
        data_res = str(DATA_DIR.resolve())
        for k, v in dirs.items():
            if not isinstance(v, str):
                issues.append(f"Invalid path for '{k}' -> reset")
                san_dirs[k] = CONFIG_DEFAULTS["dirs"].get(k, k)
                continue
            try:
                resolved_path = (DATA_DIR / v).resolve()
                resolved_path.relative_to(DATA_DIR.resolve())  # ValueError if outside
            except ValueError:
                issues.append(f"SECURITY: '{v}' for '{k}' escapes DATA_DIR -> reset")
                san_dirs[k] = CONFIG_DEFAULTS["dirs"].get(k, k)
                continue
            except Exception as e:
                issues.append(f"Invalid path '{k}': {e} -> reset")
                san_dirs[k] = CONFIG_DEFAULTS["dirs"].get(k, k)
                continue
            c = FORBID.sub("_", v)
            if c != v:
                issues.append(f"Forbidden characters in path '{k}' -> sanitised")
            san_dirs[k] = c
        cfg["dirs"] = san_dirs

    for sv in cfg.get("split_values", []):
        if isinstance(sv, dict) and "value" in sv:
            v = str(sv["value"])
            c = CTRL.sub("", v)
            if c != v:
                issues.append("Control characters in trigger value -> stripped")
                sv["value"] = c
            sv.setdefault("page_handling", "keep")
            if sv["page_handling"] not in ("keep", "delete"):
                issues.append(f"page_handling '{sv['page_handling']}' invalid → 'keep'")
                sv["page_handling"] = "keep"
            if not isinstance(sv.get("case_sensitive"), bool):
                sv["case_sensitive"] = True

    if cfg.get("separator_placement") not in ("before", "after"):
        if "separator_placement" in cfg:
            issues.append("separator_placement invalid → 'before'")
        cfg["separator_placement"] = "before"

    if not isinstance(cfg.get("log_verbose"), bool):
        issues.append("log_verbose invalid -> False")
        cfg["log_verbose"] = False

    # Webhook
    if not isinstance(cfg.get("webhook_enabled"), bool):
        cfg["webhook_enabled"] = False
    if not isinstance(cfg.get("webhook_url"), str):
        cfg["webhook_url"] = ""
    # Sanitise webhook_url: strip CRLF characters to prevent HTTP header injection
    # (e.g. "\r\nX-Inject: bad" would otherwise be stored and forwarded verbatim).
    cfg["webhook_url"] = cfg["webhook_url"].replace("\r", "").replace("\n", "")
    if not isinstance(cfg.get("webhook_secret"), str):
        cfg["webhook_secret"] = ""
    cfg["webhook_secret"] = cfg["webhook_secret"].replace("\r", "").replace("\n", "")
    if cfg.get("webhook_events") not in ("all", "success", "error"):
        cfg["webhook_events"] = "all"
    if not isinstance(cfg.get("api_key"), str):
        cfg["api_key"] = ""

    if cfg.get("separator_placement") not in ("before", "after"):
        if "separator_placement" in cfg:
            issues.append("separator_placement invalid -> 'before'")
        cfg["separator_placement"] = "before"

    if cfg.get("language") not in SUPPORTED_LANGUAGES:
        issues.append(f"language invalid -> {DEFAULT_LANGUAGE}")
        cfg["language"] = DEFAULT_LANGUAGE

    configs = cfg.get("email_configs", [])
    if not isinstance(configs, list):
        issues.append("email_configs invalid -> []")
        configs = []
    san_configs = []
    used_ids = set()
    for idx, ec in enumerate(configs):
        if not isinstance(ec, dict):
            issues.append(f"email_configs[{idx}] invalid, ignored")
            continue
        if not ec.get("id") or ec["id"] in used_ids:
            ec["id"] = uuid.uuid4().hex[:12]
            issues.append(f"email_configs[{idx}] id missing/duplicate -> generated")
        used_ids.add(ec["id"])
        if not ec.get("name"):
            ec["name"] = f"Config {idx+1}"
        if ec.get("action") not in ("read", "delete", "ignore"):
            issues.append(f"email_configs[{idx}].action invalid -> 'read'")
            ec["action"] = "read"
        port = ec.get("port", 993)
        if not isinstance(port, int) or not (1 <= port <= 65535):
            issues.append(f"email_configs[{idx}].port invalid -> 993")
            ec["port"] = 993
        interval = ec.get("poll_interval", 5)
        if not isinstance(interval, int) or interval < 1:
            issues.append(f"email_configs[{idx}].poll_interval invalid -> 5")
            ec["poll_interval"] = 5
        san_configs.append(ec)
    cfg["email_configs"] = san_configs

    if issues:
        log.warning(f"Config: {len(issues)} issue(s) corrected:")
        for issue in issues:
            log.warning(f"  -> {issue}")

    return cfg


def _is_glob_pattern(value: str) -> bool:
    """Return True if value contains glob special characters."""
    return any(c in value for c in ('*', '?', '[', ']'))


def _match_trigger(code: str, trigger: dict) -> bool:
    """
    Check whether a scanned code value matches a trigger.
    Supports glob patterns (* ? [abc] [0-9] etc.) and respects case_sensitive.
    """
    pattern = trigger.get("value", "")
    case_sensitive = trigger.get("case_sensitive", True)

    if not case_sensitive:
        code    = code.lower()
        pattern = pattern.lower()

    if _is_glob_pattern(pattern):
        return fnmatch.fnmatchcase(code, pattern)
    return code == pattern


def _seed_language() -> str:
    """Initial language if none has been persisted yet (first startup or upgrade
    from a pre-i18n config without a 'language' key). Optionally set via the
    APP_LANGUAGE environment variable; used only as a seed — once persisted
    (or changed via the interface), the saved value takes precedence."""
    seed = os.getenv("APP_LANGUAGE", "").strip().lower()
    return seed if seed in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def load_config() -> dict:
    """Load configuration from CONFIG_FILE (.splitter_config.json).

    Reads the persistent file if it exists, fills in CONFIG_DEFAULTS for any
    missing key, seeds the language if absent (_seed_language), then
    validates/sanitises the result (_validate_and_sanitize_config).

    Falls back to a copy of CONFIG_DEFAULTS (with the seeded language) if the
    file is absent or unreadable. NOT cached: every call re-reads the file.
    Use get_config()/update_config() which protect access via config_lock for
    thread safety (monitor + Flask requests + email retrieval)."""
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = dict(CONFIG_DEFAULTS)
            merged.update(data)
            if "language" not in data:
                merged["language"] = _seed_language()
            merged = _validate_and_sanitize_config(merged)
            return merged
    except Exception as e:
        log.warning(f"Config unreadable, using defaults: {e}")
    defaults = dict(CONFIG_DEFAULTS)
    defaults["language"] = _seed_language()
    return defaults


def save_config(cfg: dict):
    """Write the full configuration to CONFIG_FILE (indented JSON, UTF-8).

    Creates the parent directory if needed. On success, also refreshes the
    in-memory cache (_config_cache) so subsequent get_config() calls return
    the new values without a disk read.
    Never raises loudly: on write error (permissions, full disk...), logs a
    warning and continues — the configuration remains valid in memory for the
    current request but will not be persisted.

    processed_ids / processed_ids_oldest are stripped from email_configs
    before the disk write (#12): they are persisted in dedicated
    .email_proc_<id>.json files instead, keeping the main config JSON small
    regardless of how many messages have been processed.  The in-memory cache
    retains the full data so api_state / _email_check_limit can read the
    up-to-date counts without an extra file read."""
    global _config_cache
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Strip volatile per-email fields before writing to keep the JSON small.
        # The cache keeps the full copy (with processed_ids) for in-process use.
        _STRIP = ("processed_ids", "processed_ids_oldest")
        if cfg.get("email_configs"):
            cfg_disk = dict(cfg)
            cfg_disk["email_configs"] = [
                {k: v for k, v in ec.items() if k not in _STRIP}
                for ec in cfg["email_configs"]
            ]
        else:
            cfg_disk = cfg
        _tmp = CONFIG_FILE.with_suffix(".tmp")
        _tmp.write_text(json.dumps(cfg_disk, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        _tmp.replace(CONFIG_FILE)
        _config_cache = cfg   # keep cache in sync (full data, with processed_ids)
        log.debug(f"Config saved to {CONFIG_FILE}")
    except Exception as e:
        log.warning(f"Could not save config: {e}")


def get_config() -> dict:
    """Return the current configuration, using the in-memory cache when
    available.  The cache is populated on the first call and refreshed by
    every save_config() call.  Protected by config_lock for thread safety.

    Manual edits to CONFIG_FILE on disk are not visible until the next
    container restart (acceptable: no manual editing expected in production).
    """
    global _config_cache
    with config_lock:
        if _config_cache is None:
            _config_cache = load_config()
        return dict(_config_cache)   # defensive copy — callers must not mutate


def update_config(partial: dict) -> dict:
    """Merge `partial` into the persistent configuration and save it.

    Uses the in-memory cache (no disk read) when available.  Applies a
    top-level merge — to update a nested key such as "dirs" or "stats",
    supply the full updated dict.  Protected by config_lock to prevent
    concurrent writes (API requests, email retrieval, monitor)."""
    global _config_cache
    with config_lock:
        cfg = dict(_config_cache) if _config_cache is not None else load_config()
        cfg.update(partial)
        save_config(cfg)   # also updates _config_cache
        return cfg


_counter_lock  = threading.Lock()
_counter_value: int | None = None   # in-memory counter; None = not yet loaded


def _counter_persist(value: int) -> None:
    """Atomically write the counter to its dedicated file."""
    try:
        _tmp = _COUNTER_FILE.with_suffix(".tmp")
        _tmp.write_text(str(value), encoding="utf-8")
        _tmp.replace(_COUNTER_FILE)
    except Exception as e:
        log.warning(f"Could not persist counter: {e}")


def _counter_load() -> int:
    """Load counter from dedicated file, falling back to config (migration).

    The dedicated file (.splitter_counter) takes precedence.  On first run
    after the upgrade, the file does not exist and the value is migrated from
    the 'counter' field in .splitter_config.json, then written to the file.
    """
    try:
        v = int(_COUNTER_FILE.read_text(encoding="utf-8").strip())
        log.debug(f"Counter loaded from {_COUNTER_FILE}: {v}")
        return v
    except FileNotFoundError:
        v = (get_config().get("counter", 0))
        log.info(f"Counter migrated from config to {_COUNTER_FILE}: {v}")
        _counter_persist(v)
        return v
    except Exception as e:
        log.warning(f"Counter file unreadable ({e}), falling back to config")
        return get_config().get("counter", 0)


def next_counter() -> int:
    """Increment and persist the file-naming counter, return the new value.

    Uses a dedicated in-memory counter protected by _counter_lock (not
    config_lock), persisted to _COUNTER_FILE (~6 bytes) instead of
    rewriting the full config JSON on every call.  This eliminates the
    2 I/O-per-document contention on config_lock (faiblesse #5).
    """
    global _counter_value
    with _counter_lock:
        if _counter_value is None:
            _counter_value = _counter_load()
        _counter_value += 1
        _counter_persist(_counter_value)
        return _counter_value


def _load_stats() -> dict:
    """Load statistics from the persistent configuration."""
    try:
        cfg = load_config()
        return dict(cfg.get("stats", CONFIG_DEFAULTS["stats"]))
    except Exception:
        return dict(CONFIG_DEFAULTS["stats"])

def _save_stats(stats: dict):
    """Save statistics to the persistent configuration."""
    try:
        update_config({"stats": stats})
    except Exception as e:
        log.warning(f"Could not save stats: {e}")

