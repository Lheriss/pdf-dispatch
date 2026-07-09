# Contributing to pdf-dispatch

Thank you for your interest in contributing! This document explains how to
get involved, from reporting a bug to submitting a pull request.

---

## Table of contents

1. [Code of conduct](#code-of-conduct)
2. [Reporting bugs](#reporting-bugs)
3. [Suggesting features](#suggesting-features)
4. [Development setup](#development-setup)
5. [Submitting a pull request](#submitting-a-pull-request)
6. [Coding conventions](#coding-conventions)

---

## Code of conduct

Be respectful and constructive. Issues and pull requests that contain
harassment or personal attacks will be closed without comment.

---

## Reporting bugs

Before opening an issue, please:

1. Check the [existing issues](../../issues) to avoid duplicates.
2. Collect the relevant information:
   - pdf-dispatch version (shown in `/api/runtime` → `APP_VERSION`)
   - Docker host OS and kernel version (`uname -r`)
   - Relevant lines from the container log (`docker logs pdf-dispatch`)
   - Steps to reproduce reliably

Open a [Bug report](../../issues/new?template=bug_report.md) and fill in
the template. **Do not include real PDFs or personal data** in the report.

---

## Suggesting features

Open a [Feature request](../../issues/new?template=feature_request.md)
describing:

- The problem you are trying to solve
- Your proposed solution or the behaviour you expect
- Alternatives you have considered

Feature requests are evaluated against the project's scope (self-hosted PDF
splitting by barcode/QR code via a minimal web interface). Requests that
introduce external service dependencies or complex infrastructure are
unlikely to be accepted.

---

## Development setup

### Prerequisites

- Docker and Docker Compose
- Python ≥ 3.11
- Node.js ≥ 18 (for JS tests only)
- `pip install -r splitter/requirements.txt`

### Running the tests

```bash
# Python unit tests (no server needed)
python3 tests/test_python_core.py

# API and webhook integration tests (Flask test client)
pytest tests/test_api.py tests/test_webhook.py -v

# i18n key consistency
python3 tests/test_i18n_keys.py

# JS functional tests
node tests/test_js_functional.js
```

### Running a local instance

```bash
cd splitter
DATA_DIR=/tmp/pdf-dispatch-dev EMAIL_SECRET=$(openssl rand -hex 32) \
  python3 app.py
```

Browse to `http://localhost:5000`.

---

## Submitting a pull request

1. **Fork** the repository and create a branch from `dev` (development
   happens on `dev`; `main` is the stable branch and is updated only via a
   `dev → main` pull request):
   ```bash
   git checkout -b fix/my-bug-fix
   ```
2. Make your changes. Keep each PR focused on a single concern.
3. Run the full test suite (see above) and ensure it is green.
4. Check that all source comments and docstrings are in **English**.
5. Open a pull request against `main`. Reference the issue it addresses
   (`Closes #123`).
6. The CI must be green before merge. A maintainer will review within a
   reasonable time.

### Commit message style

Follow the [Conventional Commits](https://www.conventionalcommits.org/)
format:

```
<type>(<scope>): <short summary>

<optional body>
```

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`.

Examples:
```
fix(email_poller): handle messages with no Message-ID header
feat(config): add MAX_PAGES per-trigger override
docs(README): document MEM_LIMIT tuning
```

---

## Coding conventions

| Convention | Detail |
|------------|--------|
| Language | Python 3.11+, no type annotations required |
| Formatting | PEP 8; lines ≤ 100 chars |
| Comments | English only |
| Docstrings | First line: imperative sentence. Body: plain English paragraphs. |
| No build step | The frontend (`static/js/app.js`) must remain a single vanilla JS file — no bundler, no npm dependency |
| i18n | User-visible strings go in `splitter/i18n/fr.json` and `splitter/i18n/en.json` — never hard-coded in templates |
| Tests | New behaviour should come with a test in `tests/test_python_core.py` or `tests/test_api.py` |

---

## Keeping documentation in sync

Every functional change must update **all** the documentation it affects, in
the same commit cycle — never as an afterthought. Before opening a
`dev → main` PR, check each of these fronts:

1. **README — user-facing** — Features list, configuration, UI panels.
2. **README — Security section** — anything touching exposure,
   authentication, resource exhaustion (RAM / CPU / disk), input validation,
   or permissions.
3. **OpenAPI spec** — for any added / changed / removed endpoint or
   request/response schema change: edit `splitter/openapi.yaml` (the source),
   then regenerate `splitter/openapi.json` (see [Updating
   `openapi.json`](README.md#updating-openapijson)) and validate it.
4. **`docker-compose.yml`** — every new environment variable (commented out
   if the feature is opt-in / disabled by default).
5. **README — Development section** — project tree, module map
   (`dispatch/` + `routes/`), the touched modules' responsibilities, the
   frontend render-function list, and the CI/CD section (triggers,
   multi-arch, tag strategy).

Prefer stable wording over frozen counts: for example, state that EN/FR
parity is verified by `check_keys.py` rather than quoting a key count that
goes stale on the next string added.

### Branch & release flow

- Develop on `dev`; validate the full test suite (the separate
  `pdf-dispatch-tester` repo) against the `:dev` image.
- Promote with a `dev → main` pull request. CI builds `:latest` on merge.
- Tag a release **after** the merge, on the resulting `main` HEAD, so the
  version is clean (no commit-suffix) and the `:vX.Y.Z` image is produced by
  the tag workflow.
