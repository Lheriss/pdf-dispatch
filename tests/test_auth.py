"""
test_auth.py — authentication matrix (routes/auth.py, dispatch/credentials.py).

Covers first-run setup (one-time code), sign-in with the administrator
account, sessions and their invalidation, CSRF protection of
session-authenticated writes, the API key (machine credential), HTTP Basic
headers, APP_USERNAME/APP_PASSWORD, password change, open-redirect
protection, and the config keys that must never be readable or settable.

Uses anonymous clients (no automatic X-API-Key), unlike the `client`
fixture from conftest.py.
"""

import base64

import pytest
from flask.testing import FlaskClient

import app as _app
from dispatch import credentials as creds

CSRF = {"X-Requested-With": "pdf-dispatch"}
HTML = {"Accept": "text/html"}
USER, PWD = "admin", "correct-horse"


def _key() -> str:
    return _app.get_config().get("api_key", "")


def _reset_to_setup() -> str:
    """Remove the stored account and return a fresh one-time setup code."""
    _app.update_config({"admin_username": "", "admin_password_hash": ""})
    creds.ensure_setup_token()
    return creds._setup_token


def _setup_form(code, user=USER, pwd=PWD, confirm=None):
    return {"setup_code": code, "username": user, "password": pwd,
            "password_confirm": pwd if confirm is None else confirm}


def _basic(user=USER, pwd=PWD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("API_KEY", "APP_USERNAME", "APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def admin():
    """Guarantee the stored administrator account USER/PWD exists."""
    if not creds.verify_credentials(USER, PWD):
        assert creds.complete_setup(_reset_to_setup(), USER, PWD) is None
    return USER, PWD


@pytest.fixture
def anon():
    """Fresh anonymous client with its own cookie jar."""
    return FlaskClient(_app.app, _app.app.response_class, use_cookies=True)


@pytest.fixture
def signed_in(admin, anon):
    r = anon.post("/login", data={"username": USER, "password": PWD})
    assert r.status_code == 302
    return anon


# ── First-run setup ──────────────────────────────────────────────────────────

class TestSetup:
    @pytest.fixture
    def code(self):
        return _reset_to_setup()

    def test_pages_redirect_to_setup(self, code, anon):
        r = anon.get("/", headers=HTML)
        assert r.status_code == 302 and r.headers["Location"].endswith("/setup")
        assert anon.get("/login").headers["Location"].endswith("/setup")

    def test_api_rejected_during_setup(self, code, anon):
        assert anon.get("/api/state").status_code == 401

    def test_api_key_works_during_setup(self, code, anon):
        assert anon.get("/api/state", headers={"X-API-Key": _key()}).status_code == 200

    def test_setup_page_renders(self, code, anon):
        r = anon.get("/setup")
        assert r.status_code == 200 and b'name="setup_code"' in r.data

    def test_wrong_code(self, code, anon):
        assert anon.post("/setup", data=_setup_form("wrong")).status_code == 401
        assert creds.credentials_source() is None

    def test_short_password(self, code, anon):
        assert anon.post("/setup", data=_setup_form(code, pwd="short")).status_code == 400

    def test_password_mismatch(self, code, anon):
        assert anon.post("/setup", data=_setup_form(code, confirm="different1")).status_code == 400

    def test_empty_username(self, code, anon):
        assert anon.post("/setup", data=_setup_form(code, user="  ")).status_code == 400

    def test_success_signs_in_and_consumes_code(self, code, anon):
        r = anon.post("/setup", data=_setup_form(code))
        assert r.status_code == 302 and r.headers["Location"] == "/"
        assert anon.get("/api/state").status_code == 200
        assert creds.credentials_source() == "stored"
        # Code is single-use and setup is closed
        other = FlaskClient(_app.app, _app.app.response_class)
        r = other.post("/setup", data=_setup_form(code, user="intruder"))
        assert r.headers["Location"].endswith("/login")
        assert creds.admin_username() == USER

    def test_password_stored_hashed(self, code, anon):
        anon.post("/setup", data=_setup_form(code))
        cfg = _app.get_config()
        assert PWD not in cfg["admin_password_hash"]
        assert cfg["admin_password_hash"].startswith("scrypt:")

    def test_no_setup_with_env_credentials(self, code, anon, monkeypatch):
        monkeypatch.setenv("APP_USERNAME", "envuser")
        monkeypatch.setenv("APP_PASSWORD", "envpassword")
        assert anon.get("/setup").headers["Location"].endswith("/login")


# ── Anonymous access (account exists) ────────────────────────────────────────

class TestAnonymous:
    def test_healthz_is_public(self, admin, anon):
        assert anon.get("/healthz").status_code == 200

    @pytest.mark.parametrize("path", ["/api/openapi.json", "/api/openapi.yaml", "/api/docs"])
    def test_api_description_public(self, admin, anon, path):
        assert anon.get(path).status_code == 200

    def test_static_assets_public(self, admin, anon):
        assert anon.get("/static/css/style.css").status_code == 200

    def test_page_redirects_to_login_with_next(self, admin, anon):
        r = anon.get("/", headers=HTML)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"] and "next=" in r.headers["Location"]

    def test_api_returns_json_401_without_challenge(self, admin, anon):
        r = anon.get("/api/runtime", headers=HTML)   # /api/* never redirects
        assert r.status_code == 401
        assert r.json["ok"] is False
        assert "WWW-Authenticate" not in r.headers

    def test_api_write_rejected(self, admin, anon):
        r = anon.post("/api/config", json={"log_verbose": False}, headers=CSRF)
        assert r.status_code == 401


# ── Sign-in, session, sign-out ───────────────────────────────────────────────

class TestSession:
    def test_login_page_renders(self, admin, anon):
        r = anon.get("/login")
        assert r.status_code == 200 and b'name="password"' in r.data

    def test_wrong_password(self, admin, anon):
        r = anon.post("/login", data={"username": USER, "password": "wrong-password"})
        assert r.status_code == 401
        assert anon.get("/api/state").status_code == 401

    def test_wrong_username(self, admin, anon):
        r = anon.post("/login", data={"username": "root", "password": PWD})
        assert r.status_code == 401

    def test_api_key_is_not_a_password(self, admin, anon):
        r = anon.post("/login", data={"username": USER, "password": _key()})
        assert r.status_code == 401

    def test_sign_in_grants_page_and_api(self, signed_in):
        r = signed_in.get("/", headers=HTML)
        assert r.status_code == 200
        assert b'action="/logout"' in r.data
        assert b'id="pw-current"' in r.data
        assert signed_in.get("/api/state").status_code == 200

    def test_cookie_flags(self, admin, anon):
        r = anon.post("/login", data={"username": USER, "password": PWD})
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        assert PWD not in cookie and _key() not in cookie

    def test_login_redirects_when_already_signed_in(self, signed_in):
        assert signed_in.get("/login").status_code == 302

    def test_write_without_csrf_header_forbidden(self, signed_in):
        r = signed_in.post("/api/config", json={"log_verbose": False})
        assert r.status_code == 403

    def test_write_with_csrf_header_allowed(self, signed_in):
        r = signed_in.post("/api/config", json={"log_verbose": False}, headers=CSRF)
        assert r.status_code == 200

    def test_sign_out(self, signed_in):
        assert signed_in.post("/logout").status_code == 302
        assert signed_in.get("/api/state").status_code == 401

    def test_regenerating_api_key_keeps_sessions(self, signed_in):
        r = signed_in.post("/api/settings/regenerate-api-key", headers=CSRF)
        assert r.status_code == 200
        assert signed_in.get("/api/state").status_code == 200


# ── Password change ──────────────────────────────────────────────────────────

class TestPasswordChange:
    NEW = "brand-new-password"

    @pytest.fixture(autouse=True)
    def _restore(self, admin):
        yield
        if not creds.verify_credentials(USER, PWD):
            creds.set_stored_credentials(USER, PWD)

    def _change(self, c, current, new):
        return c.post("/api/settings/password", headers=CSRF,
                      json={"current_password": current, "new_password": new})

    def test_wrong_current_password(self, signed_in):
        r = self._change(signed_in, "nope", self.NEW)
        assert r.status_code == 400 and r.json["error_key"] == "auth.error_current_password"

    def test_new_password_too_short(self, signed_in):
        r = self._change(signed_in, PWD, "short")
        assert r.status_code == 400 and r.json["error_key"] == "auth.error_password_short"

    def test_success_signs_out_other_browsers_only(self, signed_in, anon):
        other = FlaskClient(_app.app, _app.app.response_class, use_cookies=True)
        other.post("/login", data={"username": USER, "password": PWD})
        assert other.get("/api/state").status_code == 200
        assert self._change(signed_in, PWD, self.NEW).status_code == 200
        assert signed_in.get("/api/state").status_code == 200      # caller stays in
        assert other.get("/api/state").status_code == 401          # others signed out
        assert creds.verify_credentials(USER, self.NEW)

    def test_requires_authentication(self, admin, anon):
        assert self._change(anon, PWD, self.NEW).status_code == 401

    def test_refused_with_env_credentials(self, anon, monkeypatch):
        monkeypatch.setenv("APP_USERNAME", "envuser")
        monkeypatch.setenv("APP_PASSWORD", "envpassword")
        r = anon.post("/api/settings/password", headers=_basic("envuser", "envpassword"),
                      json={"current_password": "envpassword", "new_password": self.NEW})
        assert r.status_code == 400 and r.json["error_key"] == "auth.error_env_credentials"


# ── API key (machine credential) ─────────────────────────────────────────────

class TestApiKey:
    def test_valid_key(self, admin, anon):
        assert anon.get("/api/state", headers={"X-API-Key": _key()}).status_code == 200

    def test_wrong_key(self, admin, anon):
        assert anon.get("/api/state", headers={"X-API-Key": "wrong"}).status_code == 401

    def test_wrong_key_not_rescued_by_session(self, signed_in):
        assert signed_in.get("/api/state", headers={"X-API-Key": "wrong"}).status_code == 401

    def test_key_write_needs_no_csrf_header(self, admin, anon):
        r = anon.post("/api/config", json={"log_verbose": False}, headers={"X-API-Key": _key()})
        assert r.status_code == 200

    def test_env_key_takes_precedence(self, admin, anon, monkeypatch):
        stored = _key()
        monkeypatch.setenv("API_KEY", "env-key-123")
        assert anon.get("/api/state", headers={"X-API-Key": "env-key-123"}).status_code == 200
        assert anon.get("/api/state", headers={"X-API-Key": stored}).status_code == 401


# ── HTTP Basic header and APP_USERNAME / APP_PASSWORD ────────────────────────

class TestBasicAndEnvCredentials:
    def test_basic_header_with_stored_account(self, admin, anon):
        assert anon.get("/api/state", headers=_basic()).status_code == 200

    def test_basic_header_wrong_password(self, admin, anon):
        r = anon.get("/api/state", headers=_basic(pwd="wrong-password"))
        assert r.status_code == 401 and "WWW-Authenticate" not in r.headers

    @pytest.fixture
    def env_creds(self, admin, monkeypatch):
        monkeypatch.setenv("APP_USERNAME", "envuser")
        monkeypatch.setenv("APP_PASSWORD", "envpassword")

    def test_env_credentials_sign_in(self, env_creds, anon):
        assert anon.post("/login", data={"username": "envuser", "password": "envpassword"}).status_code == 302
        assert anon.get("/api/state").status_code == 200
        r = anon.get("/", headers=HTML)
        assert b'action="/logout"' in r.data and b'id="pw-current"' not in r.data

    def test_env_credentials_override_stored(self, env_creds, anon):
        assert anon.post("/login", data={"username": USER, "password": PWD}).status_code == 401

    def test_env_basic_header(self, env_creds, anon):
        assert anon.get("/api/state", headers=_basic("envuser", "envpassword")).status_code == 200

    def test_changing_env_password_ends_sessions(self, env_creds, anon, monkeypatch):
        anon.post("/login", data={"username": "envuser", "password": "envpassword"})
        monkeypatch.setenv("APP_PASSWORD", "rotated-password")
        assert anon.get("/api/state").status_code == 401


# ── Redirect target validation ───────────────────────────────────────────────

class TestNextParameter:
    @pytest.mark.parametrize("target", ["//evil.example", "https://evil.example/",
                                        "evil", "/\\evil.example"])
    def test_external_targets_ignored(self, admin, anon, target):
        r = anon.post("/login", data={"username": USER, "password": PWD, "next": target})
        assert r.status_code == 302 and r.headers["Location"] == "/"

    def test_local_target_kept(self, admin, anon):
        r = anon.post("/login", data={"username": USER, "password": PWD, "next": "/api/runtime"})
        assert r.headers["Location"] == "/api/runtime"


# ── Secrets never exposed or settable ────────────────────────────────────────

class TestSecretsNotExposed:
    @pytest.mark.parametrize("field", ["api_key", "session_secret",
                                       "admin_username", "admin_password_hash"])
    def test_cannot_set_via_config(self, admin, anon, field):
        r = anon.post("/api/config", json={field: ""}, headers={"X-API-Key": _key()})
        assert r.status_code == 400
        assert creds.verify_credentials(USER, PWD) and len(_key()) == 64

    @pytest.mark.parametrize("field", ["session_secret", "admin_password_hash"])
    def test_not_in_state(self, admin, anon, field):
        cfg = anon.get("/api/state", headers={"X-API-Key": _key()}).json["app_config"]
        assert field not in cfg

    @pytest.mark.parametrize("field", ["session_secret", "admin_password_hash"])
    def test_not_in_config_response(self, admin, anon, field):
        r = anon.post("/api/config", json={"log_verbose": False}, headers={"X-API-Key": _key()})
        assert field not in r.json["config"]


# ── Session cookie name: one per instance ────────────────────────────────────

class TestCookieName:
    """Browsers scope cookies by host, not by port: two instances on one host
    (production :5880, test stack :5881) must not share a cookie name, or
    signing in to one signs you out of the other."""

    def test_name_unique_per_instance(self, monkeypatch):
        from dispatch import session_cookie_name
        monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
        a, b = session_cookie_name("a" * 64), session_cookie_name("b" * 64)
        assert a.startswith("pdf_dispatch_session_") and b.startswith("pdf_dispatch_session_")
        assert a != b
        assert session_cookie_name("a" * 64) == a          # stable across restarts

    def test_name_reveals_no_secret(self, monkeypatch):
        from dispatch import session_cookie_name
        monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
        secret = "0123456789abcdef" * 4
        assert secret[:8] not in session_cookie_name(secret)

    def test_override(self, monkeypatch):
        from dispatch import session_cookie_name
        monkeypatch.setenv("SESSION_COOKIE_NAME", "pdfd_prod")
        assert session_cookie_name("x") == "pdfd_prod"

    def test_invalid_override_ignored(self, monkeypatch):
        from dispatch import session_cookie_name
        monkeypatch.setenv("SESSION_COOKIE_NAME", "bad name;Path=/")
        assert session_cookie_name("x").startswith("pdf_dispatch_session_")

    def test_app_uses_instance_name(self, admin, anon):
        from dispatch import session_cookie_name
        name = _app.app.config["SESSION_COOKIE_NAME"]
        assert name == session_cookie_name(_app.app.secret_key)
        r = anon.post("/login", data={"username": USER, "password": PWD})
        assert r.headers["Set-Cookie"].startswith(name + "=")

    def test_old_generic_cookie_ignored(self, admin):
        # A leftover cookie under the pre-1.8.3 name grants nothing.
        c = FlaskClient(_app.app, _app.app.response_class, use_cookies=True)
        c.set_cookie("pdf_dispatch_session", "anything")
        assert c.get("/api/state").status_code == 401
