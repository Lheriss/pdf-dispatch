"""
dispatch/routes/auth.py
Global authentication hook: X-API-Key header and HTTP Basic auth.
Registered via @bp.before_app_request so it applies to all blueprints.
"""
import os
import secrets

from flask import Blueprint, Response, request

from dispatch.config import get_config

bp = Blueprint("auth", __name__)

@bp.before_app_request
def _require_auth():
    """Hook before_request: enforces authentication on every request.

    Authentication methods (checked in order):
      1. X-API-Key header — matches the key stored in config or the API_KEY
         environment variable. If the header is present but the key is wrong,
         the request is always rejected (no fall-through to Basic auth).
      2. HTTP Basic auth — active only when both APP_USERNAME and APP_PASSWORD
         environment variables are set (AUTH_ENABLED).

    If neither method validates the request:
      • When an API key exists in config (the default after first start),
        anonymous requests are rejected with HTTP 401.  Presenting the key
        is mandatory; the key only being *optional* was the original bypass.
      • When truly no auth mechanism is configured (no stored key, no Basic
        env vars) the request is allowed.  This is the documented
        “isolated-network, no authentication” mode.

    /healthz is always allowed without authentication (Docker HEALTHCHECK).
    """
    if request.path == "/healthz":
        return None

    # --- X-API-Key -----------------------------------------------------------
    header_key = request.headers.get("X-API-Key", "").strip()
    if header_key:
        env_key    = os.getenv("API_KEY", "").strip()
        stored_key = env_key or get_config().get("api_key", "")
        if stored_key and secrets.compare_digest(header_key, stored_key):
            return None   # valid API key
        # Wrong key supplied — reject immediately, never fall through
        return Response("Invalid API key.", 401,
                        {"WWW-Authenticate": 'Basic realm="pdf-dispatch"'})

    # --- HTTP Basic auth -----------------------------------------------------
    _username = os.getenv("APP_USERNAME", "")
    _password = os.getenv("APP_PASSWORD", "")
    if not (_username and _password):
        # Basic auth is not configured.  If an API key exists the request
        # MUST have provided it and been accepted above.  Reaching here
        # means no valid key was presented — reject to close the bypass.
        env_key    = os.getenv("API_KEY", "").strip()
        stored_key = env_key or get_config().get("api_key", "")
        if stored_key:
            return Response(
                "API key required.",
                401,
                {"WWW-Authenticate": 'Basic realm="pdf-dispatch"'},
            )
        # No API key configured at all: allow (explicitly unauthenticated
        # mode, documented for isolated single-user networks).
        return None

    auth  = request.authorization
    valid = (
        auth is not None
        and secrets.compare_digest(auth.username or "", _username)
        and secrets.compare_digest(auth.password or "", _password)
    )
    if not valid:
        return Response(
            "Authentification requise.", 401,
            {"WWW-Authenticate": 'Basic realm="pdf-dispatch"'},
        )
    return None
