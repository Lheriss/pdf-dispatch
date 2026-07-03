"""
dispatch/routes/docs.py
Blueprints: OpenAPI documentation, /healthz, /api/runtime.
"""
import io
import json
import os
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify

from dispatch.config import (
    API_TASK_TIMEOUT, APP_VERSION, BARCODE_DPI_SCAN, DATA_DIR, DPI,
    FILE_STABLE_INTERVAL, FILE_STABLE_TIMEOUT, MAX_CONCURRENT_PROCESSING,
    MAX_LOG, MAX_PAGES, MAX_UPLOAD_MB, MAX_WORKER_THREADS,
    SCANNER, SSRF_PROTECTION, UPSCALE,
)

bp = Blueprint("docs", __name__)

@bp.route("/api/openapi.json")
def api_openapi_json():
    """GET /api/openapi.json — Serve the OpenAPI spec as a JSON object.

    Reads splitter/openapi.json (pre-built from openapi.yaml at release time)
    using Python's built-in json module — no external dependencies required.
    The `info.version` field is overridden with APP_VERSION when set.
    Not protected by authentication (allows tool discovery without credentials).
    """
    import json as _json
    spec_path = Path(__file__).parent.parent.parent / "openapi.json"
    try:
        with open(spec_path, encoding="utf-8") as f:
            spec = _json.load(f)
    except FileNotFoundError:
        return jsonify({"ok": False,
                        "error": "openapi.json not found — run make gen-openapi or rebuild the image"}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    app_ver = os.getenv("APP_VERSION", "").strip()
    if app_ver:
        spec["info"]["version"] = app_ver
    return jsonify(spec)




@bp.route("/api/openapi.yaml")
def api_openapi_yaml():
    """GET /api/openapi.yaml — Serve the raw OpenAPI YAML source file."""
    spec_path = Path(__file__).parent.parent.parent / "openapi.yaml"
    return Response(
        spec_path.read_text(encoding="utf-8"),
        content_type="application/yaml; charset=utf-8",
    )




@bp.route("/api/docs")
def api_docs():
    """GET /api/docs — Swagger UI for interactive API exploration.

    Loads Swagger UI from cdnjs.cloudflare.com (CDN). An internet connection
    is required. For offline use, fetch /api/openapi.json and import it into
    https://editor.swagger.io or any local OpenAPI tool.
    """
    return (
        """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>pdf-dispatch — API docs</title>
  <link rel="stylesheet"
        href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css">
  <style>body{margin:0} .topbar{display:none}</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js">
  </script>
  <script>
    SwaggerUIBundle({
      url: "/api/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [SwaggerUIBundle.presets.apis],
      plugins: [SwaggerUIBundle.plugins.DownloadUrl],
    });
  </script>
</body>
</html>""",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )




@bp.route("/healthz")
def healthz():
    """Health check (used by HEALTHCHECK), not protected by authentication."""
    return jsonify({"ok": True})




@bp.route("/api/runtime")
def api_runtime():
    """GET /api/runtime — Expose all effective runtime configuration values.

    Returns the values actually used by the application after applying
    environment variables and defaults. Useful for diagnostic purposes
    and for populating session headers in the integration test suite.
    Protected by standard authentication (X-API-Key / HTTP Basic).
    """
    return jsonify({
        "app_version":              APP_VERSION,
        "barcode_dpi":              DPI,
        "barcode_dpi_scan":         BARCODE_DPI_SCAN,
        "barcode_scanner":          SCANNER,
        "barcode_upscale":          UPSCALE,
        "max_pages":                MAX_PAGES,
        "max_upload_mb":            MAX_UPLOAD_MB,
        "max_concurrent_processing": MAX_CONCURRENT_PROCESSING,
        "max_worker_threads":       MAX_WORKER_THREADS,
        "file_stable_timeout":      FILE_STABLE_TIMEOUT,
        "file_stable_interval":     FILE_STABLE_INTERVAL,
        "api_task_timeout":         API_TASK_TIMEOUT,
        "ssrf_protection":          "block" if SSRF_PROTECTION else "off",
        "max_log_entries":          MAX_LOG,
        "data_dir":                 str(DATA_DIR),
    })



