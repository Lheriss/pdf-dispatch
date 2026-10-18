"""
test_ssrf.py — outbound-connection guard (dispatch/netguard.py).

Covers the policy itself (which addresses are public), fail-closed name
resolution, DNS rebinding (an answer that changes between the early check and
the connection), redirects (never followed, checked with two real local HTTP
servers), and every caller: webhook delivery, webhook test route, IMAP
connection test route and the real IMAP polling cycle.

Name resolution is replaced by a fake resolver (netguard._resolve), so no test
depends on DNS or on outbound network access.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import urllib.error
import urllib.request

import app as _app   # noqa: F401  (initialises the app and its config)
from dispatch import config as _config
from dispatch import email_poller, netguard, webhook
from dispatch.netguard import SSRFBlocked


# ── Helpers ───────────────────────────────────────────────────────────────────

def _info(ip: str, port: int = 0):
    """One getaddrinfo() entry for *ip*."""
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


class FakeResolver:
    """Replaces netguard._resolve. *answers* maps host → list of IPs, or a
    callable(call_number) → list of IPs (to simulate DNS rebinding). A host
    that is not in the map fails to resolve."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []

    def __call__(self, host, port):
        self.calls.append(host)
        ans = self.answers.get(host)
        if ans is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if callable(ans):
            ans = ans(len([h for h in self.calls if h == host]))
        return [_info(ip, port) for ip in ans]


@pytest.fixture
def protection(monkeypatch):
    """Turn SSRF_PROTECTION=block on for one test."""
    monkeypatch.setattr(_config, "SSRF_PROTECTION", True)


@pytest.fixture
def no_protection(monkeypatch):
    monkeypatch.setattr(_config, "SSRF_PROTECTION", False)


@pytest.fixture
def resolver(monkeypatch):
    def install(answers):
        fake = FakeResolver(answers)
        monkeypatch.setattr(netguard, "_resolve", fake)
        return fake
    return install


@pytest.fixture(autouse=True)
def _no_env_proxy(monkeypatch):
    """Local test servers must be reached directly, never through a proxy."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)


# ── Address policy ────────────────────────────────────────────────────────────

class TestIsPublicIp:
    @pytest.mark.parametrize("ip", [
        "93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111",
    ])
    def test_public(self, ip):
        assert netguard.is_public_ip(ip)

    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "127.8.9.10", "::1",                 # loopback
        "10.0.0.1", "172.16.5.4", "192.168.1.10",         # RFC 1918
        "fd00::1", "fc12:3456::1",                        # IPv6 ULA
        "169.254.169.254", "fe80::1",                     # link-local / cloud metadata
        "100.64.0.1", "100.127.255.254",                  # CGNAT / Tailscale
        "0.0.0.0", "::",                                  # unspecified
        "224.0.0.1", "ff02::1",                           # multicast
        "240.0.0.1", "255.255.255.255",                   # reserved / broadcast
        "::ffff:127.0.0.1", "::ffff:192.168.1.1",         # IPv4-mapped IPv6
        "::ffff:100.64.0.1",
    ])
    def test_not_public(self, ip):
        assert not netguard.is_public_ip(ip)

    def test_cgnat_boundaries(self):
        assert netguard.is_public_ip("100.63.255.255")
        assert netguard.is_public_ip("100.128.0.0")


# ── check_destination / fail-closed resolution ────────────────────────────────

class TestCheckDestination:
    def test_public_host_allowed(self, protection, resolver):
        resolver({"hooks.example.com": ["93.184.216.34"]})
        assert netguard.check_destination("hooks.example.com", 443) == (True, "")

    @pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.10", "fd00::1",
                                    "100.64.0.1", "0.0.0.0", "::ffff:127.0.0.1"])
    def test_host_resolving_to_non_public_refused(self, protection, resolver, ip):
        resolver({"evil.example.com": [ip]})
        ok, reason = netguard.check_destination("evil.example.com", 80)
        assert not ok
        assert ip in reason

    def test_literal_ip_refused(self, protection):
        # A literal address goes through getaddrinfo too (no DNS involved).
        assert not netguard.check_destination("127.0.0.1", 80)[0]
        assert not netguard.check_destination("::1", 80)[0]

    def test_any_non_public_address_refuses(self, protection, resolver):
        # One public and one private answer: refused (the client could
        # connect to either).
        resolver({"mixed.example.com": ["93.184.216.34", "10.0.0.5"]})
        assert not netguard.check_destination("mixed.example.com", 80)[0]

    def test_ipv6_answer_checked(self, protection, resolver):
        resolver({"v6.example.com": ["93.184.216.34", "fe80::1"]})
        assert not netguard.check_destination("v6.example.com", 80)[0]

    def test_resolution_failure_refused(self, protection, resolver):
        resolver({})
        ok, reason = netguard.check_destination("does-not-exist.invalid", 80)
        assert not ok
        assert "cannot resolve" in reason

    def test_empty_host_refused(self, protection):
        assert not netguard.check_destination("", 80)[0]

    def test_protection_off_allows_everything(self, no_protection, resolver):
        fake = resolver({})
        assert netguard.check_destination("127.0.0.1", 80) == (True, "")
        assert netguard.check_destination("does-not-exist.invalid") == (True, "")
        assert fake.calls == []          # no resolution at all when off


# ── Connection time: DNS rebinding ────────────────────────────────────────────

class _RecordingSocket:
    """Stands in for socket.socket and records the address it connects to."""
    connected = []

    def __init__(self, family, socktype, proto):
        self.family = family

    def settimeout(self, t):
        pass

    def bind(self, addr):
        pass

    def connect(self, sockaddr):
        _RecordingSocket.connected.append(sockaddr)

    def close(self):
        pass


class TestRebinding:
    def test_connection_revalidates_after_early_check(self, protection, resolver):
        # First answer public (passes the early check), every later answer
        # internal: the connection must be refused, not made to 127.0.0.1.
        resolver({"rebind.example.com":
                  lambda n: ["93.184.216.34"] if n == 1 else ["127.0.0.1"]})
        assert netguard.check_destination("rebind.example.com", 80)[0]
        with pytest.raises(SSRFBlocked):
            netguard.guarded_create_connection(("rebind.example.com", 80), 5)

    def test_connects_to_the_checked_address_only(self, protection, resolver,
                                                  monkeypatch):
        # One resolution per connection, and the socket goes to that
        # address — never to a second, unchecked answer.
        fake = resolver({"once.example.com":
                         lambda n: ["93.184.216.34"] if n == 1 else ["127.0.0.1"]})
        _RecordingSocket.connected = []
        monkeypatch.setattr(netguard.socket, "socket", _RecordingSocket)
        netguard.guarded_create_connection(("once.example.com", 8080), 5)
        assert fake.calls == ["once.example.com"]
        assert _RecordingSocket.connected == [("93.184.216.34", 8080)]

    def test_protection_off_uses_standard_connection(self, no_protection,
                                                     monkeypatch):
        seen = []
        monkeypatch.setattr(netguard.socket, "create_connection",
                            lambda addr, timeout=None, src=None: seen.append(addr) or "sock")
        assert netguard.guarded_create_connection(("127.0.0.1", 9), 1) == "sock"
        assert seen == [("127.0.0.1", 9)]


# ── HTTP: redirects (two local servers) ───────────────────────────────────────

class _Target(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        _Target.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def redirect_lab():
    """A target server and a redirector that sends 302 → target."""
    _Target.hits = []
    target = _serve(_Target)
    target_url = f"http://127.0.0.1:{target.server_address[1]}/internal"

    class _Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *a):
            pass

    redirector = _serve(_Redirect)
    yield f"http://127.0.0.1:{redirector.server_address[1]}/hook", target_url
    redirector.shutdown()
    target.shutdown()


class TestRedirects:
    def test_redirect_not_followed(self, no_protection, redirect_lab):
        hook_url, _ = redirect_lab
        req = urllib.request.Request(hook_url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            netguard.guarded_urlopen(req, timeout=5)
        assert exc.value.code == 302
        assert _Target.hits == []          # the redirect target never received it

    def test_target_reachable_directly(self, no_protection, redirect_lab):
        # Sanity check: the lab target works, so the test above is meaningful.
        _, target_url = redirect_lab
        with netguard.guarded_urlopen(urllib.request.Request(target_url), timeout=5) as r:
            assert r.status == 200
        assert _Target.hits == ["/internal"]

    def test_local_endpoint_blocked_when_protected(self, protection, redirect_lab):
        hook_url, _ = redirect_lab
        req = urllib.request.Request(hook_url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.URLError) as exc:
            netguard.guarded_urlopen(req, timeout=5)
        assert isinstance(exc.value.reason, SSRFBlocked)


# ── Callers ───────────────────────────────────────────────────────────────────

class TestWebhookDelivery:
    def test_blocked_without_retry(self, protection, monkeypatch, redirect_lab):
        events = []
        monkeypatch.setattr(webhook, "log_event",
                            lambda lvl, msg, *a, **k: events.append(msg))
        monkeypatch.setattr(webhook.time, "sleep",
                            lambda s: pytest.fail("must not retry a policy refusal"))
        hook_url, _ = redirect_lab
        webhook._deliver_webhook(hook_url, {"event": "x"}, "", "f.pdf")
        assert any("SSRF blocked" in m for m in events)
        assert _Target.hits == []

    def test_redirect_logged_and_not_followed(self, no_protection, monkeypatch,
                                              redirect_lab):
        events = []
        monkeypatch.setattr(webhook, "log_event",
                            lambda lvl, msg, *a, **k: events.append(str(msg)))
        monkeypatch.setattr(webhook.time, "sleep", lambda s: None)
        hook_url, target_url = redirect_lab
        webhook._deliver_webhook(hook_url, {"event": "x"}, "", "f.pdf", attempts=1)
        assert _Target.hits == []
        assert any("not followed" in m and target_url in m for m in events)


class TestWebhookTestRoute:
    def test_blocked_returns_400(self, client, protection, redirect_lab):
        hook_url, _ = redirect_lab
        client.post("/api/config", json={"webhook_url": hook_url})
        try:
            r = client.post("/api/webhook/test", headers={"X-Requested-With": "x"})
            assert r.status_code == 400
            assert "non-public" in r.get_json()["error"]
            assert _Target.hits == []
        finally:
            client.post("/api/config", json={"webhook_url": ""})

    def test_redirect_reported(self, client, no_protection, redirect_lab):
        hook_url, _ = redirect_lab
        client.post("/api/config", json={"webhook_url": hook_url})
        try:
            body = client.post("/api/webhook/test").get_json()
            assert body["ok"] is False and body["code"] == 302
            assert "not followed" in body["error"]
            assert _Target.hits == []
        finally:
            client.post("/api/config", json={"webhook_url": ""})


class _FakeImapServer:
    """Minimal IMAP greeter on 127.0.0.1: proves a connection was made."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.accepted += 1
        conn.settimeout(5)
        conn.sendall(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
        # Some Python versions (3.12) ignore the greeting's response code and
        # send CAPABILITY anyway; answer it, then any other command until EOF.
        try:
            buf = b""
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    tag, _, cmd = line.partition(b" ")
                    if cmd.upper().startswith(b"CAPABILITY"):
                        conn.sendall(b"* CAPABILITY IMAP4rev1\r\n")
                    conn.sendall(tag + b" OK done\r\n")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


class TestImap:
    def test_guarded_imap_blocked(self, protection):
        srv = _FakeImapServer()
        try:
            with pytest.raises(SSRFBlocked):
                netguard.GuardedIMAP4("127.0.0.1", srv.port)
            assert srv.accepted == 0
        finally:
            srv.close()

    def test_guarded_imap_ssl_blocked(self, protection, resolver):
        resolver({"imap.example.com": ["10.1.2.3"]})
        with pytest.raises(SSRFBlocked):
            netguard.GuardedIMAP4_SSL("imap.example.com", 993, timeout=2)

    def test_guarded_imap_connects_when_off(self, no_protection):
        srv = _FakeImapServer()
        try:
            m = netguard.GuardedIMAP4("127.0.0.1", srv.port)
            assert srv.accepted == 1
            m.shutdown()
        finally:
            srv.close()

    def test_email_test_route_blocked(self, client, protection):
        r = client.post("/api/email/test", json={
            "host": "127.0.0.1", "port": 993, "username": "u", "password": "p",
        }, headers={"X-Requested-With": "x"})
        assert r.status_code == 400
        assert "non-public" in r.get_json()["error"]

    def test_real_polling_blocked(self, protection, resolver, monkeypatch):
        # The polling cycle itself (not only the connection test) applies
        # the policy.
        resolver({"imap.internal.example": ["192.168.1.20"]})
        events = []
        monkeypatch.setattr(email_poller, "log_event",
                            lambda lvl, msg, *a, **k: events.append((lvl, str(msg))))
        monkeypatch.setattr(email_poller, "decrypt_password_checked", lambda enc: "pw")
        monkeypatch.setattr(email_poller, "_proc_ids_load", lambda cid: ([], None))
        email_poller._imap_process({
            "id": "t1", "name": "lab", "host": "imap.internal.example",
            "port": 993, "username": "u", "password_enc": "x",
        })
        assert any(lvl == "error" and "SSRF blocked" in msg for lvl, msg in events)


# ── SSRF_ALLOW: explicit exceptions to block mode ─────────────────────────────

@pytest.fixture
def allow(monkeypatch):
    def set_(raw):
        monkeypatch.setattr(_config, "SSRF_ALLOW", raw)
    return set_


class TestAllowlistParsing:
    def test_valid_forms(self):
        entries, invalid = netguard.parse_allowlist(
            "192.168.1.10, 10.0.0.0/8 192.168.1.20:8123 fd00::5 fd00:1::/64 [fd00::6]:993")
        assert invalid == []
        assert [(str(n), p) for n, p in entries] == [
            ("192.168.1.10/32", None), ("10.0.0.0/8", None), ("192.168.1.20/32", 8123),
            ("fd00::5/128", None), ("fd00:1::/64", None), ("fd00::6/128", 993)]

    @pytest.mark.parametrize("bad", [
        "homeassistant.local", "ha.example.com:8123", "192.168.1.300",
        "10.0.0.0/8:80", "192.168.1.10:0", "192.168.1.10:70000", "[fd00::5]x", "1.2.3.4:abc",
    ])
    def test_invalid_entries_ignored(self, bad):
        entries, invalid = netguard.parse_allowlist(f"192.168.1.10 {bad}")
        assert invalid == [bad]
        assert len(entries) == 1                 # the valid entry is kept, nothing widened

    def test_mapped_v6_entry_normalised(self):
        (net, _), = netguard.parse_allowlist("::ffff:192.168.1.10")[0]
        assert str(net) == "192.168.1.10/32"


class TestAllowlistPolicy:
    def test_listed_ip_allowed(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"ha.lan": ["192.168.1.10"]})
        assert netguard.check_destination("ha.lan", 8123)[0]

    def test_unlisted_private_still_refused(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"nas.lan": ["192.168.1.11"]})
        ok, reason = netguard.check_destination("nas.lan", 5000)
        assert not ok and "SSRF_ALLOW" in reason

    def test_cidr(self, protection, resolver, allow):
        allow("192.168.1.0/24")
        resolver({"a.lan": ["192.168.1.77"], "b.lan": ["192.168.2.1"]})
        assert netguard.check_destination("a.lan", 80)[0]
        assert not netguard.check_destination("b.lan", 80)[0]

    def test_port_restriction(self, protection, resolver, allow):
        allow("192.168.1.10:8123")
        resolver({"ha.lan": ["192.168.1.10"]})
        assert netguard.check_destination("ha.lan", 8123)[0]
        assert not netguard.check_destination("ha.lan", 5000)[0]   # other service on the same host

    def test_every_address_must_pass(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"mixed.lan": ["192.168.1.10", "127.0.0.1"]})
        assert not netguard.check_destination("mixed.lan", 80)[0]

    def test_mapped_v6_answer_matches_v4_entry(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"m.lan": ["::ffff:192.168.1.10"]})
        assert netguard.check_destination("m.lan", 80)[0]

    def test_ipv6_entry(self, protection, resolver, allow):
        allow("[fd00::5]:993")
        resolver({"mail.lan": ["fd00::5"]})
        assert netguard.check_destination("mail.lan", 993)[0]
        assert not netguard.check_destination("mail.lan", 143)[0]

    def test_hostname_entry_grants_nothing(self, protection, resolver, allow):
        allow("ha.lan")
        resolver({"ha.lan": ["192.168.1.10"]})
        assert not netguard.check_destination("ha.lan", 8123)[0]

    def test_rebinding_to_unlisted_address_refused(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"ha.lan": lambda n: ["192.168.1.10"] if n == 1 else ["192.168.1.1"]})
        assert netguard.check_destination("ha.lan", 80)[0]
        with pytest.raises(SSRFBlocked):
            netguard.guarded_create_connection(("ha.lan", 80), 5)

    def test_public_addresses_unaffected(self, protection, resolver, allow):
        allow("192.168.1.10")
        resolver({"hooks.example.com": ["93.184.216.34"]})
        assert netguard.check_destination("hooks.example.com", 443)[0]

    def test_problems_reported(self, allow):
        allow("192.168.1.10, homeassistant.local")
        assert netguard.allowlist_problems() == ["homeassistant.local"]


class TestAllowlistCallers:
    def test_webhook_reaches_listed_local_endpoint(self, protection, allow, redirect_lab):
        # With 127.0.0.1 allowed on the redirector's port, the connection is
        # made (the 302 proves it) — and the redirect is still not followed.
        hook_url, _ = redirect_lab
        port = int(hook_url.split(":")[2].split("/")[0])
        allow(f"127.0.0.1:{port}")
        req = urllib.request.Request(hook_url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            netguard.guarded_urlopen(req, timeout=5)
        assert exc.value.code == 302
        assert _Target.hits == []

    def test_webhook_other_port_still_blocked(self, protection, allow, redirect_lab):
        hook_url, _ = redirect_lab
        allow("127.0.0.1:1")
        req = urllib.request.Request(hook_url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.URLError) as exc:
            netguard.guarded_urlopen(req, timeout=5)
        assert isinstance(exc.value.reason, SSRFBlocked)

    def test_imap_listed_server_connects(self, protection, allow):
        srv = _FakeImapServer()
        try:
            allow(f"127.0.0.1:{srv.port}")
            m = netguard.GuardedIMAP4("127.0.0.1", srv.port)
            assert srv.accepted == 1
            m.shutdown()
        finally:
            srv.close()

    def test_runtime_exposes_allowlist(self, client, allow, monkeypatch):
        from dispatch.routes import docs
        monkeypatch.setattr(docs, "SSRF_ALLOW", "192.168.1.10:8123")
        assert client.get("/api/runtime").get_json()["ssrf_allow"] == "192.168.1.10:8123"


class TestAllowlistThroughRoutes:
    """Through the real entry points, which run an early check before the
    connection: a port-restricted entry must match there too."""

    def test_url_host_port(self):
        assert webhook._url_host_port("http://ha.lan:8123/api/webhook/x") == ("ha.lan", 8123)
        assert webhook._url_host_port("https://hooks.example.com/x") == ("hooks.example.com", 443)
        assert webhook._url_host_port("http://[fd00::5]/x") == ("fd00::5", 80)

    def test_webhook_test_route_port_restricted_entry(self, client, protection, allow,
                                                      redirect_lab):
        hook_url, _ = redirect_lab
        port = int(hook_url.split(":")[2].split("/")[0])
        allow(f"127.0.0.1:{port}")
        client.post("/api/config", json={"webhook_url": hook_url})
        try:
            body = client.post("/api/webhook/test").get_json()
            assert body.get("code") == 302, body          # reached, redirect not followed
            assert _Target.hits == []
        finally:
            client.post("/api/config", json={"webhook_url": ""})

    def test_delivery_port_restricted_entry(self, protection, allow, monkeypatch, redirect_lab):
        hook_url, _ = redirect_lab
        port = int(hook_url.split(":")[2].split("/")[0])
        allow(f"127.0.0.1:{port}")
        events = []
        monkeypatch.setattr(webhook, "log_event",
                            lambda lvl, msg, *a, **k: events.append(str(msg)))
        monkeypatch.setattr(webhook.time, "sleep", lambda s: None)
        webhook._deliver_webhook(hook_url, {"event": "x"}, "", "f.pdf", attempts=1)
        assert not any("SSRF blocked" in m for m in events), events
        assert any("not followed" in m for m in events)

    def test_email_test_route_port_restricted_entry(self, client, protection, allow):
        srv = _FakeImapServer()
        try:
            allow(f"127.0.0.1:{srv.port}")
            r = client.post("/api/email/test", json={
                "host": "127.0.0.1", "port": srv.port, "username": "u", "password": "p",
                "use_ssl": False}, headers={"X-Requested-With": "x"})
            assert r.status_code != 400, r.get_json()      # not an SSRF refusal
            assert srv.accepted == 1
        finally:
            srv.close()
