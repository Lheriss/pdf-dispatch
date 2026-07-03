# Security Policy

## Supported versions

Only the latest release of pdf-dispatch receives security fixes.
Older versions are not actively maintained.

| Version | Supported |
|---------|-----------|
| Latest  | ✅ |
| Older   | ❌ |

---

## Scope

pdf-dispatch is designed to run on a **private, trusted network** (home or
small-office self-hosting). It is not intended to be exposed directly to the
public internet without a hardened reverse proxy and authentication layer.

In-scope vulnerabilities include:

- Authentication bypass on the web interface or API
- Remote code execution via crafted PDF or configuration input
- Path traversal allowing access to files outside `/data`
- SSRF via the webhook URL field
- Privilege escalation inside the Docker container

Out-of-scope:

- Denial-of-service via resource exhaustion on files that exceed the
  configured limits (`MAX_UPLOAD_MB`, `MAX_PAGES`, `MEM_LIMIT`) — these
  are documented operational limits, not vulnerabilities
- Attacks that require physical access to the host
- Vulnerabilities in third-party dependencies (report upstream directly,
  then open an issue here so a dependency update can be scheduled)

---

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report them privately via
[GitHub Security Advisories](../../security/advisories/new).

Include:

1. A description of the vulnerability
2. Steps to reproduce (a minimal example or proof of concept)
3. The potential impact and affected versions
4. Any suggested mitigation or fix

You will receive an acknowledgement within **72 hours**. Fixes are
typically issued within 14 days of a confirmed report. You will be
credited in the release notes unless you prefer to remain anonymous.

---

## Security hardening checklist

Before exposing pdf-dispatch to any network beyond localhost, review the
following items (all documented in the [README](README.md)):

- [ ] `APP_USERNAME` and `APP_PASSWORD` set, or a reverse proxy handling
      authentication (Authelia, Authentik, Nginx basic auth, …)
- [ ] `EMAIL_SECRET` generated with `openssl rand -hex 32` and stored
      securely — never change it after setting up email accounts
- [ ] `API_KEY` set if automated API access is required
- [ ] HTTPS termination at the reverse proxy (HTTP Basic auth transmits
      credentials in base64 — plaintext equivalent without TLS)
- [ ] `MEM_LIMIT` and `MAX_PAGES` set conservatively for untrusted input
- [ ] `SSRF_PROTECTION=block` if the webhook feature is enabled
- [ ] Docker container not running as root (verified by default via
      `gosu` in `entrypoint.sh`)
- [ ] Container not on a `--network host` bridge with sensitive services
