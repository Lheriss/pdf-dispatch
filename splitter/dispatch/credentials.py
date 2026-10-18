"""
dispatch/credentials.py
-----------------------
Web-interface administrator credentials and first-run setup.

Two sources, in order of precedence:
  1. APP_USERNAME / APP_PASSWORD environment variables (both set) —
     predefined credentials, no setup step; cannot be changed from the UI.
  2. Stored credentials — created on the /setup page on first run and kept
     in the persistent config as a username and a scrypt password hash
     (werkzeug.security); the password itself is never stored.

First-run protection (Jenkins-style): while no credentials exist, a one-time
setup code is generated at startup and written to the log.  The /setup page
requires it, so nobody else on the network can claim the instance before the
operator does.  The code lives in memory only (single-process server) and is
regenerated on every start until setup is completed.

The API key (X-API-Key) is a separate, machine-only credential — see
config._ensure_api_key().
"""

import hashlib
import os
import secrets
import threading

from werkzeug.security import check_password_hash, generate_password_hash

from dispatch.config import get_config, log, update_config

MIN_PASSWORD_LENGTH = 8

_setup_lock  = threading.Lock()
_setup_token: str | None = None


# ── Sources ──────────────────────────────────────────────────────────────────

def _env_credentials() -> tuple[str, str] | None:
    user = os.getenv("APP_USERNAME", "")
    pwd  = os.getenv("APP_PASSWORD", "")
    return (user, pwd) if user and pwd else None


def credentials_source() -> str | None:
    """"env", "stored", or None when setup has not been completed."""
    if _env_credentials():
        return "env"
    cfg = get_config()
    if cfg.get("admin_username") and cfg.get("admin_password_hash"):
        return "stored"
    return None


def admin_username() -> str:
    env = _env_credentials()
    return env[0] if env else get_config().get("admin_username", "")


# ── Verification ─────────────────────────────────────────────────────────────

def verify_credentials(username: str, password: str) -> bool:
    """Constant-time check of a username/password pair."""
    username = username or ""
    password = password or ""
    env = _env_credentials()
    if env:
        ok_user = secrets.compare_digest(username, env[0])
        ok_pwd  = secrets.compare_digest(password, env[1])
        return ok_user and ok_pwd
    cfg = get_config()
    stored_user = cfg.get("admin_username", "")
    stored_hash = cfg.get("admin_password_hash", "")
    if not (stored_user and stored_hash):
        return False
    ok_user = secrets.compare_digest(username, stored_user)
    ok_pwd  = check_password_hash(stored_hash, password)  # always computed
    return ok_user and ok_pwd


def credentials_version() -> str:
    """Opaque value that changes whenever the credentials change.

    Sessions carry a keyed fingerprint of it (routes/auth.py), so changing
    the password — or the APP_* variables — signs every browser out.
    The plain password never leaves this module.
    """
    env = _env_credentials()
    if env:
        material = env[0] + "\0" + hashlib.sha256(env[1].encode()).hexdigest()
    else:
        cfg = get_config()
        material = cfg.get("admin_username", "") + "\0" + cfg.get("admin_password_hash", "")
    return material


# ── Changes ──────────────────────────────────────────────────────────────────

def validate_new_credentials(username: str, password: str) -> str | None:
    """Return an i18n key describing the problem, or None if acceptable."""
    if not (username or "").strip():
        return "auth.error_username_required"
    if any(ord(c) < 0x20 for c in username):
        return "auth.error_username_required"
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return "auth.error_password_short"
    return None


def set_stored_credentials(username: str, password: str) -> None:
    update_config({
        "admin_username":      username.strip(),
        "admin_password_hash": generate_password_hash(password),
    })


# ── First-run setup code ─────────────────────────────────────────────────────

def ensure_setup_token() -> None:
    """At startup: if setup is pending, generate the one-time code and log it."""
    global _setup_token
    if credentials_source():
        return
    with _setup_lock:
        _setup_token = secrets.token_urlsafe(18)
    log.warning(
        "First-run setup: open the web interface and enter this setup code "
        "to create the administrator account: %s  "
        "(valid until setup is completed; a new code is generated on each "
        "start while setup is pending)",
        _setup_token,
    )


def check_setup_token(supplied: str) -> bool:
    token = _setup_token
    return bool(token and supplied) and secrets.compare_digest(supplied.strip(), token)


def complete_setup(token: str, username: str, password: str) -> str | None:
    """Create the stored administrator account, atomically.

    Returns None on success, or an i18n key describing why setup was
    refused.  Check-and-set runs under a lock so two simultaneous
    submissions cannot both succeed.
    """
    global _setup_token
    with _setup_lock:
        if credentials_source():
            return "auth.error_setup_done"
        if not check_setup_token(token):
            return "auth.error_setup_code"
        err = validate_new_credentials(username, password)
        if err:
            return err
        set_stored_credentials(username, password)
        _setup_token = None
    log.info("Administrator account created (user %r).", username.strip())
    return None
