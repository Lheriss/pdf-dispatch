"""
dispatch/__init__.py
---------------------
Application Factory for pdf-dispatch.

create_app() is the sole public entry point for building the Flask instance.
It encapsulates app creation, request-size configuration, the 413 error
handler, authentication logging, and Blueprint registration.

Usage:
    from dispatch import create_app
    app = create_app()
"""

import os
from pathlib import Path

from flask import Flask, jsonify

# Absolute path to splitter/ (parent of dispatch/)
_SPLITTER_ROOT = Path(__file__).parent.parent


def create_app() -> Flask:
    """Create and configure the pdf-dispatch Flask instance.

    Can be called multiple times (e.g. in unit tests) without global
    side-effects: each call returns an independent instance with its
    own routes and error handlers.

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
            "authentication — no authentication is applied."
        )
    else:
        log.info(
            "No authentication configured "
            "(APP_USERNAME/APP_PASSWORD not set). "
            "See the README to secure access if needed."
        )

    # ── Blueprints ────────────────────────────────────────────────────────
    from dispatch.routes.auth           import bp as auth_bp
    from dispatch.routes.docs           import bp as docs_bp
    from dispatch.routes.core           import bp as core_bp
    from dispatch.routes.email_routes   import bp as email_bp
    from dispatch.routes.upload         import bp as upload_bp
    from dispatch.routes.separator      import bp as separator_bp
    from dispatch.routes.webhook_routes import bp as webhook_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(email_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(separator_bp)
    app.register_blueprint(webhook_bp)

    return app
