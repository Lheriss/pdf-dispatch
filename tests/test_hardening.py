"""
test_hardening.py — secrets handling, post-processing hook, route coverage.

  TestEmailSecret     EMAIL_SECRET is fail-closed: no ephemeral key, never
                      logged; unreadable passwords are reported as such.
  TestHook            the post-processing script does not receive
                      pdf-dispatch's secrets, and a timeout kills the whole
                      process group (background children included).
  TestRouteCoverage   every route of the application, including routes added
                      later, refuses anonymous requests, except a fixed list
                      of public paths.
"""

import os
import re
import stat
import time

import pytest
from flask.testing import FlaskClient

import app as _app
from dispatch import crypto, email_poller, hook
from dispatch.routes import auth as auth_routes

CSRF = {"X-Requested-With": "pdf-dispatch"}
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


# ── EMAIL_SECRET ──────────────────────────────────────────────────────────────

@pytest.fixture
def no_secret(monkeypatch):
    """EMAIL_SECRET absent and no cached key."""
    monkeypatch.delenv("EMAIL_SECRET", raising=False)
    monkeypatch.setattr(crypto, "_EMAIL_SECRET_KEY", None)


@pytest.fixture
def secret(monkeypatch):
    """Switch to a given EMAIL_SECRET (cache reset)."""
    def use(value):
        monkeypatch.setenv("EMAIL_SECRET", value)
        monkeypatch.setattr(crypto, "_EMAIL_SECRET_KEY", None)
    return use


def _email_cfg(name, **extra):
    return {"name": name, "host": "imap.example.com", "port": 993,
            "username": "u@example.com", "folder": "INBOX", **extra}


class TestEmailSecret:
    def test_no_ephemeral_key(self, no_secret):
        with pytest.raises(crypto.EmailSecretMissing):
            crypto.encrypt_password("hunter2")
        assert crypto._EMAIL_SECRET_KEY is None      # nothing generated, nothing cached

    def test_missing_secret_never_logs_a_key(self, no_secret, caplog):
        caplog.set_level("DEBUG")
        crypto.warn_if_email_secret_missing()
        with pytest.raises(crypto.EmailSecretMissing):
            crypto._get_email_secret()
        assert "EMAIL_SECRET" in caplog.text
        assert not _HEX64.search(caplog.text)

    def test_configured_secret_not_logged(self, secret, caplog):
        value = "c" * 64
        secret(value)
        caplog.set_level("DEBUG")
        crypto._get_email_secret()
        assert value not in caplog.text

    def test_create_with_password_refused(self, client, no_secret):
        before = len(_app.get_config().get("email_configs", []))
        r = client.post("/api/email/configs", json=_email_cfg("nosecret", password="pw"))
        assert r.status_code == 400
        assert "EMAIL_SECRET" in r.get_json()["error"]
        assert len(_app.get_config().get("email_configs", [])) == before

    def test_update_with_password_refused(self, client, secret, monkeypatch):
        secret("d" * 64)
        r = client.post("/api/email/configs", json=_email_cfg("upd-nosecret"))
        cid = r.get_json()["config"]["id"]
        try:
            monkeypatch.delenv("EMAIL_SECRET")
            monkeypatch.setattr(crypto, "_EMAIL_SECRET_KEY", None)
            r = client.post(f"/api/email/configs/{cid}", json=_email_cfg("upd-nosecret", password="pw"))
            assert r.status_code == 400
            assert "EMAIL_SECRET" in r.get_json()["error"]
            stored = next(c for c in _app.get_config()["email_configs"] if c["id"] == cid)
            assert stored["password_enc"] == ""
        finally:
            client.delete(f"/api/email/configs/{cid}")

    def test_wrong_key_reported(self, secret):
        secret("a" * 64)
        token = crypto.encrypt_password("pw")
        secret("b" * 64)
        with pytest.raises(crypto.PasswordUnreadable) as exc:
            crypto.decrypt_password_checked(token)
        assert "EMAIL_SECRET" in str(exc.value)
        assert crypto.decrypt_password(token) == ""   # best-effort variant unchanged

    def test_missing_key_reported_on_read(self, secret, monkeypatch):
        secret("a" * 64)
        token = crypto.encrypt_password("pw")
        monkeypatch.delenv("EMAIL_SECRET")
        monkeypatch.setattr(crypto, "_EMAIL_SECRET_KEY", None)
        with pytest.raises(crypto.PasswordUnreadable) as exc:
            crypto.decrypt_password_checked(token)
        assert "not set" in str(exc.value)

    def test_round_trip(self, secret):
        secret("e" * 64)
        assert crypto.decrypt_password_checked(crypto.encrypt_password("pässwörd")) == "pässwörd"
        assert crypto.decrypt_password_checked("") == ""

    def test_poller_says_unreadable_not_incomplete(self, secret, monkeypatch):
        secret("a" * 64)
        token = crypto.encrypt_password("pw")
        secret("b" * 64)
        events = []
        monkeypatch.setattr(email_poller, "log_event",
                            lambda lvl, msg, *a, **k: events.append((lvl, str(msg))))
        monkeypatch.setattr(email_poller, "_proc_ids_load", lambda cid: ([], None))
        email_poller._imap_process({"id": "x1", "name": "lab", "host": "imap.example.com",
                                    "port": 993, "username": "u", "password_enc": token})
        assert any(lvl == "error" and "EMAIL_SECRET" in msg for lvl, msg in events)
        assert not any("ncomplet" in msg for _, msg in events)

    def test_connection_test_reports_unreadable(self, client, secret):
        secret("a" * 64)
        r = client.post("/api/email/configs", json=_email_cfg("unreadable", password="pw"))
        cid = r.get_json()["config"]["id"]
        try:
            secret("b" * 64)
            r = client.post("/api/email/test", json={
                "id": cid, "name": "unreadable", "host": "imap.example.com",
                "port": 993, "username": "u@example.com"})
            assert r.status_code == 400
            assert "EMAIL_SECRET" in r.get_json()["error"]
        finally:
            client.delete(f"/api/email/configs/{cid}")


# ── Post-processing hook ──────────────────────────────────────────────────────

def _script(tmp_path, body: str):
    p = tmp_path / "hook.sh"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def run_hook(monkeypatch):
    events = []
    monkeypatch.setattr(hook, "log_event",
                        lambda lvl, msg, *a, **k: events.append((lvl, str(msg))))

    def run(script, timeout=10):
        monkeypatch.setattr(hook, "POST_PROCESS_SCRIPT", script)
        monkeypatch.setattr(hook, "POST_PROCESS_TIMEOUT", timeout)
        hook._run_post_process_hook("doc.pdf", "success", ["T1"], ["/data/out/a.pdf"], 1)
        return events
    return run


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"   # zombie = dead
    except FileNotFoundError:
        return False


class TestHook:
    def test_secrets_not_passed(self, tmp_path, run_hook, monkeypatch):
        monkeypatch.setenv("EMAIL_SECRET", "secret-value-1")
        monkeypatch.setenv("API_KEY", "secret-value-2")
        monkeypatch.setenv("APP_PASSWORD", "secret-value-3")
        monkeypatch.setenv("MY_NOTIFY_TOKEN", "kept-value")
        events = run_hook(_script(tmp_path, "env"))
        out = "\n".join(msg for _, msg in events)
        assert "secret-value" not in out
        assert "MY_NOTIFY_TOKEN=kept-value" in out          # user variables still passed
        assert "PDF_DISPATCH_SOURCE=doc.pdf" in out
        assert "PDF_DISPATCH_TRIGGERS=T1" in out

    def test_exit_code_reported(self, tmp_path, run_hook):
        events = run_hook(_script(tmp_path, "echo boom >&2; exit 3"))
        assert ("warning", "[hook] boom") in events
        assert any("exited with code 3" in msg for _, msg in events)

    def test_timeout_kills_background_children(self, tmp_path, run_hook, monkeypatch):
        pidfile = tmp_path / "child.pid"
        monkeypatch.setenv("CHILD_PIDFILE", str(pidfile))
        t0 = time.monotonic()
        events = run_hook(_script(tmp_path, 'sleep 60 &\necho $! > "$CHILD_PIDFILE"\nwait'),
                          timeout=1)
        assert time.monotonic() - t0 < 10
        assert any("timed out" in msg for _, msg in events)
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "background child of the hook survived the timeout"


# ── Every route refuses anonymous access ──────────────────────────────────────

_EXPECTED_PUBLIC = {"/healthz", "/login", "/logout", "/setup",
                    "/api/openapi.json", "/api/openapi.yaml", "/api/docs"}


def _routes():
    out = []
    for rule in _app.app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.rule in _EXPECTED_PUBLIC:
            continue
        path = re.sub(r"<[^>]+>", "x", rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


class TestRouteCoverage:
    def test_public_paths_unchanged(self):
        # Adding a public path must be a deliberate change of this test.
        assert set(auth_routes._PUBLIC_PATHS) == _EXPECTED_PUBLIC

    def test_enough_routes_discovered(self):
        # 25 protected method+path pairs in v1.8.2; guards against a broken scan.
        assert len(_routes()) >= 20

    @pytest.mark.parametrize("method,path", _routes())
    def test_anonymous_refused(self, method, path):
        anon = FlaskClient(_app.app, _app.app.response_class, use_cookies=False)
        kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        r = anon.open(path, method=method, headers=CSRF, **kwargs)
        if r.status_code in (302, 303):
            assert r.headers["Location"].split("?")[0].endswith(("/login", "/setup")), \
                f"{method} {path} redirects to {r.headers['Location']}"
        else:
            assert r.status_code == 401, f"{method} {path} → {r.status_code}"

    @pytest.mark.parametrize("method,path", _routes())
    def test_wrong_api_key_refused(self, method, path):
        anon = FlaskClient(_app.app, _app.app.response_class, use_cookies=False)
        kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        r = anon.open(path, method=method, headers={**CSRF, "X-API-Key": "wrong"}, **kwargs)
        assert r.status_code == 401, f"{method} {path} → {r.status_code}"
