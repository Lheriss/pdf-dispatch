"""
test_api.py — Tests des routes API via le Flask test client.

Aucun serveur réel n'est démarré : Flask.test_client() simule les requêtes
HTTP en mémoire, ce qui rend ces tests rapides et sans effet de bord réseau.

Groupes couverts
----------------
  [1]  /healthz
  [2]  /api/state
  [3]  /api/config
  [4]  /api/upload — validation des entrées
  [5]  /api/upload — override par fichier
  [6]  /api/tasks
  [7]  /api/recent + /api/file
  [8]  /api/webhook/test (codes HTTP)
  [9]  /api/settings/regenerate-api-key
  [10] /api/stats/reset
  [11] /api/dirs
  [12] /api/email/test (connexion IMAP toujours en échec en test)
"""

import io
import json

import pytest


# ═════════════════════════════════════════════════════════════════════════════
# [1]  /healthz
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthz:
    def test_returns_200(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_ok_true(self, client):
        assert r.json["ok"] is True if (r := client.get("/healthz")) else True
        r = client.get("/healthz")
        assert r.json["ok"] is True


# ═════════════════════════════════════════════════════════════════════════════
# [2]  /api/state
# ═════════════════════════════════════════════════════════════════════════════

class TestApiState:
    def test_returns_200(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200

    def test_structure(self, client):
        d = client.get("/api/state").get_json()
        assert d is not None, "/api/state n'a pas retourné de JSON"
        for key in ("stats", "events", "queue", "app_config"):
            assert key in d, f"clé manquante dans /api/state : {key}"

    def test_stats_fields(self, client):
        stats = client.get("/api/state").json["stats"]
        for f in ("processed", "split_docs", "errors"):
            assert f in stats

    def test_config_present(self, client):
        cfg = client.get("/api/state").json["app_config"]
        assert "split_values" in cfg
        assert "separator_placement" in cfg
        assert "api_key" in cfg


# ═════════════════════════════════════════════════════════════════════════════
# [3]  /api/config
# ═════════════════════════════════════════════════════════════════════════════

class TestApiConfig:
    def test_update_separator_placement(self, client):
        r = client.post("/api/config", json={"separator_placement": "after"})
        assert r.status_code == 200
        assert r.json["config"]["separator_placement"] == "after"
        # Remettre la valeur par défaut
        client.post("/api/config", json={"separator_placement": "before"})

    def test_update_language(self, client):
        r = client.post("/api/config", json={"language": "en"})
        assert r.status_code == 200
        assert r.json["config"]["language"] == "en"
        client.post("/api/config", json={"language": "fr"})

    def test_update_webhook_enabled(self, client):
        r = client.post("/api/config", json={"webhook_enabled": True})
        assert r.status_code == 200
        assert r.json["config"]["webhook_enabled"] is True
        client.post("/api/config", json={"webhook_enabled": False})

    def test_validate_tokens_valid(self, client):
        tokens = [
            {"type": "trigger", "enabled": True},
            {"type": "counter", "enabled": True, "digits": 4},
        ]
        r = client.post("/api/config/validate_tokens", json={"tokens": tokens})
        assert r.status_code == 200
        assert r.json["ok"] is True

    def test_validate_tokens_no_counter(self, client):
        tokens = [{"type": "trigger", "enabled": True}]
        r = client.post("/api/config/validate_tokens", json={"tokens": tokens})
        assert r.status_code == 200
        assert r.json["ok"] is False


# ═════════════════════════════════════════════════════════════════════════════
# [4]  /api/upload — validation des entrées
# ═════════════════════════════════════════════════════════════════════════════

class TestApiUploadValidation:
    def test_no_file_returns_400(self, client):
        r = client.post("/api/upload")
        assert r.status_code == 400
        assert r.json["ok"] is False

    def test_invalid_separator_placement_returns_400(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "test.pdf"),
            "separator_placement": "center",
        }, content_type="multipart/form-data")
        assert r.status_code == 400
        assert "separator_placement" in r.json["error"]

    def test_invalid_split_values_json_returns_400(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "test.pdf"),
            "split_values": "not-json",
        }, content_type="multipart/form-data")
        assert r.status_code == 400
        assert r.json["ok"] is False

    def test_split_values_bad_page_handling_returns_400(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "test.pdf"),
            "split_values": '[{"value":"X","page_handling":"keep_or_delete"}]',
        }, content_type="multipart/form-data")
        assert r.status_code == 400

    def test_split_values_empty_value_returns_400(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "test.pdf"),
            "split_values": '[{"value":""}]',
        }, content_type="multipart/form-data")
        assert r.status_code == 400

    def test_non_pdf_extension_reported_in_errors(self, client):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(b"not a pdf"), "document.txt"),
        }, content_type="multipart/form-data")
        assert r.status_code == 200      # la requête aboutit…
        assert r.json["ok"] is True
        assert len(r.json["errors"]) > 0  # …mais le fichier est signalé en erreur
        assert r.json["saved"] == []


# ═════════════════════════════════════════════════════════════════════════════
# [5]  /api/upload — override par fichier
# ═════════════════════════════════════════════════════════════════════════════

class TestApiUploadOverride:
    def test_valid_upload_returns_task_id(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "ovr_base.pdf"),
        }, content_type="multipart/form-data")
        assert r.status_code == 200
        assert r.json["ok"] is True
        saved = r.json["saved"]
        assert len(saved) == 1
        assert "task_id" in saved[0]
        assert "filename" in saved[0]

    def test_override_echoed_in_response(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file":                (io.BytesIO(minimal_pdf), "ovr_echo.pdf"),
            "split_values":        '[{"value":"FAC","page_handling":"delete"}]',
            "separator_placement": "after",
            "subdirs_by_trigger":  "true",
        }, content_type="multipart/form-data")
        assert r.status_code == 200
        saved = r.json["saved"][0]
        assert "override" in saved
        assert saved["override"]["separator_placement"] == "after"
        assert saved["override"]["subdirs_by_trigger"] is True
        assert saved["override"]["split_values"][0]["value"] == "FAC"

    def test_no_override_not_in_response(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "ovr_none.pdf"),
        }, content_type="multipart/form-data")
        assert r.status_code == 200
        saved = r.json["saved"][0]
        assert "override" not in saved

    def test_override_stored_in_task(self, client, minimal_pdf):
        r = client.post("/api/upload", data={
            "file":                (io.BytesIO(minimal_pdf), "ovr_task.pdf"),
            "separator_placement": "after",
        }, content_type="multipart/form-data")
        task_id = r.json["saved"][0]["task_id"]

        task_r = client.get(f"/api/tasks/{task_id}")
        assert task_r.status_code == 200
        task = task_r.json["task"]
        assert task["config_override"]["separator_placement"] == "after"

    def test_empty_split_values_accepted(self, client, minimal_pdf):
        """split_values=[] est valide (tout code déclenche un split)."""
        r = client.post("/api/upload", data={
            "file":        (io.BytesIO(minimal_pdf), "ovr_empty_sv.pdf"),
            "split_values": "[]",
        }, content_type="multipart/form-data")
        assert r.status_code == 200
        assert r.json["ok"] is True

    def test_trigger_field_accepted_alongside_override(self, client, minimal_pdf):
        """trigger (no-barcode fallback) et split_values coexistent."""
        r = client.post("/api/upload", data={
            "file":        (io.BytesIO(minimal_pdf), "ovr_dual.pdf"),
            "trigger":     "INVOICE",
            "split_values": '[{"value":"FACTURE","page_handling":"keep"}]',
        }, content_type="multipart/form-data")
        assert r.status_code == 200

    def test_multiple_files_same_override(self, client, minimal_pdf):
        import io as _io
        # Flask test client accepte les listes de tuples pour les champs répétés
        data = {
            "separator_placement": "after",
        }
        # Envoyer les fichiers séparément via getlist — simulé avec deux requêtes
        # car Flask test client gère mal files[]=... avec list.
        # On vérifie plutôt qu'un second upload avec le même override fonctionne.
        for name in ("multi_a.pdf", "multi_b.pdf"):
            r = client.post("/api/upload", data={
                "file": (_io.BytesIO(minimal_pdf), name),
                "separator_placement": "after",
            }, content_type="multipart/form-data")
            assert r.status_code == 200
            saved = r.json["saved"]
            assert len(saved) == 1
            assert saved[0]["override"]["separator_placement"] == "after"


# ═════════════════════════════════════════════════════════════════════════════
# [6]  /api/tasks
# ═════════════════════════════════════════════════════════════════════════════

class TestApiTasks:
    def test_list_returns_ok(self, client):
        r = client.get("/api/tasks")
        assert r.status_code == 200
        assert r.json["ok"] is True
        assert isinstance(r.json["tasks"], list)
        assert isinstance(r.json["total"], int)

    def test_unknown_task_returns_404(self, client):
        r = client.get("/api/tasks/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404
        assert r.json["ok"] is False

    def test_task_created_by_upload(self, client, minimal_pdf):
        r_upload = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "task_lookup.pdf"),
        }, content_type="multipart/form-data")
        task_id = r_upload.json["saved"][0]["task_id"]

        r_task = client.get(f"/api/tasks/{task_id}")
        assert r_task.status_code == 200
        task = r_task.json["task"]
        assert task["id"] == task_id
        assert task["status"] in ("pending", "processing", "success", "error")
        assert "config_override" in task

    def test_task_list_n_parameter(self, client):
        r = client.get("/api/tasks?n=5")
        assert r.status_code == 200
        assert len(r.json["tasks"]) <= 5

    def test_task_fields_complete(self, client, minimal_pdf):
        r_upload = client.post("/api/upload", data={
            "file": (io.BytesIO(minimal_pdf), "task_fields.pdf"),
        }, content_type="multipart/form-data")
        task_id = r_upload.json["saved"][0]["task_id"]
        task = client.get(f"/api/tasks/{task_id}").json["task"]

        required = ("id", "filename", "status", "created_at", "updated_at",
                    "triggers", "outputs", "docs_count", "error", "config_override")
        for f in required:
            assert f in task, f"champ manquant dans la tâche : {f}"


# ═════════════════════════════════════════════════════════════════════════════
# [7]  /api/recent + /api/file
# ═════════════════════════════════════════════════════════════════════════════

class TestApiRecentAndFile:
    def test_recent_returns_ok(self, client):
        r = client.get("/api/recent")
        assert r.status_code == 200
        assert r.json["ok"] is True
        assert isinstance(r.json["files"], list)
        assert "total" in r.json

    def test_recent_n_parameter(self, client):
        r = client.get("/api/recent?n=3")
        assert r.status_code == 200
        assert len(r.json["files"]) <= 3

    def test_recent_file_fields(self, client):
        # Déposer un fichier réel dans output/
        import pathlib
        out = pathlib.Path(client.application.config.get("DATA_DIR",
              __import__("os").environ["DATA_DIR"])) / "output" / "test_recent.pdf"
        out.write_bytes(b"%PDF-1.4")
        r = client.get("/api/recent")
        assert r.status_code == 200
        if r.json["files"]:
            f = r.json["files"][0]
            for field in ("filename", "path", "download_url", "size_bytes", "modified"):
                assert field in f

    def test_file_download_not_found(self, client):
        r = client.get("/api/file/output/nonexistent_xyz.pdf")
        assert r.status_code == 404

    def test_file_download_traversal_blocked(self, client):
        """Tentative de path traversal vers le fichier de config → bloqué."""
        r = client.get("/api/file/.splitter_config.json")
        # Flask normalise les paths → soit 403 soit 404, jamais 200
        assert r.status_code in (403, 404)


# ═════════════════════════════════════════════════════════════════════════════
# [8]  /api/webhook/test (codes HTTP uniquement)
# ═════════════════════════════════════════════════════════════════════════════

class TestApiWebhookTest:
    def test_no_url_returns_400(self, client):
        client.post("/api/config", json={"webhook_url": "", "webhook_enabled": False})
        r = client.post("/api/webhook/test")
        assert r.status_code == 400
        assert r.json["ok"] is False

    def test_unreachable_url_returns_error(self, client):
        client.post("/api/config", json={
            "webhook_enabled": True,
            "webhook_url":     "http://127.0.0.1:19999/hook",  # rien n'écoute ici
        })
        r = client.post("/api/webhook/test")
        # L'URL est injoignable : la route retourne ok=False avec un message d'erreur
        assert r.json["ok"] is False
        # Nettoyage
        client.post("/api/config", json={"webhook_enabled": False, "webhook_url": ""})


# ═════════════════════════════════════════════════════════════════════════════
# [9]  /api/settings/regenerate-api-key
# ═════════════════════════════════════════════════════════════════════════════

class TestApiRegenerateKey:
    def test_regenerate_returns_new_key(self, client):
        r = client.post("/api/settings/regenerate-api-key")
        assert r.status_code == 200
        assert r.json["ok"] is True
        key = r.json["key"]
        assert isinstance(key, str) and len(key) == 64

    def test_regenerate_changes_key(self, client):
        r1 = client.post("/api/settings/regenerate-api-key")
        r2 = client.post("/api/settings/regenerate-api-key")
        assert r1.json["key"] != r2.json["key"]

    def test_regenerate_key_in_state(self, client):
        r_regen = client.post("/api/settings/regenerate-api-key")
        new_key = r_regen.json["key"]
        state_key = client.get("/api/state").json["app_config"]["api_key"]
        assert state_key == new_key


# ═════════════════════════════════════════════════════════════════════════════
# [10] /api/stats/reset
# ═════════════════════════════════════════════════════════════════════════════

class TestApiStatsReset:
    def test_reset_returns_ok(self, client):
        r = client.post("/api/stats/reset")
        assert r.status_code == 200
        assert r.json["ok"] is True

    def test_stats_zeroed_after_reset(self, client):
        client.post("/api/stats/reset")
        stats = client.get("/api/state").json["stats"]
        assert stats["processed"]  == 0
        assert stats["split_docs"] == 0
        assert stats["errors"]     == 0


# ═════════════════════════════════════════════════════════════════════════════
# [11] /api/dirs
# ═════════════════════════════════════════════════════════════════════════════

class TestApiDirs:
    def test_recreate_unknown_key_returns_400(self, client):
        r = client.post("/api/dirs/recreate", json={"key": "does_not_exist"})
        assert r.status_code == 400
        assert r.json["ok"] is False

    def test_rename_unknown_key_returns_400(self, client):
        r = client.post("/api/dirs/rename",
                        json={"key": "does_not_exist", "path": "output/x"})
        assert r.status_code == 400
        assert r.json["ok"] is False

    def test_recreate_no_code_dir(self, client):
        """Recréer le dossier no_code (idempotent si déjà présent)."""
        r = client.post("/api/dirs/recreate", json={"key": "no_code"})
        assert r.status_code == 200
        assert r.json["ok"] is True


# ═════════════════════════════════════════════════════════════════════════════
# [12] /api/email/test
# ═════════════════════════════════════════════════════════════════════════════

class TestApiEmailTest:
    def test_bad_host_returns_502(self, client):
        """Un hôte IMAP injoignable doit retourner 502 (et non 200)."""
        r = client.post("/api/email/test", json={
            "name":       "test",
            "host":       "imap.host.invalid",
            "port":       993,
            "username":   "user@example.com",
            "password":   "secret",
            "folder":     "INBOX",
            "verify_ssl": False,
        })
        assert r.status_code == 502
        assert r.json["ok"] is False
        # La clé d'erreur doit être "error" (pas "message")
        assert "error" in r.json
