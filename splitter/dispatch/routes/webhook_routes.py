"""
dispatch/routes/webhook_routes.py
Blueprint: outbound webhook test.
"""
import json

from flask import Blueprint, jsonify, request

from dispatch.config import get_config, SSRF_PROTECTION
from dispatch.state import log_event
from dispatch.webhook import (
    _build_webhook_payload, _ssrf_blocked_response, _ssrf_safe,
)

bp = Blueprint("webhook_routes", __name__)

@bp.route("/api/webhook/test", methods=["POST"])
def api_webhook_test():
    """POST /api/webhook/test — Send a test payload to the configured webhook URL.

    Delivers synchronously (not in a background thread) so the result can be
    returned to the caller immediately. Returns {ok, code} on success or
    {ok: false, error} on failure.
    """
    import urllib.request as _req
    import urllib.error   as _err
    import hmac           as _hmac

    cfg = get_config()
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No webhook URL configured"}), 400

    from urllib.parse import urlparse as _urlparse
    _wh_host = _urlparse(url).hostname or ""
    if not _ssrf_safe(_wh_host):
        log_event("warning", f"Webhook test SSRF blocked (SSRF_PROTECTION=block): {url}")
        return _ssrf_blocked_response()

    payload = _build_webhook_payload(
        status="success", source_file="test.pdf",
        triggers=["TEST"], outputs=[], docs_count=0,
    )
    payload["event"] = "test"

    secret = cfg.get("webhook_secret", "")
    body   = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "pdf-dispatch/webhook-test",
    }
    if secret:
        sig = _hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
        headers["X-Signature"] = f"sha256={sig}"

    try:
        request = _req.Request(url, data=body, headers=headers, method="POST")
        with _req.urlopen(request, timeout=10) as resp:
            code = resp.status
        log_event("info", f"Webhook test: HTTP {code} → {url}")
        return jsonify({"ok": 200 <= code < 300, "code": code})
    except _err.HTTPError as exc:
        return jsonify({"ok": False, "error": f"HTTP {exc.code}", "code": exc.code})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})



