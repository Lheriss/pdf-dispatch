"""
dispatch/netguard.py
--------------------
Outbound-connection guard against SSRF (server-side request forgery).

Every outbound connection pdf-dispatch makes on behalf of the configuration
— webhook delivery, webhook test, IMAP polling, IMAP connection test — goes
through guarded_create_connection(). When SSRF_PROTECTION=block:

  • the host name is resolved to ALL its addresses (IPv4 and IPv6);
  • resolution failure is a refusal (fail-closed);
  • any non-public address is a refusal: private, loopback, link-local,
    multicast, reserved, unspecified (0.0.0.0, ::), shared address space
    100.64.0.0/10 (CGNAT / Tailscale), and IPv4-mapped IPv6 forms of those;
  • the socket is connected to one of the addresses just checked — never
    re-resolved — so a DNS answer that changes between check and connect
    (DNS rebinding) cannot redirect the connection to an internal host;
  • HTTP redirects are never followed (see guarded_urlopen), so a public
    endpoint cannot bounce the request to an internal URL.

When SSRF_PROTECTION is off (default — webhooks to a local Home Assistant
are a common, legitimate use), connections behave exactly like the standard
library's, except that webhook redirects are still not followed.

The flag is read from dispatch.config at call time so tests can toggle it.
"""

import http.client
import imaplib
import ipaddress
import socket
import urllib.request

from dispatch import config as _config

_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class SSRFBlocked(OSError):
    """Raised when an outbound connection targets a forbidden destination."""


def protection_enabled() -> bool:
    return bool(_config.SSRF_PROTECTION)


def is_public_ip(ip) -> bool:
    """True only for globally routable unicast addresses."""
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in _SHARED_ADDRESS_SPACE:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _resolve(host: str, port: int) -> list:
    """All (family, type, proto, canonname, sockaddr) entries for host:port."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _checked_addresses(host: str, port: int) -> list:
    """Resolve ONCE and validate every address; return the address list.

    Raises SSRFBlocked on resolution failure or any non-public address.
    The caller must connect only to the returned addresses (no second
    resolution), which is what defeats DNS rebinding.
    """
    if not host:
        raise SSRFBlocked("empty host")
    try:
        infos = _resolve(host, port or 0)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SSRFBlocked(f"cannot resolve {host!r} ({exc})") from None
    if not infos:
        raise SSRFBlocked(f"cannot resolve {host!r}")
    for *_, sockaddr in infos:
        if not is_public_ip(sockaddr[0]):
            raise SSRFBlocked(f"{host!r} resolves to non-public address {sockaddr[0]}")
    return infos


def check_destination(host: str, port: int = 0) -> tuple[bool, str]:
    """Validate host:port under the current policy without connecting.

    Returns (allowed, reason). Always allowed when protection is off. Used for
    an early, explicit refusal; the binding check happens again at connect
    time in guarded_create_connection().
    """
    if not protection_enabled():
        return True, ""
    try:
        _checked_addresses(host, port)
    except SSRFBlocked as exc:
        return False, str(exc)
    return True, ""


def guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, **_kw):
    """Drop-in replacement for socket.create_connection().

    With protection on: resolve once, refuse if ANY address is non-public,
    then connect to those checked addresses only.
    """
    if not protection_enabled():
        return socket.create_connection(address, timeout, source_address)

    host, port = address
    last_exc = None
    for family, socktype, proto, _canon, sockaddr in _checked_addresses(host, port):
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_exc = exc
            if sock is not None:
                sock.close()
    raise last_exc or OSError(f"could not connect to {host!r}")


# ── HTTP (webhooks) ──────────────────────────────────────────────────────────

class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib then raises HTTPError for the 3xx."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def guarded_urlopen(request, timeout=10):
    """urllib.request.urlopen() with the SSRF guard and no redirects.

    With protection on, environment HTTP(S) proxies are ignored: the guard
    validates the address the socket really connects to, which would be the
    proxy rather than the destination.
    """
    handlers = [_GuardedHTTPHandler(), _GuardedHTTPSHandler(), _NoRedirectHandler()]
    if protection_enabled():
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


# ── IMAP (email polling and connection test) ─────────────────────────────────

class GuardedIMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        address = (self.host or None, self.port)
        if timeout is not None:
            return guarded_create_connection(address, timeout)
        return guarded_create_connection(address)


class GuardedIMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = GuardedIMAP4._create_socket(self, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
