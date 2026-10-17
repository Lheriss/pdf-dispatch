"""
dispatch/routes/auth.py
Global authentication hook and web-interface login.

Registered via @bp.before_app_request so it applies to all blueprints.

Accepted credentials, checked in this order:
  1. X-API-Key header — for scripts and integrations.  A wrong key is
     always rejected (no fall-through to the other methods).
  2. Session cookie — set by POST /login after the operator enters the API
     key in the web interface.  The session carries a fingerprint of the key,
     so regenerating the key signs every browser out.
  3. HTTP Basic auth — when both APP_USERNAME and APP_PASSWORD are set.

Unauthenticated requests are rejected whenever an API key exists (which is
always the case after first start): browsers asking for a page are
redirected to /login, everything else gets HTTP 401.  Only when no key and
no Basic credentials exist at all is access left open.

Cross-site request forgery: a request authenticated by the session cookie
and using a state-changing method must carry the X-Requested-With header,
which app.js adds to every call and which another site cannot set without
a CORS preflight that this server never grants.  Requests authenticated by
X-API-Key or Basic auth are not cookie-based and need no such header.
"""
import hashlib
import hmac
import os
import secrets
from urllib.parse import urlsplit

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, session, url_for)

from dispatch.config import DEFAULT_LANGUAGE, get_config
from dispatch.i18n import TRANSLATIONS

bp = Blueprint("auth", __name__)

# Paths reachable without any credential.  The API description and its
# Swagger UI contain no secret and are declared public in openapi.yaml.
_PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout",
                           "/api/openapi.json", "/api/openapi.yaml", "/api/docs"})

# Methods that cannot change server state (exempt from the CSRF header check).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Session key holding the API key fingerprint.
_SESSION_KEY = "akf"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _current_api_key() -> str:
    """The effective API key: API_KEY env var, else the stored key."""
    return os.getenv("API_KEY", "").strip() or get_config().get("api_key", "")


def _key_fingerprint(key: str) -> str:
    """Non-reversible fingerprint of the API key stored in the session.

    The key itself never goes into the cookie.  Keyed with the session
    secret so the fingerprint is useless outside this instance.
    """
    secret = get_config().get("session_secret", "").encode()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()


def _session_valid(api_key: str) -> bool:
    fp = session.get(_SESSION_KEY, "")
    return bool(api_key and fp) and secrets.compare_digest(fp, _key_fingerprint(api_key))


def _basic_credentials() -> tuple[str, str]:
    return os.getenv("APP_USERNAME", ""), os.getenv("APP_PASSWORD", "")


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


def _unauthorized(basic_enabled: bool):
    if basic_enabled:
        # Let the browser show its native Basic auth prompt.
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="pdf-dispatch"'})
    if _wants_html():
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    return jsonify({"ok": False, "error": "Authentication required."}), 401


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
    if _session_valid(api_key):
        if request.method not in _SAFE_METHODS and not request.headers.get("X-Requested-With"):
            return jsonify({"ok": False,
                            "error": "Missing X-Requested-With header."}), 403
        return None

    # 3. HTTP Basic auth
    username, password = _basic_credentials()
    basic_enabled = bool(username and password)
    if basic_enabled:
        auth = request.authorization
        if (auth is not None
                and secrets.compare_digest(auth.username or "", username)
                and secrets.compare_digest(auth.password or "", password)):
            return None
        return _unauthorized(basic_enabled=True)

    # 4. Nothing presented: reject when a key exists, else open mode
    #    (documented "isolated network, no authentication" mode).
    if api_key:
        return _unauthorized(basic_enabled=False)
    return None


# ── Login / logout ───────────────────────────────────────────────────────────

def _render_login(error: bool = False, status: int = 200):
    lang = get_config().get("language", DEFAULT_LANGUAGE)
    tr = TRANSLATIONS.get(lang, TRANSLATIONS.get(DEFAULT_LANGUAGE, {}))
    return render_template(
        "login.html", lang=lang, tr=tr, error=error,
        next=_safe_next(request.values.get("next")),
    ), status


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Web-interface sign-in: exchanges the API key for a session cookie."""
    api_key = _current_api_key()
    if not api_key:
        return redirect("/")          # open mode: nothing to sign in to
    if request.method == "GET":
        if _session_valid(api_key):
            return redirect(_safe_next(request.args.get("next")))
        return _render_login()

    supplied = request.form.get("api_key", "").strip()
    if not supplied or not secrets.compare_digest(supplied, api_key):
        return _render_login(error=True, status=401)
    session.clear()
    session.permanent = True
    session[_SESSION_KEY] = _key_fingerprint(api_key)
    return redirect(_safe_next(request.form.get("next")))


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    """Clear the web-interface session and return to the sign-in page."""
    session.clear()
    return redirect(url_for("auth.login"))
