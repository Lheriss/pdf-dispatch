"""
dispatch/routes/upload.py
Blueprints: file upload and task tracking.
"""
import json
import os
import re
import time
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from dispatch.config import (
    DATA_DIR, DPI, INPUT_DIR, MAX_PAGES, MAX_UPLOAD_MB, OUTPUT_DIR, log,
)
from dispatch.i18n import t
from dispatch.state import (
    _email_triggers, _email_triggers_lock,
    _store_file_override, _task_create, _tasks, _tasks_lock, log_event,
)

bp = Blueprint("upload", __name__)

def _parse_upload_override(form) -> tuple[dict, list[str]]:
    """Parse and validate per-file configuration override fields from a multipart form.

    Returns (override_dict, errors).
    - override_dict contains only the keys that were explicitly provided.
      It is empty ({}) when no override fields are present.
    - errors is a non-empty list when a value fails validation; in that case
      the caller should reject the entire request with HTTP 400.

    Supported fields and their expected formats
    -------------------------------------------
    split_values        JSON string — array of trigger objects:
                          '[{"value":"INVOICE","page_handling":"keep","case_sensitive":true}]'
                        page_handling: "keep" | "delete" (default "keep")
                        case_sensitive: boolean (default true)
                        Empty array ([]) is valid and means "split on every code".

    separator_placement "before" | "after"

    subdirs_by_trigger  "true" | "1" | "yes"  → True
    delete_source       "false" | "0" | "no"  → False
    log_verbose         (any truthy/falsy string)

    split_values and trigger/default_trigger are independent:
    - split_values controls which barcodes trigger a split.
    - trigger is the fallback name when no barcode is detected.
    Both can be provided together.
    """
    override: dict = {}
    errors:   list = []

    # ── split_values ────────────────────────────────────────────────────────
    raw_sv = (form.get("split_values") or "").strip()
    if raw_sv:
        try:
            sv = json.loads(raw_sv)
        except json.JSONDecodeError as exc:
            errors.append(f"split_values: invalid JSON — {exc}")
            sv = None
        if sv is not None:
            if not isinstance(sv, list):
                errors.append("split_values must be a JSON array")
            else:
                cleaned = []
                ok = True
                for i, item in enumerate(sv):
                    if not isinstance(item, dict):
                        errors.append(f"split_values[{i}]: must be a JSON object")
                        ok = False; break
                    val = str(item.get("value", "")).strip()
                    if not val:
                        errors.append(f"split_values[{i}].value: must be a non-empty string")
                        ok = False; break
                    ph = item.get("page_handling", "keep")
                    if ph not in ("keep", "delete"):
                        errors.append(
                            f"split_values[{i}].page_handling: must be 'keep' or 'delete' (got {ph!r})"
                        )
                        ok = False; break
                    cs = item.get("case_sensitive", True)
                    if not isinstance(cs, bool):
                        cs = str(cs).lower() not in ("false", "0", "no")
                    cleaned.append({"value": val, "page_handling": ph, "case_sensitive": cs})
                if ok:
                    override["split_values"] = cleaned

    # ── separator_placement ─────────────────────────────────────────────────
    sp = (form.get("separator_placement") or "").strip().lower()
    if sp:
        if sp not in ("before", "after"):
            errors.append(f"separator_placement: must be 'before' or 'after' (got {sp!r})")
        else:
            override["separator_placement"] = sp

    # ── boolean fields ──────────────────────────────────────────────────────
    def _bool(key: str):
        raw = form.get(key)
        if raw is None:
            return None
        return raw.strip().lower() not in ("false", "0", "no", "")

    for bool_key in ("subdirs_by_trigger", "delete_source", "log_verbose"):
        v = _bool(bool_key)
        if v is not None:
            override[bool_key] = v

    return override, errors



@bp.route("/api/upload", methods=["POST"])
def api_upload():
    """Receive one or more PDF files and deposit them in INPUT_DIR.

    Optional form fields:
      files    — one or more PDF files (multipart/form-data)
      file     — alias for files (single-file clients)
      trigger  — default trigger code for files that contain no barcode;
                 overrides the email default trigger mechanism for this upload.

    Per-file configuration override fields (apply only to the processing of
    files in this request; the global configuration is never modified):

      split_values        — JSON array of trigger dicts, e.g.:
                            '[{"value":"INVOICE","page_handling":"delete","case_sensitive":true}]'
                            Replaces the global trigger list for these files.
                            Empty array [] means every detected code triggers a split.
      separator_placement — "before" | "after"
      subdirs_by_trigger  — "true" | "false"
      delete_source       — "true" | "false"
      log_verbose         — "true" | "false"

    On validation error the entire request is rejected (HTTP 400) before any
    file is saved.  The response includes an "override" field per saved file
    confirming which overrides were applied.
    """
    from flask import request as req
    if "files" not in req.files and "file" not in req.files:
        return jsonify({"ok": False, "error": t("upload.error_no_file")}), 400
    files = req.files.getlist("files") or req.files.getlist("file")
    trigger_override = (req.form.get("trigger") or req.form.get("default_trigger") or "").strip()

    # Parse and validate config overrides once for the whole request
    config_override, override_errors = _parse_upload_override(req.form)
    if override_errors:
        return jsonify({"ok": False, "error": "; ".join(override_errors)}), 400

    saved = []
    errors = []
    for file in files:
        fname = file.filename or ""
        # Security: sanitise filename
        fname = re.sub(r"[^\w\-. ]", "_", Path(fname).name)
        # Prevent OS errors on excessively long names: truncate stem to 200 chars
        _stem = Path(fname).stem[:200]
        fname = _stem + ".pdf"
        if not fname.lower().endswith(".pdf"):
            errors.append(t("upload.error_not_pdf", filename=fname))
            continue
        if not fname:
            errors.append(t("upload.error_empty_filename"))
            continue
        dest = INPUT_DIR / fname
        # Avoid filename collisions
        counter = 1
        while dest.exists():
            dest = INPUT_DIR / f"{Path(fname).stem}_{counter:03d}.pdf"
            counter += 1

        # ── Resource-limit checks ──────────────────────────────────────────
        # Read the upload into memory so we can check size before writing to
        # disk.  We read at most MAX_UPLOAD_MB + 1 byte so we can detect the
        # oversize case without buffering the entire (potentially huge) file.
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        data = file.stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            errors.append(
                f"{fname}: file exceeds the {MAX_UPLOAD_MB} MB upload limit "
                f"(reduce MAX_UPLOAD_MB env var to change this threshold)"
            )
            continue

        # Write the accepted bytes to disk
        dest.write_bytes(data)

        # Page-count check — open the PDF with pypdf (cheap, no rendering)
        # before the watchdog picks it up and renders every page at DPI.
        try:
            from pypdf import PdfReader as _PdfReader
            _rdr = _PdfReader(str(dest))
            _pages = len(_rdr.pages)
            if _pages > MAX_PAGES:
                dest.unlink(missing_ok=True)
                errors.append(
                    f"{fname}: PDF has {_pages} pages which exceeds the "
                    f"{MAX_PAGES}-page limit (set MAX_PAGES env var to change)"
                )
                continue
        except Exception as _e:
            # Malformed PDF — let the watchdog handle it as usual (error folder)
            log.debug(f"Could not pre-check page count for {fname}: {_e}")
        try: os.chmod(dest, 0o664)
        except: pass
        if trigger_override:
            with _email_triggers_lock:
                _email_triggers[dest.name] = trigger_override
        if config_override:
            _store_file_override(dest.name, config_override)
        task_id = _task_create(dest.name, config_override=config_override if config_override else None)
        entry = {"filename": dest.name, "task_id": task_id}
        if config_override:
            entry["override"] = config_override
        saved.append(entry)
        log_event("info", t("log.file_received_web", filename=dest.name))
    return jsonify({"ok": True, "saved": saved, "errors": errors})




@bp.route("/api/tasks")
def api_tasks():
    """GET /api/tasks[?n=20] — List recent upload tasks, newest first.

    Only covers files uploaded via POST /api/upload.  Files processed from
    INPUT_DIR directly (watchdog, email) do not appear here.

    Each entry:
      id          — task UUID (use with GET /api/tasks/<id>)
      filename    — filename as saved in INPUT_DIR
      status      — "pending" | "processing" | "success" | "error"
      created_at  — ISO 8601 timestamp (upload time)
      updated_at  — ISO 8601 timestamp (last status change)
      triggers    — list of detected trigger code values (on success)
      outputs     — list of {filename, path, download_url} (on success)
      docs_count  — number of output documents produced (on success)
      error       — error description (on error, empty otherwise)
    """
    n = min(int(request.args.get("n", 20)), 200)
    with _tasks_lock:
        items = list(_tasks.values())
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify({"ok": True, "tasks": items[:n], "total": len(items)})




@bp.route("/api/tasks/<task_id>")
def api_task_get(task_id):
    """GET /api/tasks/<task_id> — Get the status of a specific task.

    Typical polling pattern after POST /api/upload:

      # Upload
      r = requests.post("/api/upload", files={"file": open("scan.pdf","rb")},
                        headers={"X-API-Key": key})
      task_id = r.json()["saved"][0]["task_id"]

      # Poll until done (usually a few seconds)
      while True:
          s = requests.get(f"/api/tasks/{task_id}", headers=...).json()["task"]
          if s["status"] in ("success", "error"):
              break
          time.sleep(1)
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return jsonify({"ok": False, "error": "Task not found"}), 404
    return jsonify({"ok": True, "task": dict(task)})




@bp.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    """GET /api/file/<path> — Download a file by its path relative to DATA_DIR.

    The path must point to a file inside OUTPUT_DIR (not input, not the full
    DATA_DIR). This prevents accidental exposure of the configuration file or
    input PDFs in progress.

    The `filepath` parameter matches the `path` field returned by GET /api/recent,
    so callers can directly concatenate:
        download_url = "/api/file/" + recent_item["path"]

    Query parameters:
      download=1   Force a Content-Disposition: attachment header (browser download).
                   Default: inline (browser preview when possible).

    Responses:
      200  File content with Content-Type and Content-Disposition headers.
      400  Path resolves to something other than a regular file.
      403  Path escapes OUTPUT_DIR.
      404  File does not exist.
    """
    try:
        abs_path = (DATA_DIR / filepath).resolve()
        output_dir_resolved = OUTPUT_DIR.resolve()
        # Must be strictly inside OUTPUT_DIR
        abs_path.relative_to(output_dir_resolved)
    except ValueError:
        return jsonify({"ok": False, "error": "Access restricted to output directory"}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not abs_path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404
    if not abs_path.is_file():
        return jsonify({"ok": False, "error": "Not a regular file"}), 400

    as_attachment = request.args.get("download", "0") == "1"
    return send_file(str(abs_path), as_attachment=as_attachment,
                     download_name=abs_path.name)



