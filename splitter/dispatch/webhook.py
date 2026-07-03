"""
dispatch/webhook.py
--------------------
Outbound webhook: payload construction, SSRF protection, HTTP delivery,
and Flask error responses for routes that perform SSRF checks.

Contains:
  - _build_webhook_payload() — builds the JSON event payload
  - _ssrf_safe()             — checks whether a host is safe (SSRF guard)
  - _ssrf_blocked_response() — JSON HTTP 400 response for SSRF-blocked routes
  - _deliver_webhook()       — HTTP delivery with retries (daemon thread)
  - _fire_webhook()          — entry point: filters and dispatches delivery

Internal dependencies:
  - dispatch.config  (SSRF_PROTECTION, get_config, DATA_DIR, log)
  - dispatch.state   (log_event)
  - dispatch.i18n    (t — translated log messages)
  - flask.jsonify    (_ssrf_blocked_response returns a Flask response)
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import jsonify

from dispatch.config import DATA_DIR, SSRF_PROTECTION, get_config, log
from dispatch.i18n import t
from dispatch.state import log_event


def _build_webhook_payload(
    status: str,
    source_file: str,
    triggers: list | None = None,
    outputs: list | None  = None,
    docs_count: int = 0,
    error_msg: str  = "",
    config_override: dict | None = None,
) -> dict:
    """Build the JSON payload sent to the webhook endpoint.

    Schema (fat event — receiver needs no follow-up call):
      event        : "file.processed"
      timestamp    : ISO 8601 (second precision)
      source_file  : original filename (basename)
      status       : "success" | "error"
      triggers     : list of detected trigger code values
      documents    : list of {filename, path} for each output file
                     (path is relative to DATA_DIR)
      docs_count   : number of output documents produced
      error        : error description (empty string on success)
    """
    docs = []
    for p in (outputs or []):
        try:
            rel = str(Path(p).relative_to(DATA_DIR))
        except ValueError:
            rel = p
        docs.append({"filename": Path(p).name, "path": rel})
    payload = {
        "event":       "file.processed",
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        "source_file": source_file,
        "status":      status,
        "triggers":    triggers or [],
        "documents":   docs,
        "docs_count":  docs_count,
        "error":       error_msg,
    }
    if config_override:
        payload["config_override"] = config_override
    return payload


def _ssrf_safe(host: str) -> bool:
    """Return True if *host* is safe to connect to.

    Always True when SSRF_PROTECTION is "off" (default).  When "block", resolves
    *host* and rejects private/loopback/link-local addresses.  Fail-open: if
    resolution fails, the request is allowed (it will fail at connect time).
    """
    if not SSRF_PROTECTION:
        return True
    import ipaddress
    import socket as _sock
    _PRIVATE = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("::1/128"),
    ]
    try:
        ip = ipaddress.ip_address(_sock.gethostbyname(host))
        return not any(ip in net for net in _PRIVATE)
    except Exception:
        return True   # unknown host → allow; will fail at connect time


def _ssrf_blocked_response():
    """Standard JSON error response for SSRF-blocked requests.
    Returns a Flask-compatible (Response, 400) tuple.
    """
    return jsonify({
        "ok": False,
        "error": (
            "SSRF protection active (SSRF_PROTECTION=block): "
            "host resolves to a private/loopback address. "
            "Set SSRF_PROTECTION=off to allow connections to internal hosts."
        )
    }), 400


def _deliver_webhook(
    url: str, payload: dict, secret: str, source_file: str, attempts: int = 3
) -> None:
    """Deliver a webhook with up to `attempts` retries and exponential backoff.

    Called in a daemon thread — does not block document processing.
    Uses only the standard library (urllib.request) to avoid extra dependencies.
    """
    import urllib.request as _req
    import urllib.error   as _err
    import hmac           as _hmac

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   f"pdf-dispatch/webhook",
    }
    if secret:
        sig = _hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
        headers["X-Signature"] = f"sha256={sig}"

    from urllib.parse import urlparse as _urlparse
    _host = _urlparse(url).hostname or ""
    if not _ssrf_safe(_host):
        log_event("warning",
                  f"Webhook SSRF blocked (SSRF_PROTECTION=block): {url}",
                  source_file)
        return   # abort without retrying

    delays = [0] + [2 ** i for i in range(attempts - 1)]   # 0 s, 2 s, 4 s, …
    for attempt, delay in enumerate(delays[:attempts], 1):
        if delay:
            time.sleep(delay)
        try:
            request = _req.Request(url, data=body, headers=headers, method="POST")
            with _req.urlopen(request, timeout=10) as resp:
                code = resp.status
            if 200 <= code < 300:
                log_event("info",
                          t("webhook.log_delivered", code=code),
                          source_file, verbose=True)
                return
            log_event("warning",
                      t("webhook.log_attempt", attempt=attempt, msg=f"HTTP {code}"),
                      source_file)
        except _err.URLError as exc:
            reason = getattr(exc, "reason", str(exc))
            log_event("warning",
                      t("webhook.log_attempt", attempt=attempt, msg=reason),
                      source_file)
        except Exception as exc:
            log_event("warning",
                      t("webhook.log_attempt", attempt=attempt, msg=exc),
                      source_file)

    log_event("warning", t("webhook.log_failed"), source_file)


def _fire_webhook(
    source_file: str,
    status: str,
    triggers: list | None = None,
    outputs:  list | None = None,
    docs_count: int = 0,
    error_msg: str  = "",
    config_override: dict | None = None,
) -> None:
    """Fire the configured outbound webhook asynchronously (daemon thread).

    Reads the current configuration at call time (not at module load) to
    always use the latest settings. Does nothing if the webhook is disabled
    or no URL is configured.
    """
    cfg = get_config()
    if not cfg.get("webhook_enabled", False):
        return
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return

    events_filter = cfg.get("webhook_events", "all")
    if events_filter == "success" and status != "success":
        return
    if events_filter == "error" and status != "error":
        return

    payload = _build_webhook_payload(
        status=status, source_file=source_file, triggers=triggers,
        outputs=outputs, docs_count=docs_count, error_msg=error_msg,
        config_override=config_override,
    )
    secret = cfg.get("webhook_secret", "")

    threading.Thread(
        target=_deliver_webhook,
        args=(url, payload, secret, source_file),
        daemon=True,
    ).start()
