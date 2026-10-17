"""
test_auth.py — authentication matrix (routes/auth.py).

Covers the three accepted credentials (X-API-Key, web-interface session,
HTTP Basic), the anonymous cases, CSRF protection of session-authenticated
writes, sign-in/sign-out, session invalidation on key regeneration, the
API_KEY env override, open-redirect protection and the config keys that
must never be readable or settable from the API.

Uses anonymous clients (no automatic X-API-Key), unlike the `client`
fixture from conftest.py.
"""

import base64

import pytest
from flask.testing import FlaskClient

import app as _app

CSRF = {"X-Requested-With": "pdf-dispatch"}
HTML = {"Accept": "text/html"}


def _key() -> str:
    return _app.get_config().get("api_key", "")


@pytest.fixture
def anon():
    """Fresh anonymous client with its own cookie jar."""
    return FlaskClient(_app.app, _app.app.response_class, use_cookies=True)


@pytest.fixture
def signed_in(anon):
    r = anon.post("/login", data={"api_key": _key(), "next": "/"})
    assert r.status_code == 302
    return anon


@pytest.fixture(autouse=True)
def _no_env_auth(monkeypatch):
    for var in ("API_KEY", "APP_USERNAME", "APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


# ── Anonymous access ─────────────────────────────────────────────────────────

class TestAnonymous:
    def test_key_exists_after_startup(self):
        assert len(_key()) == 64

    def test_healthz_is_public(self, anon):
        assert anon.get("/healthz").status_code == 200

    def test_page_redirects_to_login(self, anon):
        r = anon.get("/", headers=HTML)
        assert r.status_code == 302
        assert r.headers["Location"].startswith("/login")

    def test_api_returns_json_401_without_basic_challenge(self, anon):
        r = anon.get("/api/state")
        assert r.status_code == 401
        assert r.json["ok"] is False
        assert "WWW-Authenticate" not in r.headers

    def test_api_write_rejected(self, anon):
        r = anon.post("/api/config", json={"log_verbose": False}, headers=CSRF)
        assert r.status_code == 401

    def test_static_assets_public(self, anon):
        assert anon.get("/static/css/style.css").status_code == 200

    @pytest.mark.parametrize("path", ["/api/openapi.json", "/api/openapi.yaml", "/api/docs"])
    def test_api_description_public(self, anon, path):
        assert anon.get(path).status_code == 200

    def test_runtime_info_protected(self, anon):
        assert anon.get("/api/runtime").status_code == 401

    def test_login_page_renders(self, anon):
        r = anon.get("/login")
        assert r.status_code == 200
        assert b'name="api_key"' in r.data


# ── X-API-Key ────────────────────────────────────────────────────────────────

class TestApiKeyHeader:
    def test_valid_key(self, anon):
        assert anon.get("/api/state", headers={"X-API-Key": _key()}).status_code == 200

    def test_wrong_key(self, anon):
        assert anon.get("/api/state", headers={"X-API-Key": "wrong"}).status_code == 401

    def test_wrong_key_not_rescued_by_session(self, signed_in):
        r = signed_in.get("/api/state", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_api_key_write_needs_no_csrf_header(self, anon):
        r = anon.post("/api/config", json={"log_verbose": False},
                      headers={"X-API-Key": _key()})
        assert r.status_code == 200


# ── Sign-in, session, sign-out ───────────────────────────────────────────────

class TestSession:
    def test_wrong_key_rejected_without_session(self, anon):
        r = anon.post("/login", data={"api_key": "wrong"})
        assert r.status_code == 401
        assert anon.get("/api/state").status_code == 401

    def test_empty_key_rejected(self, anon):
        assert anon.post("/login", data={"api_key": ""}).status_code == 401

    def test_sign_in_grants_page_and_api(self, signed_in):
        r = signed_in.get("/", headers=HTML)
        assert r.status_code == 200
        assert b'action="/logout"' in r.data
        assert signed_in.get("/api/state").status_code == 200

    def test_cookie_flags(self, anon):
        r = anon.post("/login", data={"api_key": _key()})
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert _key() not in cookie

    def test_login_redirects_when_already_signed_in(self, signed_in):
        r = signed_in.get("/login")
        assert r.status_code == 302

    def test_write_without_csrf_header_forbidden(self, signed_in):
        r = signed_in.post("/api/config", json={"log_verbose": False})
        assert r.status_code == 403

    def test_write_with_csrf_header_allowed(self, signed_in):
        r = signed_in.post("/api/config", json={"log_verbose": False}, headers=CSRF)
        assert r.status_code == 200

    def test_sign_out(self, signed_in):
        r = signed_in.post("/logout")
        assert r.status_code == 302
        assert signed_in.get("/api/state").status_code == 401

    def test_regenerating_key_signs_everyone_out(self, signed_in):
        r = signed_in.post("/api/settings/regenerate-api-key", headers=CSRF)
        assert r.status_code == 200
        new_key = r.json["key"]
        assert signed_in.get("/api/state").status_code == 401
        assert signed_in.get("/api/state", headers={"X-API-Key": new_key}).status_code == 200


# ── Redirect target validation ───────────────────────────────────────────────

class TestNextParameter:
    @pytest.mark.parametrize("target", ["//evil.example", "https://evil.example/",
                                        "evil", "/\\evil.example"])
    def test_external_targets_ignored(self, anon, target):
        r = anon.post("/login", data={"api_key": _key(), "next": target})
        assert r.status_code == 302
        assert r.headers["Location"] == "/"

    def test_local_target_kept(self, anon):
        r = anon.post("/login", data={"api_key": _key(), "next": "/api/runtime"})
        assert r.headers["Location"] == "/api/runtime"

    def test_page_redirect_carries_next(self, anon):
        r = anon.get("/api/runtime", headers=HTML)
        assert r.status_code == 401  # /api/* never redirects
        r = anon.get("/", headers=HTML)
        assert "next=" in r.headers["Location"]


# ── API_KEY environment variable ─────────────────────────────────────────────

class TestEnvApiKey:
    def test_env_key_takes_precedence(self, anon, monkeypatch):
        stored = _key()
        monkeypatch.setenv("API_KEY", "env-key-123")
        assert anon.get("/api/state", headers={"X-API-Key": "env-key-123"}).status_code == 200
        assert anon.get("/api/state", headers={"X-API-Key": stored}).status_code == 401

    def test_sign_in_with_env_key(self, anon, monkeypatch):
        monkeypatch.setenv("API_KEY", "env-key-123")
        assert anon.post("/login", data={"api_key": "env-key-123"}).status_code == 302
        assert anon.get("/api/state").status_code == 200


# ── HTTP Basic auth ──────────────────────────────────────────────────────────

class TestBasicAuth:
    @pytest.fixture(autouse=True)
    def _basic(self, monkeypatch):
        monkeypatch.setenv("APP_USERNAME", "u")
        monkeypatch.setenv("APP_PASSWORD", "p")

    @staticmethod
    def _hdr(user="u", pwd="p"):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}

    def test_anonymous_gets_basic_challenge(self, anon):
        r = anon.get("/", headers=HTML)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Basic")

    def test_valid_credentials(self, anon):
        r = anon.get("/", headers={**HTML, **self._hdr()})
        assert r.status_code == 200
        assert b'action="/logout"' not in r.data   # no sign-out with Basic

    def test_wrong_credentials(self, anon):
        assert anon.get("/api/state", headers=self._hdr(pwd="x")).status_code == 401

    def test_api_key_still_accepted(self, anon):
        assert anon.get("/api/state", headers={"X-API-Key": _key()}).status_code == 200


# ── Protected config keys ────────────────────────────────────────────────────

class TestSecretsNotExposed:
    @pytest.mark.parametrize("field", ["api_key", "session_secret"])
    def test_cannot_set_via_config(self, anon, field):
        r = anon.post("/api/config", json={field: ""}, headers={"X-API-Key": _key()})
        assert r.status_code == 400
        assert len(_key()) == 64

    def test_session_secret_not_in_state(self, anon):
        cfg = anon.get("/api/state", headers={"X-API-Key": _key()}).json["app_config"]
        assert "session_secret" not in cfg

    def test_session_secret_not_in_config_response(self, anon):
        r = anon.post("/api/config", json={"log_verbose": False},
                      headers={"X-API-Key": _key()})
        assert "session_secret" not in r.json["config"]
