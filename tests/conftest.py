"""
conftest.py — fixtures pytest partagées entre test_api.py et test_webhook.py.

Initialise le module app avec un DATA_DIR temporaire avant tout import,
puis expose :
  client         — Flask test client (scope session)
  minimal_pdf    — bytes d'un PDF minimaliste valide (scope session)
  webhook_server — récepteur HTTP local en thread + utilitaire d'attente
"""

import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import pytest

# ── Setup : doit se produire AVANT l'import de app ───────────────────────────
_TMPDIR = tempfile.mkdtemp(prefix="pdftest_")
for sub in ("input", "output/error", "output/processed", "output/no_code"):
    (pathlib.Path(_TMPDIR) / sub).mkdir(parents=True, exist_ok=True)

os.environ["DATA_DIR"]     = _TMPDIR
os.environ["EMAIL_SECRET"] = "conftest_test_secret_key_" + "x" * 40

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "splitter"))

import app as _app   # noqa: E402  (import après setup env)


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def client():
    """Flask test client sans serveur réel.

    Auth désactivée (APP_USERNAME / APP_PASSWORD non définis).
    Toutes les routes API sont accessibles sans credentials.
    """
    _app.app.config["TESTING"] = True
    with _app.app.test_client() as c:
        yield c


# ── PDF minimaliste ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def minimal_pdf() -> bytes:
    """PDF d'une page valide généré par reportlab."""
    from reportlab.pdfgen import canvas as rl_canvas
    buf = BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, "pdf-dispatch test — no barcode")
    c.save()
    buf.seek(0)
    return buf.read()


# ── Récepteur webhook local ───────────────────────────────────────────────────

class _WebhookCapture(BaseHTTPRequestHandler):
    """Mini-serveur HTTP qui capture les POST JSON et répond 200."""

    received: list = []   # liste des appels capturés, partagée entre tests

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        self.received.append({
            "body":    json.loads(raw) if raw else {},
            "headers": dict(self.headers),
            "raw":     raw,
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass   # silence les logs HTTP dans la sortie pytest


class WebhookServer:
    """Encapsule le serveur HTTP et expose des helpers pour les tests."""

    def __init__(self, server: HTTPServer, url: str):
        self._server = server
        self.url     = url

    # ── accès aux captures ────────────────────────────────────────────────────

    @property
    def calls(self) -> list:
        return _WebhookCapture.received

    def clear(self):
        _WebhookCapture.received.clear()

    # ── attente asynchrone ────────────────────────────────────────────────────

    def wait(self, count: int = 1, timeout: float = 3.0) -> bool:
        """Attend que *count* webhooks aient été reçus ou que *timeout* s'écoule.
        Retourne True si le compte est atteint, False en cas de timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls) >= count:
                return True
            time.sleep(0.05)
        return False

    # ── vérification signature HMAC ───────────────────────────────────────────

    def verify_hmac(self, call: dict, secret: str) -> bool:
        """Vérifie la signature X-Signature du call capturé."""
        sig_hdr  = call["headers"].get("X-Signature", "")
        expected = "sha256=" + hmac.new(
            secret.encode(), call["raw"], hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig_hdr, expected)

    def shutdown(self):
        self._server.shutdown()


@pytest.fixture
def webhook_server(client):
    """Démarre un récepteur HTTP sur un port libre, configure pdf-dispatch pour
    l'utiliser, puis l'arrête en fin de test.

    Usage:
        def test_foo(client, webhook_server):
            webhook_server.clear()
            client.post("/api/webhook/test")
            assert webhook_server.wait(1)
            payload = webhook_server.calls[0]["body"]
    """
    _WebhookCapture.received.clear()
    server = HTTPServer(("127.0.0.1", 0), _WebhookCapture)
    port   = server.server_address[1]
    url    = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Configurer pdf-dispatch pour pointer vers ce récepteur
    client.post("/api/config", json={
        "webhook_enabled": True,
        "webhook_url":     url,
        "webhook_events":  "all",
        "webhook_secret":  "",
    })

    ws = WebhookServer(server, url)
    yield ws

    # Nettoyage : désactiver le webhook et arrêter le serveur
    client.post("/api/config", json={"webhook_enabled": False, "webhook_url": ""})
    server.shutdown()
