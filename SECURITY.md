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
- SSRF via the webhook URL or the IMAP server fields
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

- [ ] First-run setup completed: administrator account created with a
      strong password (the one-time setup code is printed in the container
      log until then — restrict access to the logs), or `APP_USERNAME` /
      `APP_PASSWORD` set
- [ ] `EMAIL_SECRET` generated with `openssl rand -hex 32` and stored
      securely — never change it after setting up email accounts
      (required for email polling: without it, IMAP passwords are refused)
- [ ] API key kept secret — it is auto-generated; regenerate it from the
      *API access* panel if it leaks (or manage it yourself with `API_KEY`)
- [ ] HTTPS termination at the reverse proxy if exposed beyond a trusted
      network (the sign-in form, session cookie and API key travel in clear
      otherwise), with `SESSION_COOKIE_SECURE=true`
- [ ] `MEM_LIMIT` and `MAX_PAGES` set conservatively for untrusted input
- [ ] `SSRF_PROTECTION=block`, with the local services webhooks or IMAP must
      reach listed in `SSRF_ALLOW` (see the README, *Outbound connections*)
- [ ] Docker container not running as root (verified by default via
      `gosu` in `entrypoint.sh`)
- [ ] Container not on a `--network host` bridge with sensitive services
- [ ] `/healthz` monitored (it answers 503 when an internal check fails)
