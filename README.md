# pdf-dispatch

Self-hosted Docker service that splits multi-page PDFs on detection of barcodes or QR codes. A web interface lets you configure everything without restarting the container.

## Features

- **Automatic splitting** — monitors a folder; every PDF dropped in is processed immediately
- **Three input sources** — watched folder, web interface (drag-and-drop or API upload), IMAP email
- **Flexible trigger matching** — exact codes, glob patterns (`INVOICE*`, `[A-Z][0-9]`), case-insensitive
- **Configurable filename construction** — tokens (trigger, date, counter, free text) drag-and-drop reordered
- **Outbound notifications** — HTTP webhook and/or post-processing shell script after each file
- **REST API** — full programmatic control; OpenAPI 3.1 spec + Swagger UI built in
- **Bilingual interface** — French and English

---

## How it works

```
[Watched folder /data/input/]  ─┐
[Web interface / API upload]   ─┼─→ [pdf-dispatch] → /data/output/             (split documents)
[IMAP email attachments]       ─┘                  → /data/output/no_code/      (no trigger found)
                                                   → /data/output/error/        (invalid files)
                                                   → /data/output/processed/    (archived sources)
                                                   → /data/output/<trigger>/    (if subfolders on)
```

After each file: optional **post-processing script** and/or **outbound webhook** — see the Advanced sections.

---

## 1. Deployment — Quick guide

### Prerequisites

- Docker + Docker Compose (or Portainer)
- A data folder on the host, readable/writable by the container user

```bash
# Create the data folder (adapt the path to your setup)
mkdir -p /your/data/path
```

> ⚠️ **Synology NAS** — if permissions revert to `d---------+` after `chmod`, set them via DSM → Control Panel → Shared Folder → Permissions → Read/Write, then apply to child folders.

### docker-compose.yml

```yaml
services:
  pdf-dispatch:
    image: ghcr.io/lheriss/pdf-dispatch:latest
    container_name: pdf-dispatch
    restart: unless-stopped

    environment:
      DATA_DIR: /data

      # ⚠️ Adapt to your system — run 'id <youruser>' to find these values
      PUID: "${PUID:-1000}"
      PGID: "${PGID:-1000}"

      # ⚠️ Required if you use email retrieval — generate once and never change
      # Generate with: openssl rand -hex 32
      EMAIL_SECRET: "${EMAIL_SECRET:-}"

      TZ: "${TZ:-Europe/Zurich}"

      # Optional — see Environment variables section for the full list
      # APP_USERNAME: "${APP_USERNAME:-}"
      # APP_PASSWORD: "${APP_PASSWORD:-}"
      # BARCODE_DPI: "300"

    volumes:
      # ⚠️ Replace DATA_VOLUME with your actual data folder path
      # Example: DATA_VOLUME=/opt/pdf-dispatch/data
      - "${DATA_VOLUME:-/data}:/data"
      # Writable scratch space for PDF page rendering — keeps the rest
      # of the container filesystem effectively read-only.
      - type: tmpfs
        target: /tmp
        tmpfs:
          size: "${TMP_SIZE:-512m}"

    ports:
      - "${PORT:-5880}:5000"

    # ── Resource limits ────────────────────────────────────────────────────
    # A single malicious or oversized PDF can exhaust all available RAM
    # during barcode rasterisation (300 DPI, ~26 MB per A4 page).
    # These limits contain the damage to this container only.
    mem_limit: "${MEM_LIMIT:-2g}"
    memswap_limit: "${MEM_LIMIT:-2g}"   # = mem_limit → swap disabled
    cpus: "${CPU_LIMIT:-2.0}"
    pids_limit: "${PIDS_LIMIT:-256}"

    # ── Hardening ─────────────────────────────────────────────────────────
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    cap_add:
      - CHOWN      # entrypoint.sh: chown /data to PUID/PGID
      - SETUID     # gosu: drop from root to PUID after setup
      - SETGID
      - DAC_OVERRIDE
      - FOWNER
    # Note: the container is NOT read_only because entrypoint.sh writes to
    # /etc/passwd and /etc/group during PUID/PGID setup (before gosu drops
    # privileges). The application itself only writes to /data and /tmp.
```

### ⚠️ Critical first-time configuration

These three items **must** be adapted before first launch — the defaults will not work for your system:

| Item | Why | How |
|------|-----|-----|
| `PUID` / `PGID` | Files must be owned by your user, not root | Run `id <youruser>` on the host |
| Volume path | Points to your actual data folder | Replace `/your/data/path` |
| `EMAIL_SECRET` | Without it, IMAP passwords are lost on every restart | `openssl rand -hex 32` |

### Container hardening and resource limits

The `docker-compose.yml` shipped with the project applies several layers of
protection out of the box. All numeric limits are tunable via environment
variables.

#### Resource limits — why they matter

At `BARCODE_DPI=300`, rasterising one A4 page to a PIL image requires
approximately **26 MB of RAM**. Without limits:

| Scenario | Peak RAM | Risk |
|----------|----------|------|
| 10-page PDF, 1 concurrent | ~260 MB | Low |
| 60-page PDF, 2 concurrent | ~3 GB | Container OOM kill (exit 137) |
| Malicious 1000-page PDF | ~26 GB | Entire host OOM |

The default `MEM_LIMIT=2g` **contains** an OOM event to this container only.
`MAX_PAGES=100` and `MAX_UPLOAD_MB=50` reject oversized inputs before any
rendering begins, so normal workloads never approach the memory cap.

> **Exit code 137** means the Linux OOM killer terminated the process.
> If you see this in Portainer or `docker ps`, lower `MAX_PAGES` /
> `MAX_CONCURRENT_PROCESSING`, or raise `MEM_LIMIT` to match your hardware.

#### Capability model

The entrypoint runs briefly as `root` to fix ownership of the bind-mounted
`/data` directory (`chown -R PUID:PGID /data`) and to remap the internal user
account to match `PUID`/`PGID` (writes to `/etc/passwd` and `/etc/group`).
It then calls `gosu PUID:PGID` to drop permanently to the unprivileged user
before starting Flask — the application itself never runs as root.

The `cap_drop: ALL` + `cap_add` block retains only the five capabilities
actually used during that startup phase:

| Capability | Used for |
|------------|----------|
| `CHOWN` | `chown /data` to PUID/PGID |
| `SETUID` / `SETGID` | `gosu` identity switch |
| `DAC_OVERRIDE` | Override permissions when `chown` sees files owned by another UID |
| `FOWNER` | `chmod` on files not owned by the current UID |

`no-new-privileges:true` prevents any process inside the container from
gaining additional capabilities via `setuid` binaries.

#### Why not `read_only: true`

The entrypoint must write `/etc/passwd` and `/etc/group` to remap the
internal `nobody` user to `PUID`/`PGID`. These files are on the root
filesystem (not on `/data` or `/tmp`), so `read_only` would break every
container start. The application phase only ever writes to `/data` (bind
mount) and `/tmp` (tmpfs), which are separately mounted and controlled.

#### Tuning for your hardware

```bash
# Light NAS (2 GB RAM, 2 cores) — conservative
MEM_LIMIT=1g  CPU_LIMIT=1.0  MAX_PAGES=50  MAX_CONCURRENT_PROCESSING=1

# Desktop / server (16 GB RAM, 8 cores) — generous
MEM_LIMIT=8g  CPU_LIMIT=4.0  MAX_PAGES=200 MAX_CONCURRENT_PROCESSING=4
```

Set these in Portainer's **Environment variables** section or in a `.env`
file alongside `docker-compose.yml`.

---

### Portainer GitOps setup

| Field | Value |
|-------|-------|
| Repository URL | `https://github.com/lheriss/pdf-dispatch` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |
| Authentication | Token — your GitHub PAT (`repo` + `read:packages` scopes) |
| GitOps updates | ✅ Enabled, polling every 1 min |
| Re-pull image | ✅ Enabled |
| Force redeployment | ❌ Disabled |

> **How automatic updates work**: on every push to `main`, GitHub Actions builds and pushes a new image to `ghcr.io`. Portainer polls every minute, detects the new commit, pulls the updated `docker-compose.yml`, then pulls the new image from ghcr.io. If the image digest has changed, the container is restarted automatically — no manual action needed.
>
> **Why Force redeployment is disabled**: enabling it would restart the container at *every* polling interval (every minute), even when nothing has changed. Keep it disabled — re-pull image alone is sufficient.

### Automatically created subfolders

On first startup, pdf-dispatch creates all required subfolders inside `/data`:

```
/data/
├── .splitter_config.json   ← all settings (persistent across restarts)
├── input/                  ← drop PDFs here to process them
└── output/
    ├── error/              ← invalid or unreadable files
    ├── processed/          ← archived source files (if archiving is on)
    └── no_code/            ← PDFs where no trigger code was detected
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/data` | Root data folder — all subfolders are managed beneath it |
| `PUID` | — | UID of the host user who should own the output files |
| `PGID` | — | GID of the host user |
| `TZ` | `Europe/Zurich` | Container timezone |
| `EMAIL_SECRET` | — | Encryption key for stored IMAP passwords. **Set this once and never change it** — changing it makes all stored passwords unreadable. Generate with `openssl rand -hex 32`. |
| `APP_LANGUAGE` | `fr` | Interface language at first startup (`fr` or `en`). After first launch the saved value in the config file takes precedence. |
| `APP_USERNAME` | — | Enables HTTP Basic auth when set together with `APP_PASSWORD` |
| `APP_PASSWORD` | — | Password for Basic auth |
| `API_KEY` | — | Override the auto-generated API key. If absent, a key is generated at first startup and stored in `.splitter_config.json`. |
| `POST_PROCESS_SCRIPT` | — | Path (inside the container) to a script run after every file. See [Post-processing hook](#3-post-processing-hook). |
| `POST_PROCESS_TIMEOUT` | `30` | Seconds before the script is killed |
| `BARCODE_SCANNER` | `ZXING` | `ZXING` (recommended) or `PYZBAR` |
| `BARCODE_DPI` | `300` | Increase to `600` if codes are not detected |
| `BARCODE_DPI_SCAN` | `200` | DPI used for the fast first pass over all pages. Pages where a barcode is detected are re-decoded at `BARCODE_DPI` for accuracy. Pages with no code (content pages) are never rasterised at full DPI, making large all-content PDFs ~4× faster to process. Set equal to `BARCODE_DPI` to disable two-pass mode and always scan at full DPI (useful if your barcodes are very small or printed at low quality). Raised from 150 to 200 to reliably detect QR codes in email attachments where the QR image occupies a smaller area of the page than on dedicated separator sheets. |
| `BARCODE_UPSCALE` | `1.0` | Upscale factor applied before detection |
| `FILE_STABLE_TIMEOUT` | `60` | Max seconds to wait for a file to stop changing before processing. Files that exceed this timeout (zero bytes, or still growing) are moved to `/data/output/error/`. |
| `FILE_STABLE_INTERVAL` | `2` | Seconds between two file-size checks during stabilisation |
| `MAX_LOG_ENTRIES` | `200` | Maximum number of entries kept in the activity log |
| `MAX_UPLOAD_MB` | `50` | Maximum file size accepted by `/api/upload` and the web drag-and-drop. Files exceeding this limit are rejected immediately (HTTP 400 with a clear error message) — no bytes are written to disk. Reduce if memory is constrained. |
| `MAX_PAGES` | `100` | Maximum number of pages a PDF may contain. Checked after upload using pypdf (no rendering — cheap) and before the watchdog starts barcode scanning. PDFs over the limit are deleted and an error is returned to the caller. At `BARCODE_DPI=300`, each A4 page requires ~26 MB of RAM during scanning; a 200-page PDF would need ~5 GB and crash the container. |
| `MAX_CONCURRENT_PROCESSING` | `2` | Maximum number of PDFs processed simultaneously. All three processing modes (watchdog/file-drop, API upload, email attachment) funnel through the same `process_file` function; this setting limits how many run concurrently regardless of source. At `BARCODE_DPI=300` each concurrent render occupies ~26 MB per page; more than 2–3 simultaneous renders can saturate the NAS CPU/RAM and make the Flask API unresponsive (GET `/api/tasks` latency spikes from 2 ms to tens of seconds). Increase only if your hardware has spare capacity. |
| `MAX_WORKER_THREADS` | `max(8, MAX_CONCURRENT_PROCESSING + 4)` | Size of the internal thread pool shared between the file-drop watchdog and `scan_existing`. Each incoming file consumes one worker slot while waiting for stabilisation; the rendering concurrency is further limited by `MAX_CONCURRENT_PROCESSING`. Reducing this value limits memory pressure on very constrained hardware (each thread has ~8 MB of virtual stack); increasing it allows more files to be queued in parallel before hitting the pool limit. Default is at least 8 to handle normal spikes at startup. |
| `API_TASK_TIMEOUT` | `120` | Hard deadline in seconds for processing a single PDF. Checked immediately before and after `find_split_pages` (the DPI-render step). If the deadline is exceeded the file is moved to `/data/output/error/` and the task is marked `error`. Cannot interrupt an ongoing page render (Python threads cannot be killed), but prevents post-processing of a result that arrived after the deadline and releases the concurrency slot. |
| `MAX_REQUEST_MB` | `500` | Global ceiling on the HTTP request body size across **all** endpoints (in megabytes). Flask's WSGI layer buffers the full request body before any application code runs; without this cap a single oversized JSON body could exhaust memory regardless of `MAX_UPLOAD_MB`. The upload route applies its own `MAX_UPLOAD_MB` limit per file on top of this ceiling, so a multipart batch of several files can legitimately exceed `MAX_UPLOAD_MB` in aggregate without hitting `MAX_REQUEST_MB`. Reduce on very memory-constrained hardware; increase only if you routinely upload very large batches. |
| `APP_VERSION` | — | Version string shown in the footer and injected into `/api/openapi.json`. Set automatically at build time from the commit SHA — do not override in production. |
| `PORT` | `5880` | Host port mapped to the container's internal port 5000. Change if 5880 conflicts with another service. |
| `DATA_VOLUME` | `/data` | Host path mounted as `/data` inside the container. Set this in Portainer's environment variables instead of editing `docker-compose.yml` directly. |
| `TMP_SIZE` | `512m` | Size of the tmpfs mount at `/tmp`. `pdf2image` writes rendered page images here during barcode scanning. Increase if you process very large PDFs at high DPI, or if the rasteriser crashes with "no space left on device" errors. |
| `MEM_LIMIT` | `2g` | Hard memory cap for the container (`mem_limit` + `memswap_limit`). At 300 DPI each A4 page requires ~26 MB of RAM during rasterisation; a 60-page PDF with `MAX_CONCURRENT_PROCESSING=2` can peak at ~3 GB. **If this limit is exceeded, Docker kills the container with exit code 137 (OOM kill)** — increase the limit on machines with more RAM, or lower `MAX_PAGES` and `MAX_CONCURRENT_PROCESSING` to reduce peak usage. Setting `memswap_limit` equal to `mem_limit` disables swap for this container, which avoids latency spikes from disk-backed memory. |
| `CPU_LIMIT` | — | CPU quota (`cpus`), expressed in fractional cores (`1.5` = 1.5 cores). **Not set by default** — requires `CONFIG_CFS_BANDWIDTH` kernel support and mounted cgroup CPU controllers. Absent from the default `docker-compose.yml` because it causes a fatal error on NAS kernels that lack this support (Synology DSM, some ARM boards). To enable it on a compatible host, add `cpus: "${CPU_LIMIT:-2.0}"` to the compose file. |
| `PIDS_LIMIT` | `256` | Maximum number of processes and threads (`pids_limit`). Protects against runaway thread creation from a defective PDF or a bug in the thread pool. Reduce on very constrained devices; increase if you see "fork: Resource temporarily unavailable" errors under heavy load. |
| `SSRF_PROTECTION` | `off` | When set to `block`, outbound HTTP requests made by pdf-dispatch (webhook deliveries, webhook test, IMAP connection test) are checked against the resolved IP of the destination host. Requests targeting private, loopback or link-local addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`, `fc00::/7`, `::1`) are blocked and return HTTP 400. Set to `off` (default) if you legitimately send webhooks to local services (home automation hub, internal Zapier relay, etc.). |

---

## 2. Web interface

Access the interface at `http://<host>:5880`. All configuration is applied immediately — no restart needed.

### Split triggers

The trigger list defines which barcode/QR code values cause a split. If the list is empty, **every detected code** triggers a split.

**Glob patterns** are supported: `INVOICE*` matches `INVOICE`, `INVOICE_2025`, etc. Patterns `*`, `?`, `[A-Z]`, `[0-9]` follow standard Unix glob syntax.

Each trigger in the list shows indicator badges:

| Badge | Meaning |
|-------|---------|
| `~` | Glob pattern |
| `Aa` | Case-insensitive matching active |
| `✂` | Separator page will be deleted |

Clicking a trigger opens its configuration panel:

| Option | Default | Description |
|--------|---------|-------------|
| Delete the separator page | No | When enabled, the page containing the code is removed from the output. A document that consists only of the code page is discarded entirely. |
| Case-sensitive matching | Yes | When disabled, `INVOICE` also matches `invoice`. Has no effect on glob patterns. |

**Multiple triggers on the same page** — one output document is produced per matching trigger, each with its own options applied.

#### Downloading the separator page

The **⬇ Download PDF** button inside a trigger's panel generates a printable A4 page containing that trigger's code as a QR code or Code128 barcode. Insert it between documents before scanning. This button is disabled for glob patterns (no fixed value to encode).

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Separator placement | Before | Whether the separator page precedes (`before`) or follows (`after`) the document it names. Combined with the per-trigger delete option, this gives four behaviours: keep-before (first page of output), delete-before (removed), keep-after (last page of output), delete-after (removed, names preceding content). |
| Sort into subfolders | No | Creates one subfolder per trigger code inside `/data/output/`. |
| Archive source file | No | Moves the source file to `/data/output/processed/` after processing. When off, the source is deleted. |
| Detailed log | No | Also shows verbose events (email polling, stabilisation waits, etc.) in the activity log. |
| Interface language | French | **FR** / **EN** buttons switch the interface language. The choice is saved immediately. |

### Configuration panels

Four expandable panels live in the Options section — click their button to open them. Only one panel can be open at a time.

#### 📁 Folders

Shows the current path of each folder (relative to `DATA_DIR`) and lets you rename them individually. Changes take effect immediately on disk. If a folder has been accidentally deleted, a **Recreate** button recreates it.

#### ✉ Email (IMAP)

Configure one or more IMAP accounts to poll for PDF attachments. Each configuration is independent:

| Field | Description |
|-------|-------------|
| Name | Display name (must be unique) |
| Host / Port | IMAP server address and port (default: 993) |
| Username / Password | Account credentials — password is stored encrypted |
| IMAP folder | Folder to monitor (default: `INBOX`) |
| Poll interval | Minutes between checks (minimum 1) |
| Filter — From | Only process emails whose sender contains this string |
| Filter — Subject | Only process emails whose subject contains this string |
| Action after download | Mark as read / Delete / Leave untouched |
| TLS mode (`use_ssl`) | **On** (IMAP4_SSL, port 993) by default. Set to **Off** for plain IMAP without TLS (IMAP4, port 143) — useful for local test servers (Greenmail, MailHog) confined to an internal network. |
| Default trigger | Applied to attachments that contain no barcode |

The **Test connection** button verifies the parameters without saving.

Each configuration tracks processed Message-IDs to avoid duplicates (limit: 1000 IDs or 90 days). When the limit is reached, retrieval is blocked and a **Reset processed IDs** button appears.

**Accounts requiring an app password (Gmail, Outlook, etc.)**

Many providers no longer accept the regular account password for IMAP.

- **Gmail**: enable 2-step verification → https://myaccount.google.com/apppasswords → create an app password → use it in the Password field. Server: `imap.gmail.com`, port `993`.
- **Microsoft 365**: https://account.live.com/proofs/AppPassword — same principle.

#### 🔗 Webhook HTTP

See [Advanced — Webhook HTTP](#advanced--webhook-http) below.

#### 🔑 API access

Displays the current API key, with options to reveal it, copy it, or regenerate it. The key is auto-generated at first startup and stored in `.splitter_config.json`. To use a fixed key instead, set the `API_KEY` environment variable — regeneration from the UI is then disabled.

See [Advanced — REST API](#advanced--rest-api) for usage.

### Filename construction

Build the output filename by combining tokens, separated by a configurable character (`_`, `-`, `.`, or none). Drag tokens to reorder them.

| Token | Required | Description |
|-------|----------|-------------|
| Trigger | No | The detected trigger code value, or `no_code` |
| Free text | No | Any fixed string; multiple tokens allowed |
| Timestamp | No | Date/time in strftime format. Presets: **Date** (`%Y%m%d`), **Date+time** (`%Y%m%d-%H%M`) |
| Counter | **Yes** | Sequential number, 3–8 digits, global and persistent across restarts |

The counter can be reset independently via the **Reset counter** button.

**Preview** updates in real time. Click **Save format** to persist the configuration.

### PDF metadata

Every output file receives metadata:

| Field | Value |
|-------|-------|
| Title | Output filename |
| Subject | Trigger code value |
| Author | `pdf-dispatch` |
| Creation date | Processing date and time |

### Statistics

| Counter | Description |
|---------|-------------|
| Processed | Total source files processed |
| Produced | Total output documents generated |
| Errors | Total errors encountered |
| Last file | Name and timestamp of the last processed file |

**Reset statistics** resets all counters to zero (does not affect the filename counter).

### Activity log

Shows recent events: file processing, configuration changes, email polling, errors. Displayed newest-first. Use the **Detailed log** toggle to also see verbose events. The number of retained entries is controlled by `MAX_LOG_ENTRIES`.

### Special cases

| Situation | Behaviour |
|-----------|-----------|
| PDF with no trigger code | Copied to `/data/output/no_code/` |
| Corrupted or unreadable PDF | Moved to `/data/output/error/` |
| Non-PDF file | Moved to `/data/output/error/` |
| Files present at startup | Processed automatically |
| File that never stabilises (zero bytes or still growing after `FILE_STABLE_TIMEOUT` s) | Moved to `/data/output/error/` with a stabilisation timeout reason |
| Separator page only + delete enabled | Discarded, warning logged |
| Multiple triggers on one page | One output document per trigger |
| Upload exceeds `MAX_UPLOAD_MB` | Rejected with HTTP 400 before writing to disk. The `errors[]` array in the response contains the filename and the limit. |
| PDF exceeds `MAX_PAGES` | Accepted by the upload endpoint, then immediately deleted. A task is created with `status: error` and a descriptive message. The watchdog never processes it. |

---

## Advanced — Webhook HTTP

After every processed file, pdf-dispatch can POST a JSON payload to any URL. Configure it in the **Webhook HTTP** panel (inside Options).

| Field | Description |
|-------|-------------|
| Enable | Toggle the webhook on or off without losing other settings |
| URL | Any HTTP/HTTPS endpoint |
| Events | `All (success + errors)` / `Success only` / `Errors only` |
| HMAC-SHA256 secret | Optional. When set, every request includes `X-Signature: sha256=<hmac>` |
| Test | Sends a synchronous test payload and shows the response code |

### Payload

The payload is a "fat event" — the receiver needs no follow-up call to the API.

```json
{
  "event":       "file.processed",
  "timestamp":   "2025-01-15T10:30:00",
  "source_file": "batch_scan.pdf",
  "status":      "success",
  "triggers":    ["INVOICE"],
  "documents":   [
    {
      "filename": "INVOICE_000042.pdf",
      "path":     "output/INVOICE_000042.pdf"
    }
  ],
  "docs_count":  1,
  "error":       ""
}
```

`status` is `"success"` or `"error"`. On error, `error` contains a description.

When a file was uploaded via API with a [per-file config override](#per-file-configuration-override), the payload also includes:

```json
"config_override": {
  "separator_placement": "after",
  "split_values": [{"value": "FACTURE", "page_handling": "delete", "case_sensitive": true}]
}
```

### Signature verification

```js
// Node.js
const sig = crypto.createHmac('sha256', secret).update(body).digest('hex');
if (!crypto.timingSafeEqual(
  Buffer.from(`sha256=${sig}`),
  Buffer.from(req.headers['x-signature'])
)) {
  return res.status(401).send('Invalid signature');
}
```

### Delivery

Asynchronous (daemon thread) — never blocks document processing. Up to 3 attempts with exponential backoff (0 s, 2 s, 4 s). Results are logged in the activity log.

### Home Assistant example

```yaml
automation:
  trigger:
    platform: webhook
    webhook_id: pdf_dispatch
  action:
    service: notify.mobile_app
    data:
      message: >
        {{ trigger.json.docs_count }} doc(s) —
        {{ trigger.json.triggers | join(', ') }}
        from {{ trigger.json.source_file }}
```

---

## Advanced — Post-processing hook

Run an arbitrary script after every file processed by pdf-dispatch. Useful for custom notifications, file routing, DMS ingestion, or any downstream automation.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `POST_PROCESS_SCRIPT` | — | Absolute path inside the container to an executable script |
| `POST_PROCESS_TIMEOUT` | `30` | Seconds before the script is killed |

The script must be executable (`chmod +x`). Its stdout and stderr are forwarded line-by-line to the activity log. A non-zero exit code logs a warning but does not affect document processing.

**docker-compose.yml setup:**

```yaml
environment:
  POST_PROCESS_SCRIPT: /data/scripts/notify.sh
volumes:
  - /your/data/path:/data
```

Place your script in `/your/data/path/scripts/` so it persists across container recreations.

### Variables available in the script

| Variable | Description |
|----------|-------------|
| `PDF_DISPATCH_STATUS` | `success` or `error` |
| `PDF_DISPATCH_SOURCE` | Original filename (basename only) |
| `PDF_DISPATCH_TRIGGERS` | Comma-separated detected trigger codes |
| `PDF_DISPATCH_OUTPUTS` | Comma-separated absolute paths of produced files |
| `PDF_DISPATCH_DOCS_COUNT` | Number of output documents produced |
| `PDF_DISPATCH_TIMESTAMP` | ISO 8601 timestamp |
| `PDF_DISPATCH_ERROR` | Error description (empty on success) |
| `PDF_DISPATCH_DATA_DIR` | Value of `DATA_DIR` |

### Examples

**Pushover notification:**
```bash
#!/bin/bash
[ "$PDF_DISPATCH_STATUS" = "success" ] || exit 0
curl -s \
  -F "token=YOUR_APP_TOKEN" \
  -F "user=YOUR_USER_KEY" \
  -F "message=[$PDF_DISPATCH_TRIGGERS] $PDF_DISPATCH_SOURCE → $PDF_DISPATCH_DOCS_COUNT doc(s)" \
  https://api.pushover.net/1/messages.json
```

**Home Assistant REST call:**
```bash
#!/bin/bash
curl -s -X POST \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"source\":\"$PDF_DISPATCH_SOURCE\",\"trigger\":\"$PDF_DISPATCH_TRIGGERS\"}" \
  http://homeassistant.local:8123/api/webhook/pdf_dispatch
```

---

## Advanced — REST API

pdf-dispatch exposes a REST API covering all operations available from the web interface, plus file upload and async task tracking.

### Authentication

All endpoints except `/healthz` support two methods, checked in order:

1. **API key** (recommended for scripts): `X-API-Key: <key>` header.
   The key is auto-generated at first startup and shown in the **API access** panel. Override with the `API_KEY` environment variable.

2. **HTTP Basic auth**: active only when both `APP_USERNAME` and `APP_PASSWORD` are set.

If neither is configured, the API is open (trusted-network assumption).
If `X-API-Key` is present but wrong, the request is **always rejected** — no fall-through to Basic auth.

### Quick start

```bash
KEY="your-api-key"
BASE="http://your-server:5880"

# Upload a PDF
curl -F "file=@invoice.pdf" -H "X-API-Key: $KEY" $BASE/api/upload

# Poll until done
curl -H "X-API-Key: $KEY" $BASE/api/tasks/<task_id>

# Download result
curl -H "X-API-Key: $KEY" "$BASE/api/file/output/INVOICE_000042.pdf" -o result.pdf

# List recent output files
curl -H "X-API-Key: $KEY" "$BASE/api/recent?n=10"

# Read current configuration
curl -H "X-API-Key: $KEY" $BASE/api/state | jq .app_config

# Update a setting
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"separator_placement":"after"}' $BASE/api/config
```

### Async upload workflow

`POST /api/upload` saves the file and returns immediately with a `task_id`. Poll `GET /api/tasks/<id>` to follow the processing:

```python
import requests, time

headers = {"X-API-Key": "your-key"}
base    = "http://your-server:5880"

# Upload
r       = requests.post(f"{base}/api/upload",
                        files={"file": open("batch.pdf", "rb")},
                        headers=headers)
task_id = r.json()["saved"][0]["task_id"]

# Poll (typically 2–10 s depending on PDF size)
while True:
    task = requests.get(f"{base}/api/tasks/{task_id}", headers=headers).json()["task"]
    if task["status"] in ("success", "error"):
        break
    time.sleep(1)

# Download results
for doc in task["outputs"]:
    data = requests.get(f"{base}{doc['download_url']}", headers=headers).content
    open(doc["filename"], "wb").write(data)
```

Task states: `pending` → `processing` → `success` / `error`

### Per-file configuration override

Pass temporary configuration overrides at upload time — they apply only to the files in that request and never modify the global configuration.

```bash
curl -F "file=@batch.pdf" \
     -F 'split_values=[{"value":"FACTURE","page_handling":"delete","case_sensitive":true}]' \
     -F "separator_placement=after" \
     -F "subdirs_by_trigger=true" \
     -H "X-API-Key: $KEY" \
     http://your-server:5880/api/upload
```

| Override field | Type | Description |
|----------------|------|-------------|
| `split_values` | JSON string | Trigger list. `[]` = split on every code. |
| `separator_placement` | `"before"` \| `"after"` | Overrides global placement |
| `subdirs_by_trigger` | `"true"` \| `"false"` | Overrides subfolder routing |
| `delete_source` | `"true"` \| `"false"` | Overrides source-file handling |
| `log_verbose` | `"true"` \| `"false"` | Overrides log verbosity |

The override is echoed in the upload response (`saved[n].override`), stored in the task (`config_override`), and included in the webhook payload when active.

The `trigger` field (no-barcode fallback) and `split_values` are independent — both can be provided together.

### Key endpoints

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/healthz` | Health check (no auth) |
| `GET` | `/api/state` | Full state: stats, log, config, email status |
| `POST` | `/api/config` | Update any config key(s) |
| `POST` | `/api/upload` | Upload PDFs → returns `task_id` per file |
| `GET` | `/api/recent?n=20` | List recent output files with download URLs |
| `GET` | `/api/file/<path>` | Download an output file (`?download=1` forces attachment) |
| `GET` | `/api/tasks?n=20` | List recent upload tasks |
| `GET` | `/api/tasks/<id>` | Get task status |
| `GET` | `/api/separator/<idx>` | Download separator PDF for trigger at index `idx` |
| `POST` | `/api/webhook/test` | Send test payload to the configured webhook URL |
| `POST` | `/api/settings/regenerate-api-key` | Generate a new API key |
| `POST` | `/api/stats/reset` | Reset processing statistics |

### Full API reference

The complete API is documented in an **OpenAPI 3.1 specification** served by the application itself:

| URL | Description |
|-----|-------------|
| `/api/docs` | Swagger UI — interactive documentation (requires internet for CDN) |
| `/api/openapi.json` | Spec as JSON — import into Postman, Insomnia, etc. |
| `/api/openapi.yaml` | Same spec as YAML |

```bash
# Generate a typed Python client
npx @openapitools/openapi-generator-cli generate \
  -i http://your-server:5880/api/openapi.json \
  -g python -o ./pdf-dispatch-client
```

---

## Security

- **IMAP passwords** are encrypted (Fernet/AES) with a key derived from `EMAIL_SECRET`. They are never transmitted to the browser.
- **API key** is stored in `.splitter_config.json`. Override with `API_KEY` env var to keep it out of the config file.
- **HTTP Basic auth** (`APP_USERNAME`/`APP_PASSWORD`): when both are set, every route (except `/healthz`) requires authentication. Without them, the interface is open.
- **`X-API-Key`**: if provided and invalid, the request is rejected immediately — no fall-through to Basic auth.
- **Folder paths** are validated to stay within `DATA_DIR` — path traversal is blocked.
- **IMAP field validation** — `host`, `username`, and `folder` reject CRLF characters (`\r`, `\n`) to prevent IMAP injection. `port` is validated to `1–65535` and `poll_interval` must be ≥ 1.
- **`password_enc`** is never returned by any API endpoint — the encrypted field is stripped before serialisation.
- **`POST /api/config`** rejects attempts to overwrite internal-only keys (`email_configs`, `stats`, `counter`) and validates folder paths before applying.
- **Activity log** — control characters in messages posted via `/api/log` are sanitised (replaced with spaces) to prevent log injection and terminal escape sequences.
- **Webhook URL sanitisation** — `webhook_url` and `webhook_secret` are stripped of CR (`\r`) and LF (`\n`) characters on every config update to prevent HTTP header injection. A value such as `"\r\nX-Inject: bad"` is stored and delivered as `"X-Inject: bad"` (CRLF prefix removed).
- **`/api/recent?n=`** — non-integer values return HTTP 400 instead of 500.
- **IMAP connection** — plain (`use_ssl: false`, port 143) or SSL/TLS (`use_ssl: true`, default, port 993). Use plain only for servers on a trusted internal network (Docker, VPN). Combine with `verify_ssl: false` only for self-signed certificates on private infrastructure.
- **Resource limits** — several env vars protect against resource exhaustion:
  - `MAX_UPLOAD_MB` (default 50) and `MAX_PAGES` (default 100): reject oversized files before any rendering. A 200-page PDF at 300 DPI needs ~5 GB of RAM and can crash Docker.
  - `MAX_CONCURRENT_PROCESSING` (default 2): limits concurrent DPI renders across all input modes (file-drop, API, email). Without this cap, simultaneous uploads saturate the CPU/RAM and make the Flask API unresponsive even for lightweight requests.
  - `API_TASK_TIMEOUT` (default 120 s): moves a task to `/error/` if processing takes too long, preventing a single stuck PDF from occupying a concurrency slot indefinitely.
- **Basic auth transmits credentials in base64** (not encrypted). Combine with HTTPS if exposed beyond a trusted network — for example via the Synology reverse proxy (DSM → Control Panel → Application Portal → Reverse Proxy) with a Let's Encrypt certificate.
- For SSO or multi-factor authentication, place the service behind an authentication reverse proxy (Authelia, Authentik, etc.).

### Container-level hardening

| Protection | Mechanism |
|------------|-----------|
| **No privilege escalation** | `no-new-privileges:true` — no `setuid` binary can grant extra capabilities |
| **Minimal capabilities** | `cap_drop: ALL` + five capabilities for startup only; dropped after `gosu` |
| **Unprivileged runtime** | `gosu PUID:PGID` drops from root before Flask starts — all file I/O runs as `PUID:PGID` |
| **Scratch space isolation** | `/tmp` is a `tmpfs` (in-memory, size-bounded) — rendered page images never touch the host disk |
| **Memory ceiling** | `mem_limit` + `memswap_limit` — a malicious PDF that exhausts RAM kills only this container, not the host |
| **CPU cap** | `cpus` — prevents barcode rasterisation from starving co-located services |
| **Process count cap** | `pids_limit` — bounds thread creation regardless of load |

> ⚠️ **OOM kill (exit code 137)**: if a PDF exceeds the memory ceiling during
> rasterisation, Docker sends SIGKILL. The container restarts automatically
> (`restart: unless-stopped`). The file is **not** moved to `/error/` — on
> restart pdf-dispatch will attempt it again. Prevent this by setting
> `MAX_PAGES` and `MAX_UPLOAD_MB` conservatively, or by raising `MEM_LIMIT`
> to accommodate your largest expected PDFs.

---

## Development

### Project structure

```
pdf-dispatch/
├── Dockerfile
├── docker-compose.yml
├── pytest.ini
├── tests/
│   ├── conftest.py              ← pytest fixtures (Flask test client, webhook receiver)
│   ├── test_api.py              ← API route tests (Flask test client, 44 tests)
│   ├── test_webhook.py          ← webhook integration tests (local HTTP receiver, 37 tests)
│   ├── test_python_core.py      ← unit tests for core Python functions (86 tests)
│   ├── test_js_t_collision.py   ← detects t() key shadowing bugs in app.js
│   ├── test_i18n_keys.py        ← verifies FR/EN key consistency
│   └── test_js_functional.js    ← Node.js functional tests (no browser)
└── splitter/
    ├── app.py              ← service entry point (~120 lines); re-exports symbols for tests
    ├── openapi.yaml        ← OpenAPI 3.1 source (human-readable)
    ├── openapi.json        ← pre-built JSON version (served by /api/openapi.json)
    ├── entrypoint.sh       ← UID/GID management + app launch
    ├── requirements.txt
    ├── dispatch/           ← application package (modular architecture)
    │   ├── __init__.py     ← create_app() Application Factory
    │   ├── config.py       ← environment constants, persistent config, counter, stats
    │   ├── crypto.py       ← Fernet/AES password encryption (EMAIL_SECRET)
    │   ├── i18n.py         ← translation loader and t() function
    │   ├── state.py        ← in-memory shared state (locks, log, task tracking)
    │   ├── hook.py         ← post-processing hook (POST_PROCESS_SCRIPT)
    │   ├── webhook.py      ← outbound webhook (SSRF guard, HMAC, async delivery)
    │   ├── processing.py   ← full PDF pipeline (stabilisation, barcode scan, split, write)
    │   ├── email_poller.py ← background IMAP poller (threading, deduplication, limits)
    │   ├── watcher.py      ← watchdog observer on /data/input/, startup scan
    │   └── routes/         ← Flask Blueprints (one per concern)
    │       ├── auth.py         ← before_app_request: X-API-Key + HTTP Basic auth
    │       ├── docs.py         ← /healthz, /api/runtime, /api/openapi.*, /api/docs
    │       ├── core.py         ← /api/state, /api/config, /api/log, /api/dirs, /api/recent
    │       ├── upload.py       ← /api/upload, /api/tasks, /api/file
    │       ├── email_routes.py ← /api/email/configs, /api/email/test, /api/email/reset_ids
    │       ├── separator.py    ← /api/separator/<idx>
    │       └── webhook_routes.py ← /api/webhook/test
    ├── i18n/
    │   ├── fr.json         ← French translations (283 keys)
    │   ├── en.json         ← English translations
    │   └── check_keys.py   ← verifies key consistency between FR/EN files
    ├── templates/
    │   └── index.html      ← single-page app HTML skeleton (rendered with Jinja2)
    └── static/
        ├── css/style.css   ← theme (CSS variables, layout)
        └── js/app.js       ← all frontend logic (no build step, no dependencies)
```

### `splitter/app.py` — section map

Single file (~3500 lines), sections marked by `# --- ... ---` comments:

| Section | Content |
|---------|---------|
| Base paths | Environment variable reading, working folder paths |
| i18n | Loading `fr.json`/`en.json`, `t(key, **kwargs)` function |
| Persistent configuration | `load_config`, `save_config`, defaults, validation |
| Email password encryption | Fernet/AES, `EMAIL_SECRET` key derivation |
| Shared state | Statistics, processing queue, in-memory activity log |
| Task tracking | `_tasks` OrderedDict, `_task_create`, `_task_update` |
| Per-file config overrides | `_file_config_overrides`, `_store_file_override`, `_pop_file_override` |
| Post-processing hook | `_run_post_process_hook` — subprocess execution after each file |
| Outbound webhook | `_fire_webhook`, `_deliver_webhook` — async HTTP POST with HMAC |
| Filename construction | Token validation, `build_filename` |
| Folders | Output path resolution, per-trigger subfolders |
| File stabilisation | Wait for write completion before processing |
| Code detection | ZXing/pyzbar, barcode matching against trigger list |
| PDF metadata | Writing metadata onto produced files |
| Separator page generation | QR/Code128 separator PDF generation |
| IMAP retrieval | Polling, filters, duplicate tracking, attachment download |
| Main processing | `process_file` — complete pipeline |
| Monitor | `watchdog.Observer` on `/data/input/`, startup scan |
| Flask | App init, `before_request` auth (Basic + API key) |
| API routes | All `/api/...` endpoints |
| OpenAPI routes | `/api/openapi.json`, `/api/openapi.yaml`, `/api/docs` |

### Frontend

- **`index.html`** — HTML skeleton only. Translations for the active language are injected server-side into `window.I18N`.
- **`app.js`** — all frontend logic, no external dependencies, no build step. Global state in `cfg` and `state`; rendering functions (`renderTriggers`, `renderOptions`, `renderWebhook`, `renderApiKey`, `renderTokens`, …); action functions (save, validate, upload, testWebhook, …). `t('key', {param: value})` translates via `window.I18N`.
- **`style.css`** — theme via CSS variables (`:root`). Dark mode by default.

No build step: edit source files and restart the container.

### Updating `openapi.json`

`openapi.yaml` is the source of truth (human-readable). `openapi.json` is the pre-built version served by the API. After editing the YAML, regenerate the JSON:

```bash
python3 -c "
import yaml, json
json.dump(
  yaml.safe_load(open('splitter/openapi.yaml')),
  open('splitter/openapi.json', 'w'),
  ensure_ascii=False, indent=2
)"
```

### Running tests locally

```bash
pip install pytest reportlab
pytest                    # all tests
pytest tests/test_api.py  # API tests only (no server needed)
pytest -v -k "webhook"    # filter by name

# Legacy test runners (output pass/fail summary)
python3 tests/test_python_core.py
node tests/test_js_functional.js
```

### CI/CD

GitHub Actions (`.github/workflows/docker-build.yml`) runs on every push to `main`:

1. Dependency security audit (`pip-audit`)
2. Python syntax check; JS syntax check
3. i18n key consistency; `t()` collision detection
4. Python core unit tests (`test_python_core.py`) and JS functional tests
5. Docker image build (with smoke test: container starts and `/healthz` responds)
6. Push to `ghcr.io/lheriss/pdf-dispatch:latest` (and `:<short-sha>`) — only on push to `main`, skipped on PRs

