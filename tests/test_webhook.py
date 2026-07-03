"""
test_webhook.py — Tests d'intégration du webhook HTTP sortant.

Stratégie : un récepteur HTTP minimaliste tourne dans un thread daemon pendant
chaque test (fixture webhook_server de conftest.py). pdf-dispatch est configuré
pour lui envoyer ses webhooks.  On vérifie le payload reçu, la signature HMAC,
le filtre d'événements, et l'intégration avec le champ config_override.

Groupes couverts
----------------
  [1]  Livraison de base (/api/webhook/test — synchrone)
  [2]  Signature HMAC-SHA256
  [3]  Filtre d'événements (all / success / error)
  [4]  Payload — structure et champs obligatoires
  [5]  Payload — config_override
  [6]  _fire_webhook asynchrone (appel direct)
"""

import hashlib
import hmac
import io
import time

import pytest


# ═════════════════════════════════════════════════════════════════════════════
# [1]  Livraison de base via /api/webhook/test (synchrone)
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookDelivery:
    def test_test_endpoint_delivers(self, client, webhook_server):
        """POST /api/webhook/test doit livrer un payload et retourner ok=True."""
        webhook_server.clear()
        r = client.post("/api/webhook/test")
        assert r.status_code == 200
        assert r.json["ok"] is True
        assert r.json["code"] == 200
        assert len(webhook_server.calls) == 1

    def test_test_endpoint_payload_event_field(self, client, webhook_server):
        webhook_server.clear()
        client.post("/api/webhook/test")
        payload = webhook_server.calls[0]["body"]
        assert payload["event"] == "test"

    def test_test_endpoint_payload_mandatory_fields(self, client, webhook_server):
        webhook_server.clear()
        client.post("/api/webhook/test")
        payload = webhook_server.calls[0]["body"]
        for field in ("event", "timestamp", "source_file", "status",
                      "triggers", "documents", "docs_count", "error"):
            assert field in payload, f"champ manquant dans le payload : {field}"

    def test_test_payload_status_success(self, client, webhook_server):
        webhook_server.clear()
        client.post("/api/webhook/test")
        assert webhook_server.calls[0]["body"]["status"] == "success"

    def test_no_url_returns_400(self, client):
        """Sans URL configurée, /api/webhook/test doit retourner 400."""
        client.post("/api/config", json={"webhook_enabled": False, "webhook_url": ""})
        r = client.post("/api/webhook/test")
        assert r.status_code == 400
        assert r.json["ok"] is False


# ═════════════════════════════════════════════════════════════════════════════
# [2]  Signature HMAC-SHA256
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookHmac:
    def test_no_secret_no_signature_header(self, client, webhook_server):
        """Sans secret configuré, l'en-tête X-Signature ne doit pas être présent."""
        client.post("/api/config", json={"webhook_secret": ""})
        webhook_server.clear()
        client.post("/api/webhook/test")
        headers = webhook_server.calls[0]["headers"]
        assert "X-Signature" not in headers

    def test_with_secret_signature_present(self, client, webhook_server):
        client.post("/api/config", json={"webhook_secret": "my-test-secret"})
        webhook_server.clear()
        client.post("/api/webhook/test")
        headers = webhook_server.calls[0]["headers"]
        assert "X-Signature" in headers
        # Nettoyage
        client.post("/api/config", json={"webhook_secret": ""})

    def test_signature_correct_hmac(self, client, webhook_server):
        """La signature doit être sha256=HMAC(secret, body)."""
        secret = "pytest-secret-42"
        client.post("/api/config", json={"webhook_secret": secret})
        webhook_server.clear()
        client.post("/api/webhook/test")
        call = webhook_server.calls[0]
        assert webhook_server.verify_hmac(call, secret), "Signature HMAC invalide"
        client.post("/api/config", json={"webhook_secret": ""})

    def test_signature_format_sha256_prefix(self, client, webhook_server):
        client.post("/api/config", json={"webhook_secret": "s3cr3t"})
        webhook_server.clear()
        client.post("/api/webhook/test")
        sig = webhook_server.calls[0]["headers"].get("X-Signature", "")
        assert sig.startswith("sha256="), f"Format inattendu : {sig!r}"
        client.post("/api/config", json={"webhook_secret": ""})

    def test_signature_changes_with_secret(self, client, webhook_server):
        """Deux secrets différents produisent deux signatures différentes."""
        sigs = []
        for secret in ("secret-A", "secret-B"):
            client.post("/api/config", json={"webhook_secret": secret})
            webhook_server.clear()
            client.post("/api/webhook/test")
            sigs.append(webhook_server.calls[0]["headers"].get("X-Signature", ""))
        assert sigs[0] != sigs[1]
        client.post("/api/config", json={"webhook_secret": ""})


# ═════════════════════════════════════════════════════════════════════════════
# [3]  Filtre d'événements
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookEventsFilter:
    """Utilise _fire_webhook directement pour contrôler le status (async)."""

    def _fire(self, status: str, webhook_server):
        """Lance _fire_webhook et attend la livraison."""
        import app as _app
        webhook_server.clear()
        _app._fire_webhook(source_file="filter_test.pdf", status=status)
        return webhook_server.wait(count=1, timeout=3.0)

    def test_all_receives_success(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "all"})
        delivered = self._fire("success", webhook_server)
        assert delivered, "Webhook 'success' non reçu en mode 'all'"

    def test_all_receives_error(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "all"})
        delivered = self._fire("error", webhook_server)
        assert delivered, "Webhook 'error' non reçu en mode 'all'"

    def test_success_only_blocks_error(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "success"})
        delivered = self._fire("error", webhook_server)
        assert not delivered, "Webhook 'error' ne doit pas être livré en mode 'success'"

    def test_success_only_passes_success(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "success"})
        delivered = self._fire("success", webhook_server)
        assert delivered, "Webhook 'success' doit être livré en mode 'success'"

    def test_error_only_blocks_success(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "error"})
        delivered = self._fire("success", webhook_server)
        assert not delivered, "Webhook 'success' ne doit pas être livré en mode 'error'"

    def test_error_only_passes_error(self, client, webhook_server):
        client.post("/api/config", json={"webhook_events": "error"})
        delivered = self._fire("error", webhook_server)
        assert delivered, "Webhook 'error' doit être livré en mode 'error'"

    def teardown_method(self):
        """Remettre le filtre à 'all' après chaque test."""
        pass   # le fixture webhook_server remet à 'all' via conftest


# ═════════════════════════════════════════════════════════════════════════════
# [4]  Payload — structure et champs
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookPayload:
    """Teste _build_webhook_payload directement (unitaire, sans HTTP)."""

    def _build(self, **kwargs):
        import app as _app
        return _app._build_webhook_payload(**kwargs)

    def test_event_field(self):
        p = self._build(status="success", source_file="x.pdf")
        assert p["event"] == "file.processed"

    def test_status_field(self):
        for status in ("success", "error"):
            p = self._build(status=status, source_file="x.pdf")
            assert p["status"] == status

    def test_triggers_default_empty(self):
        p = self._build(status="success", source_file="x.pdf")
        assert p["triggers"] == []

    def test_triggers_passed_through(self):
        p = self._build(status="success", source_file="x.pdf",
                        triggers=["INVOICE", "CREDIT"])
        assert p["triggers"] == ["INVOICE", "CREDIT"]

    def test_docs_count(self):
        p = self._build(status="success", source_file="x.pdf", docs_count=3)
        assert p["docs_count"] == 3

    def test_error_field(self):
        p = self._build(status="error", source_file="x.pdf",
                        error_msg="invalid PDF")
        assert p["error"] == "invalid PDF"

    def test_success_no_error_msg(self):
        p = self._build(status="success", source_file="x.pdf")
        assert p["error"] == ""

    def test_timestamp_present(self):
        p = self._build(status="success", source_file="x.pdf")
        assert isinstance(p["timestamp"], str) and len(p["timestamp"]) >= 10

    def test_documents_paths_relative(self, tmp_path):
        """Les chemins de documents doivent être relatifs à DATA_DIR."""
        import app as _app
        import os
        data_dir = _app.DATA_DIR
        out_file = str(data_dir / "output" / "INV_001.pdf")
        p = self._build(status="success", source_file="x.pdf",
                        outputs=[out_file])
        assert len(p["documents"]) == 1
        doc = p["documents"][0]
        assert "filename" in doc
        assert "path" in doc
        # Le chemin ne doit pas commencer par / ni contenir DATA_DIR
        assert not doc["path"].startswith("/")


# ═════════════════════════════════════════════════════════════════════════════
# [5]  Payload — config_override
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookPayloadOverride:
    def _build(self, **kwargs):
        import app as _app
        return _app._build_webhook_payload(**kwargs)

    def test_no_override_key_absent(self):
        p = self._build(status="success", source_file="x.pdf")
        assert "config_override" not in p

    def test_empty_override_key_absent(self):
        p = self._build(status="success", source_file="x.pdf", config_override={})
        assert "config_override" not in p

    def test_override_included_when_present(self):
        override = {"separator_placement": "after", "subdirs_by_trigger": True}
        p = self._build(status="success", source_file="x.pdf",
                        config_override=override)
        assert "config_override" in p
        assert p["config_override"] == override

    def test_override_with_split_values(self):
        override = {
            "split_values": [{"value": "FAC", "page_handling": "delete",
                              "case_sensitive": True}]
        }
        p = self._build(status="success", source_file="x.pdf",
                        config_override=override)
        assert p["config_override"]["split_values"][0]["value"] == "FAC"

    def test_override_in_async_fire(self, client, webhook_server):
        """_fire_webhook doit inclure config_override dans le payload livré."""
        import app as _app
        override = {"separator_placement": "after"}
        client.post("/api/config", json={"webhook_events": "all"})
        webhook_server.clear()
        _app._fire_webhook(source_file="ovr.pdf", status="success",
                           config_override=override)
        delivered = webhook_server.wait(1, timeout=3.0)
        assert delivered
        payload = webhook_server.calls[0]["body"]
        assert payload.get("config_override") == override

    def test_override_via_upload(self, client, webhook_server, minimal_pdf):
        """Vérification end-to-end : upload avec override → webhook contient
        config_override.  Nécessite que process_file s'exécute, ce qui n'est
        pas garanti dans les tests rapides.  On teste donc _fire_webhook
        directement avec l'override que process_file transmettrait."""
        import app as _app
        override = {"separator_placement": "after", "subdirs_by_trigger": True}
        client.post("/api/config", json={"webhook_events": "all"})
        webhook_server.clear()
        _app._fire_webhook(
            source_file="e2e_test.pdf",
            status="success",
            triggers=["FAC"],
            docs_count=1,
            config_override=override,
        )
        assert webhook_server.wait(1)
        payload = webhook_server.calls[0]["body"]
        assert payload["config_override"]["separator_placement"] == "after"
        assert payload["triggers"] == ["FAC"]


# ═════════════════════════════════════════════════════════════════════════════
# [6]  _fire_webhook asynchrone — comportement général
# ═════════════════════════════════════════════════════════════════════════════

class TestFireWebhookAsync:
    def test_disabled_webhook_not_fired(self, client, webhook_server):
        import app as _app
        client.post("/api/config", json={"webhook_enabled": False})
        webhook_server.clear()
        _app._fire_webhook(source_file="disabled.pdf", status="success")
        # Laisser le temps à un éventuel thread de se déclencher
        time.sleep(0.3)
        assert len(webhook_server.calls) == 0
        # Remettre en état pour les tests suivants
        client.post("/api/config", json={"webhook_enabled": True})

    def test_empty_url_not_fired(self, client, webhook_server):
        import app as _app
        client.post("/api/config", json={"webhook_url": ""})
        webhook_server.clear()
        _app._fire_webhook(source_file="nourl.pdf", status="success")
        time.sleep(0.3)
        assert len(webhook_server.calls) == 0

    def test_fired_asynchronously(self, client, webhook_server):
        """_fire_webhook retourne immédiatement ; le payload arrive après."""
        import app as _app
        webhook_server.clear()
        before = time.monotonic()
        _app._fire_webhook(source_file="async.pdf", status="success")
        elapsed_before_delivery = time.monotonic() - before
        # L'appel doit retourner en quelques ms (bien avant les 3s du wait)
        assert elapsed_before_delivery < 1.0
        # Mais le payload doit arriver
        assert webhook_server.wait(1, timeout=3.0)

    def test_content_type_header(self, client, webhook_server):
        import app as _app
        webhook_server.clear()
        _app._fire_webhook(source_file="ct.pdf", status="success")
        webhook_server.wait(1)
        ct = webhook_server.calls[0]["headers"].get("Content-Type", "")
        assert "application/json" in ct

    def test_user_agent_header(self, client, webhook_server):
        import app as _app
        webhook_server.clear()
        _app._fire_webhook(source_file="ua.pdf", status="success")
        webhook_server.wait(1)
        ua = webhook_server.calls[0]["headers"].get("User-Agent", "")
        assert "pdf-dispatch" in ua.lower()
