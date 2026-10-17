"""
dispatch/__init__.py
---------------------
Application Factory for pdf-dispatch.

create_app() is the sole public entry point for building the Flask instance.
It encapsulates app creation, request-size configuration, the 413 error
handler, authentication logging, Blueprint registration, and the one-time
API key generation that closes the anonymous-access window on first startup,
and the signed session cookie used by the web-interface sign-in.

Usage:
    from dispatch import create_app
    app = create_app()
"""

import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify

# Absolute path to splitter/ (parent of dispatch/)
_SPLITTER_ROOT = Path(__file__).parent.parent


def create_app() -> Flask:
    """Create and configure the pdf-dispatch Flask instance.

    Can be called multiple times (e.g. in unit tests) without global
    side-effects: each call returns an independent instance with its
    own routes and error handlers.  _ensure_api_key() is idempotent
    (no-op when a key already exists) so multiple calls are safe.

    Returns the fully configured Flask application.
    """
    from dispatch.config import log

    app = Flask(
        "pdf-dispatch",
        template_folder=str(_SPLITTER_ROOT / "templates"),
        static_folder=str(_SPLITTER_ROOT / "static"),
    )

    # ── Request body size cap ──────────────────────────────────────────────
    # Global ceiling across all endpoints, distinct from MAX_UPLOAD_MB.
    # /api/upload accepts several files in a single multipart POST, each
    # checked individually by the route. This cap prevents memory exhaustion
    # via any JSON-accepting endpoint regardless of the per-file limit.
    max_request_mb = int(os.getenv("MAX_REQUEST_MB", "500"))
    app.config["MAX_CONTENT_LENGTH"] = max_request_mb * 1024 * 1024

    # ── 413 handler ───────────────────────────────────────────────────────
    @app.errorhandler(413)
    def _request_too_large(_exc):
        return jsonify({
            "ok":    False,
            "error": f"Request body exceeds the {max_request_mb} MB limit "
                     f"(MAX_REQUEST_MB env var).",
        }), 413

    # ── Authentication logging ─────────────────────────────────────────────
    app_username = os.getenv("APP_USERNAME", "")
    app_password = os.getenv("APP_PASSWORD", "")
    if app_username and app_password:
        log.info("HTTP Basic authentication enabled "
                 "(APP_USERNAME/APP_PASSWORD set).")
    elif app_username or app_password:
        log.warning(
            "APP_USERNAME and APP_PASSWORD must both be set to enable "
            "Basic authentication — Basic auth is ignored; the API key "
            "(X-API-Key header or web-interface sign-in) still applies."
        )
    else:
        log.info(
            "No Basic authentication configured "
            "(APP_USERNAME/APP_PASSWORD not set). "
            "The web interface asks for the API key at sign-in; "
            "API calls must send it in the X-API-Key header."
        )

    # ── Blueprints ────────────────────────────────────────────────────────
    from dispatch.routes.auth           import bp as auth_bp
    from dispatch.routes.docs           import bp as docs_bp
    from dispatch.routes.core           import bp as core_bp
    from dispatch.routes.email_routes   import bp as email_bp
    from dispatch.routes.upload         import bp as upload_bp
    from dispatch.routes.separator      import bp as separator_bp
    from dispatch.routes.webhook_routes import bp as webhook_bp
    from dispatch.routes.detect         import bp as detect_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(email_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(separator_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(detect_bp)

    # ── First-startup API key generation ─────────────────────────────────
    # Generate and persist a random API key if none is configured yet.
    # Must run after blueprints (so auth_bp.before_app_request is registered)
    # and before the server starts serving connections.
    # Idempotent: no-op when API_KEY env var is set or a key already exists.
    from dispatch.config import _ensure_api_key, _ensure_session_secret
    _ensure_api_key()

    # ── Web-interface session cookie ──────────────────────────────────────
    # Signed with a persisted random secret (survives restarts, so users stay
    # signed in).  HttpOnly + SameSite=Lax; Secure only when explicitly asked
    # for, since most installs are served over plain HTTP on a LAN.
    app.secret_key = _ensure_session_secret()
    app.config.update(
        SESSION_COOKIE_NAME="pdf_dispatch_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(
            days=int(os.getenv("SESSION_LIFETIME_DAYS", "30"))),
    )

    return app
