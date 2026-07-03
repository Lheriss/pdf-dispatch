"""
dispatch/email_poller.py
-------------------------
Background IMAP poller: downloads PDF attachments received by email,
triggers processing, manages processed message IDs (deduplication), and
enforces safety limits.

Contains:
  - EMAIL_MAX_IDS, EMAIL_MAX_DAYS — retention limits
  - _email_username_key, _email_config_signature — normalisation helpers
  - _email_find_duplicate, _email_find_name_conflict — config validation
  - _proc_ids_path/load/save/delete — dedicated deduplication files
  - _update_email_config, _email_check_limit — config update and safety
  - _imap_poll, _imap_process — background loop and per-account processing
  - start_email_poller, stop_email_poller — thread lifecycle

Internal dependencies:
  - dispatch.config    (get_config, update_config, log, DATA_DIR, MAX_UPLOAD_MB)
  - dispatch.crypto    (decrypt_password)
  - dispatch.i18n      (t)
  - dispatch.state     (log_event, _email_triggers, _email_triggers_lock,
                        _store_file_override)
  - dispatch.processing (process_file)
"""

import email as _email_mod
import hashlib
import imaplib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dispatch.config import (
    DATA_DIR, INPUT_DIR, MAX_UPLOAD_MB, get_config, log, update_config,
)
from dispatch.crypto import decrypt_password
from dispatch.i18n import t
from dispatch.state import (
    _email_triggers, _email_triggers_lock,
    _store_file_override,
    log_event,
)
from dispatch.processing import process_file

EMAIL_MAX_IDS  = 1000
EMAIL_MAX_DAYS = 90
_email_thread       = None
_email_stop         = threading.Event()

def _email_username_key(username: str) -> str:
    """Normalise an IMAP username: 'user' and 'user@domain' are equivalent."""
    u = str(username or "").strip().lower()
    if "@" in u:
        u = u.split("@", 1)[0]
    return u


def _email_config_signature(ec: dict) -> tuple:
    """Signature used to detect duplicate configurations.
    Intentionally excludes: action, default trigger, interval, enabled/disabled."""
    return (
        str(ec.get("host", "")).strip().lower(),
        _email_username_key(ec.get("username", "")),
        str(ec.get("folder", "INBOX") or "INBOX").strip().lower(),
        str(ec.get("filter_from", "")).strip().lower(),
        str(ec.get("filter_subject", "")).strip().lower(),
    )


def _email_find_duplicate(configs: list, signature: tuple, exclude_id: str = None):
    """Search `configs` for an email configuration whose signature
    (_email_config_signature: host/port/user/folder/filters, lowercased)
    matches `signature`. `exclude_id` allows skipping the config being
    edited during an update. Returns the duplicate config or None."""
    for ec in configs:
        if exclude_id and ec.get("id") == exclude_id:
            continue
        if _email_config_signature(ec) == signature:
            return ec
    return None


def _email_find_name_conflict(configs: list, name: str, exclude_id: str = None):
    """Check whether another configuration already uses this name (case/space-insensitive)."""
    name_norm = str(name or "").strip().lower()
    for ec in configs:
        if exclude_id and ec.get("id") == exclude_id:
            continue
        if str(ec.get("name", "")).strip().lower() == name_norm:
            return ec
    return None


# ---------------------------------------------------------------------------
# processed_ids — fichiers dédiés par config email (#12)
# ---------------------------------------------------------------------------
# Les Message-IDs traités sont stockés dans DATA_DIR/.email_proc_<id>.json
# (one file per email configuration) rather than in the main JSON config.
# Avantage : le JSON de config reste léger (~KB) même avec des milliers
# de Message-IDs ; la config principale n'est plus réécrite à chaque cycle
# IMAP.
#
# Sécurité :
#   - Le config_id est uuid4().hex[:12] (aléatoire 48 bits) → imprévisible.
#   - On config CREATION, any pre-existing file for that config_id is
#     deleted (defence in depth against file injection).
#   - À la SUPPRESSION d'une config, le fichier est supprimé.
#   - Validation à la lecture : doit être un JSON {"ids":[str,...], "oldest": str|null}
#     avec au plus EMAIL_MAX_IDS + 100 entrées.  Fichier corrompu → reset.
# ---------------------------------------------------------------------------

def _proc_ids_path(config_id: str) -> Path:
    """Path to the dedicated processed-IDs file for an email config."""
    return DATA_DIR / f".email_proc_{config_id}.json"


def _proc_ids_load(config_id: str) -> tuple[list, str | None]:
    """Load processed IDs from the dedicated file for *config_id*.

    Returns ([], None) if the file doesn't exist yet (new email config).
    Returns (ids_list, oldest_iso_str | None) otherwise.
    """
    p = _proc_ids_path(config_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected dict")
        ids    = data.get("ids", [])
        oldest = data.get("oldest")
        # Validate: list of strings, bounded size
        if not isinstance(ids, list):
            raise ValueError("ids must be a list")
        ids = [str(x)[:1000] for x in ids
               if isinstance(x, str)][:EMAIL_MAX_IDS + 100]
        return ids, oldest
    except FileNotFoundError:
        return [], None
    except Exception as e:
        log.warning(f"processed_ids file for {config_id} is corrupt ({e}), resetting")
        try: p.unlink(missing_ok=True)
        except Exception: pass
        return [], None


def _proc_ids_save(config_id: str, ids: list, oldest: str | None) -> None:
    """Atomically save processed IDs to the dedicated file."""
    p   = _proc_ids_path(config_id)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"ids": ids, "oldest": oldest},
                                  ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        log.warning(f"Could not save processed_ids for {config_id}: {e}")


def _proc_ids_delete(config_id: str) -> None:
    """Delete the dedicated processed-IDs file when an email config is removed."""
    p = _proc_ids_path(config_id)
    try:
        p.unlink(missing_ok=True)
        # Remove any leftover .tmp from an interrupted write
        p.with_suffix(".tmp").unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"Could not delete processed_ids file for {config_id}: {e}")


def _update_email_config(config_id: str, updates: dict):
    """Apply `updates` (top-level dict.update) to the email configuration with
    id `config_id` and persist the result. Used by IMAP retrieval to update
    processed_ids/processed_ids_oldest/polling_blocked in the course of a
    retrieval cycle. Returns the full list of email configurations after the
    update (unchanged if `config_id` is not found)."""
    cfg     = get_config()
    configs = cfg.get("email_configs", [])
    for ec in configs:
        if ec.get("id") == config_id:
            ec.update(updates)
            break
    update_config({"email_configs": configs})
    return configs


def _email_check_limit(email_cfg: dict) -> bool:
    """Check safety limits for the IMAP retrieval of a given email configuration.

    Two independent guards, either of which is sufficient to block retrieval:
      - EMAIL_MAX_IDS: number of Message-IDs retained in `processed_ids`
        (prevents unbounded growth if the server never deletes/marks messages);
      - EMAIL_MAX_DAYS: age of the oldest retained message
        (`processed_ids_oldest`), useful when the configured action is
        "ignore" and the same old messages keep appearing.

    If a limit is reached and the configuration is not already marked
    `polling_blocked`, logs the event (log.email_blocked) and persists
    `polling_blocked: True` via _update_email_config — retrieval for this
    configuration is then skipped (_imap_poll) until the user clicks
    "Reset IDs" in the interface.

    Returns True if retrieval should be blocked for this configuration."""
    from datetime import datetime, timezone
    ids     = email_cfg.get("processed_ids", [])
    oldest  = email_cfg.get("processed_ids_oldest")
    blocked = False
    reason  = None
    if len(ids) >= EMAIL_MAX_IDS:
        blocked = True
        reason  = t("log.email_limit_ids", count=len(ids), limit=EMAIL_MAX_IDS)
    if oldest and not blocked:
        try:
            oldest_dt = datetime.fromisoformat(oldest)
            if oldest_dt.tzinfo is None:
                oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - oldest_dt).days
            if age_days >= EMAIL_MAX_DAYS:
                blocked = True
                reason  = t("log.email_limit_age", days=age_days, limit=EMAIL_MAX_DAYS)
        except Exception:
            pass
    if blocked and not email_cfg.get("polling_blocked"):
        name = email_cfg.get("name", "?")
        last = email_cfg.get("processed_ids_oldest", "unknown")
        msg  = ("Email polling [" + name + "] blocked — " + str(reason) +
                ". Oldest processed message: " + str(last) +
                ". Use the Reset IDs button in the email configuration.")
        log.error(msg)
        log_event("error", t("log.email_blocked", name=name, reason=reason, last=last))
        _update_email_config(email_cfg.get("id"), {"polling_blocked": True})
    return blocked


_email_last_poll = {}  # {config_id: unix timestamp of last poll}


def _imap_poll():
    """Background loop run by the "imap-poller" thread (started by start_email_poller).

    Every 30 seconds (_email_stop.wait(30)), iterates over enabled email
    configurations and calls _imap_process for each one whose poll_interval
    (in minutes, minimum 60 s) has elapsed since the last pass
    (_email_last_poll).

    A configuration marked polling_blocked (see _email_check_limit) is
    skipped with an error log until the user resets its processed IDs via
    the web interface. Any exception raised by _imap_process is caught and
    logged without interrupting the loop or affecting other configurations.

    Stops when _email_stop is set (stop_email_poller, called by
    _restart_watcher on an email configuration change)."""
    while not _email_stop.is_set():
        cfg     = get_config()
        configs = cfg.get("email_configs", [])
        now     = time.time()
        for ec in configs:
            if not ec.get("enabled"):
                continue
            cid      = ec.get("id")
            interval = max(60, ec.get("poll_interval", 5) * 60)
            last     = _email_last_poll.get(cid, 0)
            if now - last < interval:
                continue
            _email_last_poll[cid] = now
            if ec.get("polling_blocked"):
                log_event("error", t("log.email_polling_disabled", name=ec.get("name", "?")))
                continue
            try:
                _imap_process(ec)
            except Exception as e:
                log_event("error", t("log.email_polling_error", name=ec.get("name", "?"), message=e))
        _email_stop.wait(30)


def _imap_process(email_cfg: dict):
    """Run one IMAP polling cycle for the given email configuration.

    Connects to the server (IMAP4_SSL or plain IMAP4; verify_ssl controls
    certificate validation), iterates over ALL messages in the configured
    folder (default: INBOX), and for each message whose Message-ID (or a
    fallback header hash) is not yet in processed_ids:

      1. applies the optional filter_from / filter_subject filters
         (substring, case-insensitive). A message that does not match is
         skipped but NOT marked as processed (it will be re-examined on
         the next cycle).
      2. extracts all PDF attachments (MIME type application/pdf or filename
         ending in .pdf), writes them to /data/input/ with a sanitised name
         (non-alphanumeric characters replaced, numeric suffix on collision).
      3. if default_trigger is set, registers a filename → trigger mapping
         in _email_triggers (read by process_file for PDFs received by email
         that contain no barcode).
      4. applies the configured action: "read" (sets \\Seen), "delete"
         (sets \\Deleted then expunges), "ignore" (no IMAP action).
      5. appends the Message-ID to processed_ids and initialises
         processed_ids_oldest on the first processed message.

    On exit (including errors, via finally → M.logout()), persists
    processed_ids / processed_ids_oldest via _update_email_config, then
    calls _email_check_limit to detect safety-limit breaches.

    Does not raise on connection/search errors (logged; returns silently).
    Per-message exceptions are not caught here — an exception aborts the
    cycle for that configuration and is caught by the caller _imap_poll."""
    import ssl as _ssl
    from datetime import datetime, timezone
    from email.header import decode_header as _dec_hdr

    cid         = email_cfg.get("id", "")
    name        = email_cfg.get("name", "?")
    host        = email_cfg.get("host", "")
    port        = int(email_cfg.get("port", 993))
    username    = email_cfg.get("username", "")
    password    = decrypt_password(email_cfg.get("password_enc", ""))
    folder      = email_cfg.get("folder", "INBOX")
    verify_ssl  = email_cfg.get("verify_ssl", True)
    use_ssl     = email_cfg.get("use_ssl", True)  # False = plain IMAP (no TLS)
    action      = email_cfg.get("action", "read")
    f_from      = email_cfg.get("filter_from", "").lower().strip()
    f_subj      = email_cfg.get("filter_subject", "").lower().strip()
    def_trigger = email_cfg.get("default_trigger")
    processed, oldest = _proc_ids_load(cid)
    processed_set = set(processed)   # O(1) lookup

    if not host or not username or not password:
        log_event("warning", t("log.email_config_incomplete", name=name))
        return

    log_event("info", t("log.email_polling_start", name=name, username=username, host=host, port=port, folder=folder), verbose=True)

    ctx = _ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode    = _ssl.CERT_NONE

    # Use the username as-is — some servers expect user@domain, others just user
    login_user = username
    try:
        if use_ssl:
            M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=15)
        else:
            M = imaplib.IMAP4(host, port)
            try: M.socket().settimeout(15)
            except Exception: pass
        M.login(login_user, password)
    except Exception as e:
        log_event("error", t("log.email_connection_failed", name=name, message=e))
        return

    try:
        M.select(folder)
        status, data = M.search(None, "UNSEEN")
        if status != "OK":
            log_event("warning", t("log.email_search_failed", name=name), verbose=True)
            return

        uids    = data[0].split()
        found   = 0
        saved   = 0
        skipped = 0

        for uid in uids:
            _, msg_data = M.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = _email_mod.message_from_bytes(raw)
            msg_id = msg.get("Message-ID", "").strip()
            if not msg_id:
                msg_id = hashlib.md5(raw[:200]).hexdigest()

            if msg_id in processed_set:
                skipped += 1
                continue

            from_addr = msg.get("From", "").lower()
            subject   = ""
            raw_subj  = msg.get("Subject", "")
            for part, enc in _dec_hdr(raw_subj):
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="replace")
                else:
                    subject += part
            subject = subject.lower()

            if f_from and f_from not in from_addr:
                # Mark as processed to avoid re-downloading on every poll cycle.
                # The email intentionally filtered; processed_ids prevents it
                # from being re-fetched (critical when action="ignore" or IMAP
                # "UNSEEN" search is used, since the server flag is never set).
                processed.append(msg_id)
                processed_set.add(msg_id)
                if oldest is None:
                    oldest = datetime.now(timezone.utc).isoformat()
                skipped += 1
                continue
            if f_subj and f_subj not in subject:
                processed.append(msg_id)
                processed_set.add(msg_id)
                if oldest is None:
                    oldest = datetime.now(timezone.utc).isoformat()
                skipped += 1
                continue

            pdfs_found = 0
            for part in msg.walk():
                ct    = part.get_content_type()
                fname = part.get_filename()
                if ct == "application/pdf" or (fname and fname.lower().endswith(".pdf")):
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    safe_name = re.sub(r"[^\w\-. ]", "_", fname or "email.pdf")
                    if not safe_name.lower().endswith(".pdf"):
                        safe_name += ".pdf"
                    dest = INPUT_DIR / safe_name
                    ctr = 1
                    while dest.exists():
                        dest = INPUT_DIR / (Path(safe_name).stem + "_" + str(ctr).zfill(3) + ".pdf")
                        ctr += 1
                    # Size guard before writing to disk
                    _att_mb = len(payload) / (1024 * 1024)
                    if _att_mb > MAX_UPLOAD_MB:
                        log_event("warning",
                            t("log.file_too_large",
                              filename=safe_name,
                              size_mb=f"{_att_mb:.1f}",
                              max_mb=str(MAX_UPLOAD_MB)))
                        continue
                    # Populate trigger BEFORE writing the file so the watchdog
                    # can never fire on_created before _email_triggers is set.
                    if def_trigger:
                        with _email_triggers_lock:
                            _email_triggers[dest.name] = def_trigger
                    try:
                        dest.write_bytes(payload)
                    except Exception:
                        # Write failed — remove the pre-populated trigger to
                        # avoid a dangling entry for a file that never appeared.
                        if def_trigger:
                            with _email_triggers_lock:
                                _email_triggers.pop(dest.name, None)
                        raise
                    try: os.chmod(dest, 0o664)
                    except: pass
                    log_event("info", t("log.email_pdf_received", name=name, sender=msg.get("From","?"), filename=safe_name))
                    pdfs_found += 1
                    saved += 1

            if pdfs_found == 0:
                log_event("info", t("log.email_no_pdf", name=name, sender=msg.get("From","?")), verbose=True)

            found += 1
            if action == "read":
                M.store(uid, "+FLAGS", "\\Seen")
            elif action == "delete":
                M.store(uid, "+FLAGS", "\\Deleted")
                M.expunge()

            processed.append(msg_id)
            processed_set.add(msg_id)
            if oldest is None:
                oldest = datetime.now(timezone.utc).isoformat()

        _poll_verbose = (found == 0 and saved == 0)
        log_event("info", t("log.email_polling_done", name=name, found=found, saved=saved, skipped=skipped), verbose=_poll_verbose)

    finally:
        try: M.logout()
        except: pass

    # Save processed IDs to dedicated file (not into the main config JSON)
    _proc_ids_save(cid, processed, oldest)
    # Update in-memory config cache so api_state / _email_check_limit see fresh data
    _update_email_config(cid, {
        "processed_ids":        processed,
        "processed_ids_oldest": oldest,
    })
    merged_ec = dict(email_cfg)
    merged_ec["processed_ids"]        = processed
    merged_ec["processed_ids_oldest"] = oldest
    _email_check_limit(merged_ec)


def start_email_poller():
    """Demarre (ou redemarre) le thread de releve IMAP _imap_poll en arriere-
    plan. Appelee au demarrage de l'application et apres tout changement de
    configuration email (via _restart_watcher)."""
    global _email_thread, _email_stop
    _email_stop.clear()
    _email_thread = threading.Thread(target=_imap_poll, daemon=True, name="imap-poller")
    _email_thread.start()
    log.info("IMAP poller thread started.")


def stop_email_poller():
    """Signal the _imap_poll thread to stop after the current cycle.

    Sets the _email_stop Event, which is awaited by _email_stop.wait(30)
    inside the polling loop."""
    _email_stop.set()

