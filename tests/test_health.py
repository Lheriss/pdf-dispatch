"""
test_health.py — GET /healthz runs real checks (dispatch/health.py).

Each check is driven into its failing state and the endpoint must answer
503 naming it; in the test process the background services are not started,
so their checks report "n/a" and /healthz stays 200.
"""

import threading
import time

import pytest
from flask.testing import FlaskClient
from watchdog.observers import Observer

import app as _app
from dispatch import email_poller, health, retention, watcher


@pytest.fixture
def anon():
    return FlaskClient(_app.app, _app.app.response_class, use_cookies=False)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(health, "_last_failed", frozenset())


def _dead_thread():
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    return t


@pytest.fixture
def live_thread():
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    yield t
    stop.set()
    t.join()


class TestHealthzEndpoint:
    def test_healthy_in_test_process(self, anon):
        r = anon.get("/healthz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True and body["failed"] == []
        assert set(body["checks"]) == {"watcher", "input_dir", "data_dir", "disk",
                                       "email_poller", "retention"}
        assert body["checks"]["watcher"] == "n/a"
        assert r.headers["Cache-Control"] == "no-store"

    def test_public_and_minimal(self, anon):
        # Public endpoint: only names and statuses, never paths or versions.
        body = anon.get("/healthz").get_json()
        assert set(body) == {"ok", "checks", "failed"}
        assert set(body["checks"].values()) <= {"ok", "failed", "disabled", "n/a"}

    def test_failure_gives_503_and_names_the_check(self, anon, monkeypatch, tmp_path):
        monkeypatch.setattr(health, "DATA_DIR", tmp_path / "missing")
        r = anon.get("/healthz")
        assert r.status_code == 503
        body = r.get_json()
        assert body["ok"] is False
        assert "data_dir" in body["failed"]

    def test_crashing_check_is_a_failure_not_a_500(self, anon, monkeypatch):
        def boom():
            raise RuntimeError("x")
        monkeypatch.setitem(health.CHECKS, "disk", boom)
        r = anon.get("/healthz")
        assert r.status_code == 503 and r.get_json()["failed"] == ["disk"]

    def test_state_change_logged_once(self, monkeypatch, caplog, tmp_path):
        monkeypatch.setattr(health, "DATA_DIR", tmp_path / "missing")
        caplog.set_level("WARNING", logger="pdf-dispatch")
        health.run_checks()
        health.run_checks()
        assert caplog.text.count("unhealthy") == 1


class TestChecks:
    def test_data_dir_writable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(health, "DATA_DIR", tmp_path)
        assert health._check_data_dir() == "ok"
        assert list(tmp_path.iterdir()) == []       # probe file removed

    def test_data_dir_read_only(self, monkeypatch, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            monkeypatch.setattr(health, "DATA_DIR", ro)
            import os
            if os.geteuid() == 0:
                pytest.skip("root ignores directory permissions")
            assert health._check_data_dir() == "failed"
        finally:
            ro.chmod(0o700)

    def test_disk_low(self, monkeypatch):
        monkeypatch.setattr(health, "MIN_FREE_MB", 10**12)
        assert health._check_disk() == "failed"

    def test_input_dir_follows_rename(self, monkeypatch, tmp_path):
        from dispatch import config
        monkeypatch.setattr(config, "INPUT_DIR", tmp_path / "renamed")
        assert health._check_input_dir() == "failed"
        (tmp_path / "renamed").mkdir()
        assert health._check_input_dir() == "ok"

    def test_watcher_alive_then_stopped(self, monkeypatch, tmp_path):
        obs = Observer()
        obs.schedule(watcher.PDFHandler(), str(tmp_path), recursive=False)
        obs.start()
        monkeypatch.setattr(watcher, "_observer", obs)
        try:
            assert health._check_watcher() == "ok"
        finally:
            obs.stop()
            obs.join()
        assert health._check_watcher() == "failed"

    def test_email_disabled_without_enabled_account(self, monkeypatch):
        monkeypatch.setattr(health, "get_config",
                            lambda: {"email_configs": [{"enabled": False}]})
        assert health._check_email_poller() == "disabled"

    def test_email_poller_states(self, monkeypatch, live_thread):
        monkeypatch.setattr(health, "get_config",
                            lambda: {"email_configs": [{"enabled": True}]})
        monkeypatch.setattr(email_poller, "_email_thread", None)
        assert health._check_email_poller() == "n/a"
        monkeypatch.setattr(email_poller, "_email_thread", _dead_thread())
        assert health._check_email_poller() == "failed"
        monkeypatch.setattr(email_poller, "_email_thread", live_thread)
        monkeypatch.setattr(email_poller, "_email_heartbeat", time.time())
        assert health._check_email_poller() == "ok"
        monkeypatch.setattr(email_poller, "_email_heartbeat",
                            time.time() - health.EMAIL_STALL_SECONDS - 60)
        assert health._check_email_poller() == "failed"     # stuck loop

    def test_email_poller_stopped(self, monkeypatch, live_thread):
        monkeypatch.setattr(health, "get_config",
                            lambda: {"email_configs": [{"enabled": True}]})
        monkeypatch.setattr(email_poller, "_email_thread", live_thread)
        monkeypatch.setattr(email_poller, "_email_heartbeat", time.time())
        stop = threading.Event()
        stop.set()
        monkeypatch.setattr(email_poller, "_email_stop", stop)
        assert health._check_email_poller() == "failed"

    def test_retention_states(self, monkeypatch, live_thread):
        monkeypatch.setattr(retention, "RETENTION_DAYS_PROCESSED", 0)
        monkeypatch.setattr(retention, "RETENTION_DAYS_ERROR", 0)
        assert health._check_retention() == "disabled"
        monkeypatch.setattr(retention, "RETENTION_DAYS_PROCESSED", 30)
        monkeypatch.setattr(retention, "_retention_thread", None)
        assert health._check_retention() == "n/a"
        monkeypatch.setattr(retention, "_retention_thread", _dead_thread())
        assert health._check_retention() == "failed"
        monkeypatch.setattr(retention, "_retention_thread", live_thread)
        assert health._check_retention() == "ok"

    def test_poller_loop_updates_heartbeat(self, monkeypatch):
        # The real loop must stamp the heartbeat on each iteration.
        stop = threading.Event()
        monkeypatch.setattr(email_poller, "_email_stop", stop)
        monkeypatch.setattr(email_poller, "_email_heartbeat", 0.0)
        monkeypatch.setattr(email_poller, "get_config", lambda: {"email_configs": []})
        t = threading.Thread(target=email_poller._imap_poll, daemon=True)
        t.start()
        deadline = time.time() + 3
        while email_poller._email_heartbeat == 0.0 and time.time() < deadline:
            time.sleep(0.02)
        stop.set()
        t.join(timeout=5)
        assert email_poller._email_heartbeat > 0
