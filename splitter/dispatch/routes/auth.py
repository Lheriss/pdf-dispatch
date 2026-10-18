"""
dispatch/routes/auth.py
Global authentication hook, first-run setup, sign-in and sign-out.

Registered via @bp.before_app_request so it applies to all blueprints.

Two kinds of credentials (see dispatch/credentials.py):
  • People use the administrator account (username + password) on the
    /login page and get a signed session cookie.
  • Scripts and integrations use the API key in the X-API-Key header, or
    the administrator account through an HTTP Basic Authorization header.

Checks, in order:
  1. X-API-Key header — a wrong key is always rejected (no fall-through).
  2. Session cookie — carries a keyed fingerprint of the current
     credentials, so changing the password signs every browser out.
     Regenerating the API key does not.
  3. HTTP Basic Authorization header — checked against the administrator
     account; never solicited (no WWW-Authenticate challenge), so browsers
     do not pop up their native dialog.

Unauthenticated page loads redirect to /login (or /setup while no account
exists); everything else gets a JSON 401.

Cross-site request forgery: a request authenticated by the session cookie
and using a state-changing method must carry the X-Requested-With header,
which app.js adds to every call and which another site cannot set without
a CORS preflight that this server never grants.
"""
import hashlib
import hmac
import os
import secrets
from urllib.parse import urlsplit

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from dispatch import credentials as creds
from dispatch.config import DEFAULT_LANGUAGE, get_config
from dispatch.i18n import TRANSLATIONS

bp = Blueprint("auth", __name__)

# Paths reachable without any credential.  The API description and its
# Swagger UI contain no secret and are declared public in openapi.yaml.
_PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout", "/setup",
                           "/api/openapi.json", "/api/openapi.yaml", "/api/docs"})

# Methods that cannot change server state (exempt from the CSRF header check).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Session key holding the credentials fingerprint.
_SESSION_KEY = "cfp"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _current_api_key() -> str:
    """The effective API key: API_KEY env var, else the stored key."""
    return os.getenv("API_KEY", "").strip() or get_config().get("api_key", "")


def _credentials_fingerprint() -> str:
    secret = get_config().get("session_secret", "").encode()
    return hmac.new(secret, creds.credentials_version().encode(),
                    hashlib.sha256).hexdigest()


def session_valid() -> bool:
    """True when the request carries a session for the current credentials."""
    if not creds.credentials_source():
        return False
    fp = session.get(_SESSION_KEY, "")
    return bool(fp) and secrets.compare_digest(fp, _credentials_fingerprint())


def _start_session() -> None:
    session.clear()
    session.permanent = True
    session[_SESSION_KEY] = _credentials_fingerprint()


def _safe_next(target: str | None) -> str:
    """Only allow local, absolute-path redirect targets (no open redirect)."""
    if not target:
        return "/"
    # Browsers treat "\\" like "/", so "/\\evil" would become "//evil".
    if "\\" in target or any(ord(c) < 0x20 for c in target):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _wants_html() -> bool:
    """True for a browser navigating to a page (not an API/XHR call)."""
    if request.path.startswith("/api/") or request.method != "GET":
        return False
    return request.accept_mimetypes.accept_html


def _translations() -> tuple[str, dict]:
    lang = get_config().get("language", DEFAULT_LANGUAGE)
    return lang, TRANSLATIONS.get(lang, TRANSLATIONS.get(DEFAULT_LANGUAGE, {}))


# ── Global hook ──────────────────────────────────────────────────────────────

@bp.before_app_request
def _require_auth():
    """Enforce authentication on every request (see module docstring)."""
    if request.path in _PUBLIC_PATHS or request.endpoint == "static":
        return None

    api_key = _current_api_key()

    # 1. X-API-Key header
    header_key = request.headers.get("X-API-Key", "").strip()
    if header_key:
        if api_key and secrets.compare_digest(header_key, api_key):
            return None
        return jsonify({"ok": False, "error": "Invalid API key."}), 401

    # 2. Session cookie (web interface)
    if session_valid():
        if request.method not in _SAFE_METHODS and not request.headers.get("X-Requested-With"):
            return jsonify({"ok": False,
                            "error": "Missing X-Requested-With header."}), 403
        return None

    # 3. HTTP Basic Authorization header (administrator account)
    auth = request.authorization
    if auth is not None and auth.type == "basic":
        if creds.verify_credentials(auth.username or "", auth.password or ""):
            return None
        return jsonify({"ok": False, "error": "Invalid credentials."}), 401

    # 4. Nothing valid presented.  Open mode only when no credential of any
    #    kind exists (documented "isolated network" mode — unreachable in
    #    practice since an API key is generated at startup).
    if not api_key and not creds.credentials_source():
        return None
    if _wants_html():
        if not creds.credentials_source():
            return redirect(url_for("auth.setup"))
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    return jsonify({"ok": False, "error": "Authentication required."}), 401


# ── First-run setup ──────────────────────────────────────────────────────────

def _render_setup(error_key: str | None = None, username: str = "admin", status: int = 200):
    lang, tr = _translations()
    return render_template("setup.html", lang=lang, tr=tr, error_key=error_key,
                           username=username,
                           min_len=creds.MIN_PASSWORD_LENGTH), status


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the administrator account (only while none exists)."""
    if creds.credentials_source():
        return redirect(url_for("auth.login"))
    if request.method == "GET":
        return _render_setup()

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if password != request.form.get("password_confirm", ""):
        return _render_setup("auth.error_password_mismatch", username, 400)
    err = creds.complete_setup(request.form.get("setup_code", ""), username, password)
    if err == "auth.error_setup_done":
        return redirect(url_for("auth.login"))
    if err:
        return _render_setup(err, username, 401 if err == "auth.error_setup_code" else 400)
    _start_session()
    return redirect("/")


# ── Sign-in / sign-out ───────────────────────────────────────────────────────

def _render_login(error: bool = False, status: int = 200, username: str = ""):
    lang, tr = _translations()
    return render_template(
        "login.html", lang=lang, tr=tr, error=error, username=username,
        next=_safe_next(request.values.get("next")),
    ), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Web-interface sign-in with the administrator account."""
    if not creds.credentials_source():
        return redirect(url_for("auth.setup"))
    if request.method == "GET":
        if session_valid():
            return redirect(_safe_next(request.args.get("next")))
        return _render_login()

    username = request.form.get("username", "").strip()
    if not creds.verify_credentials(username, request.form.get("password", "")):
        return _render_login(error=True, status=401, username=username)
    _start_session()
    return redirect(_safe_next(request.form.get("next")))


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    """Clear the web-interface session and return to the sign-in page."""
    session.clear()
    return redirect(url_for("auth.login"))


# ── Password change (signed-in administrator) ────────────────────────────────

@bp.route("/api/settings/password", methods=["POST"])
def api_change_password():
    """POST /api/settings/password — change the administrator password.

    Body: {"current_password": "...", "new_password": "..."}.
    Refused when the credentials come from APP_USERNAME/APP_PASSWORD.
    Other browsers are signed out; the calling session stays signed in.
    """
    if creds.credentials_source() != "stored":
        return jsonify({"ok": False, "error_key": "auth.error_env_credentials",
                        "error": "Credentials are set via APP_USERNAME/APP_PASSWORD."}), 400
    data = request.get_json(silent=True) or {}
    current = data.get("current_password", "")
    new = data.get("new_password", "")
    if not creds.verify_credentials(creds.admin_username(), current):
        return jsonify({"ok": False, "error_key": "auth.error_current_password",
                        "error": "Current password is incorrect."}), 400
    err = creds.validate_new_credentials(creds.admin_username(), new)
    if err:
        return jsonify({"ok": False, "error_key": err,
                        "error": "New password is too short."}), 400
    creds.set_stored_credentials(creds.admin_username(), new)
    if session.get(_SESSION_KEY):
        _start_session()   # keep the caller signed in under the new fingerprint
    return jsonify({"ok": True})
