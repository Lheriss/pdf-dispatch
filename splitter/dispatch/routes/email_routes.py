"""
dispatch/routes/email_routes.py
Blueprints: IMAP email configuration management.
"""
import imaplib
import re
import uuid

from flask import Blueprint, jsonify, request

from dispatch.config import get_config, log, update_config
from dispatch.crypto import decrypt_password, encrypt_password
from dispatch.email_poller import (
    _email_config_signature, _email_find_duplicate,
    _email_find_name_conflict, _proc_ids_delete,
    _proc_ids_load, _proc_ids_save,
)
from dispatch.i18n import t
from dispatch.state import log_event
from dispatch.webhook import _ssrf_blocked_response, _ssrf_safe

bp = Blueprint("email_routes", __name__)

def _validate_imap_field(field_name, value):
    """Return an error string if value contains CRLF chars, else None."""
    if value and ("\r" in value or "\n" in value):
        return "%s: CRLF characters are not allowed" % field_name
    return None




def _validate_email_config_fields(data):
    """Validate IMAP fields and numeric bounds. Returns list of error strings."""
    errors = []
    # host is mandatory — an IMAP config without a hostname is unusable
    host = (data.get("host") or "").strip()
    if not host:
        errors.append("host: is required")
    for field in ("host", "username", "folder"):
        val = (data.get(field) or "").strip()
        err = _validate_imap_field(field, val)
        if err:
            errors.append(err)
    try:
        port = int(data.get("port", 993))
        if not (1 <= port <= 65535):
            errors.append("port: must be between 1 and 65535 (got %d)" % port)
    except (ValueError, TypeError):
        errors.append("port: must be an integer")
    try:
        interval = int(data.get("poll_interval", 5))
        if interval < 1:
            errors.append("poll_interval: must be >= 1 (got %d)" % interval)
    except (ValueError, TypeError):
        errors.append("poll_interval: must be an integer")
    return errors




@bp.route("/api/email/configs", methods=["POST"])
def api_email_configs_create():
    """Create a new email polling configuration."""
    data    = request.get_json(force=True)
    cfg     = get_config()
    configs = cfg.get("email_configs", [])

    field_errors = _validate_email_config_fields(data)
    if field_errors:
        return jsonify({"ok": False, "error": "; ".join(field_errors)}), 400

    new_ec = {
        "id":                   uuid.uuid4().hex[:12],
        "name":                 (data.get("name") or "").strip() or ("Config " + str(len(configs) + 1)),
        "enabled":              bool(data.get("enabled", False)),
        "host":                 (data.get("host") or "").strip(),
        "port":                 int(data.get("port", 993) or 993),
        "username":             (data.get("username") or "").strip(),
        "password_enc":         "",
        "folder":               (data.get("folder") or "INBOX").strip() or "INBOX",
        "poll_interval":        int(data.get("poll_interval", 5) or 5),
        "filter_from":          (data.get("filter_from") or "").strip(),
        "filter_subject":       (data.get("filter_subject") or "").strip(),
        "action":               data.get("action", "read"),
        "verify_ssl":           bool(data.get("verify_ssl", True)),
        "use_ssl":              bool(data.get("use_ssl", True)),
        "default_trigger":      data.get("default_trigger") or None,
        "processed_ids":        [],
        "processed_ids_oldest": None,
        "polling_blocked":      False,
    }
    if data.get("password"):
        new_ec["password_enc"] = encrypt_password(data["password"])

    name_dup = _email_find_name_conflict(configs, new_ec["name"])
    if name_dup:
        return jsonify({"ok": False, "error": t("email.error_duplicate_name", name=new_ec["name"])}), 400

    sig = _email_config_signature(new_ec)
    dup = _email_find_duplicate(configs, sig)
    if dup:
        return jsonify({"ok": False, "error": t("email.error_duplicate_config", name=dup.get("name", "?"))}), 400

    # Defense in depth: delete any pre-existing processed_ids file for this id.
    # uuid4().hex[:12] is random (48-bit) so collisions are astronomically
    # unlikely, but an explicit purge eliminates any injection risk.
    _proc_ids_delete(new_ec["id"])
    configs.append(new_ec)
    update_config({"email_configs": configs})
    log_event("info", t("log.email_config_created", name=new_ec["name"]))
    _resp_ec = {k: v for k, v in new_ec.items()
                if k not in ("password_enc", "processed_ids", "processed_ids_oldest")}
    return jsonify({"ok": True, "config": _resp_ec})




@bp.route("/api/email/configs/<config_id>", methods=["POST"])
def api_email_configs_update(config_id):
    """Update an existing email configuration (including renaming)."""
    data    = request.get_json(force=True)
    cfg     = get_config()
    configs = cfg.get("email_configs", [])
    ec      = next((c for c in configs if c.get("id") == config_id), None)
    if ec is None:
        return jsonify({"ok": False, "error": t("email.error_not_found")}), 404

    # C+E: validate CRLF and numeric bounds on incoming data
    field_errors = _validate_email_config_fields(data)
    if field_errors:
        return jsonify({"ok": False, "error": "; ".join(field_errors)}), 400

    updated = dict(ec)
    for k in ("name", "enabled", "host", "port", "username", "folder", "poll_interval",
              "filter_from", "filter_subject", "action", "verify_ssl", "use_ssl", "default_trigger"):
        if k in data:
            updated[k] = data[k]
    updated["name"]            = (updated.get("name") or "").strip() or ec.get("name", "Config")
    updated["host"]            = (updated.get("host") or "").strip()
    updated["username"]        = (updated.get("username") or "").strip()
    updated["folder"]          = (updated.get("folder") or "INBOX").strip() or "INBOX"
    updated["filter_from"]     = (updated.get("filter_from") or "").strip()
    updated["filter_subject"]  = (updated.get("filter_subject") or "").strip()
    updated["port"]            = int(updated.get("port", 993) or 993)
    updated["poll_interval"]   = int(updated.get("poll_interval", 5) or 5)

    if data.get("password"):
        updated["password_enc"] = encrypt_password(data["password"])

    name_dup = _email_find_name_conflict(configs, updated["name"], exclude_id=config_id)
    if name_dup:
        return jsonify({"ok": False, "error": t("email.error_duplicate_name", name=updated["name"])}), 400

    sig = _email_config_signature(updated)
    dup = _email_find_duplicate(configs, sig, exclude_id=config_id)
    if dup:
        return jsonify({"ok": False, "error": t("email.error_duplicate_config", name=dup.get("name", "?"))}), 400

    for i, c in enumerate(configs):
        if c.get("id") == config_id:
            configs[i] = updated
            break
    update_config({"email_configs": configs})
    log_event("info", t("log.email_config_updated", name=updated["name"]))
    _resp_updated = {k: v for k, v in updated.items()
                     if k not in ("password_enc", "processed_ids", "processed_ids_oldest")}
    return jsonify({"ok": True, "config": _resp_updated})




@bp.route("/api/email/configs/<config_id>", methods=["DELETE"])
def api_email_configs_delete(config_id):
    """Supprime une configuration de polling email."""
    cfg     = get_config()
    configs = cfg.get("email_configs", [])
    ec      = next((c for c in configs if c.get("id") == config_id), None)
    if ec is None:
        return jsonify({"ok": False, "error": t("email.error_not_found")}), 404
    configs = [c for c in configs if c.get("id") != config_id]
    update_config({"email_configs": configs})
    _proc_ids_delete(config_id)   # remove dedicated processed-IDs file
    log_event("info", t("log.email_config_deleted", name=ec.get("name", "?")))
    return jsonify({"ok": True})




@bp.route("/api/email/test", methods=["POST"])
def api_email_test():
    """POST /api/email/test - teste une connexion IMAP sans sauvegarder la
    configuration (bouton "Tester la connexion" du panneau email).

    Corps JSON attendu : host, port, username, verify_ssl, folder, et soit
    `password` (mot de passe saisi en clair par l'utilisateur, pour une
    nouvelle configuration ou un changement de mot de passe), soit `id`
    (identifiant d'une configuration existante - le mot de passe chiffre
    est alors dechiffre cote serveur, jamais transmis au navigateur).

    Se connecte en IMAP4_SSL, selectionne `folder` et compte les messages.
    Journalise le resultat (succes ou echec, avec le detail technique cote
    journal applicatif Python pour le debogage) dans le journal d'activite
    de l'interface. Repond toujours HTTP 200 : `{"ok": true, "message": ...}`
    en cas de succes, `{"ok": false, "message": "<TypeException>: <detail>"}`
    en cas d'echec (le frontend affiche ce message tel quel)."""
    import ssl as _ssl
    data       = request.get_json(force=True)
    name       = data.get("name", "?")
    host       = data.get("host", "")
    port       = int(data.get("port", 993))
    username   = data.get("username", "")
    verify_ssl = data.get("verify_ssl", True)
    use_ssl    = bool(data.get("use_ssl", True))
    folder     = data.get("folder", "INBOX")

    # Password: use the plaintext value if supplied (user input);
    # otherwise retrieve it server-side from the stored config by id
    # (the encrypted password is never sent to the browser).
    password = data.get("password", "")
    if not password:
        config_id = data.get("id")
        if config_id:
            stored = next((c for c in get_config().get("email_configs", [])
                            if c.get("id") == config_id), None)
            if stored:
                password = decrypt_password(stored.get("password_enc", ""))

    log.info(f"IMAP test [{name}]: host={host} port={port} user={username} ssl={verify_ssl} folder={folder}")

    if not _ssrf_safe(host):
        log_event("warning", f"Test IMAP SSRF blocked (SSRF_PROTECTION=block): {host}")
        return _ssrf_blocked_response()

    try:
        ctx = _ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode    = _ssl.CERT_NONE
        log.info(f"IMAP test [{name}]: opening {'SSL' if use_ssl else 'plain'} connection to {host}:{port}...")
        if use_ssl:
            M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=10)
        else:
            M = imaplib.IMAP4(host, port)
            try: M.socket().settimeout(15)
            except Exception: pass
        log.info(f"IMAP test [{name}]: logging in as {username} (password {len(password)} chars)...")
        M.login(username, password)
        log.info(f"IMAP test [{name}]: login OK, selecting folder {folder}...")
        status, d2 = M.select(folder)
        count = int(d2[0]) if status == "OK" and d2 else 0
        M.logout()
        msg_ok = "IMAP test [" + name + "]: connection successful — " + str(count) + " message(s) in " + folder
        log.info(msg_ok)
        log_event("info", "📧 " + t("log.email_test_success", name=name, count=count, folder=folder))
        return jsonify({"ok": True, "message": t("email.test_success_message", count=count, folder=folder)})
    except Exception as e:
        msg_err = (f"IMAP test [{name}] FAILED: host={host}:{port} user={username} "
                   f"pwd_len={len(password)} ssl={verify_ssl} -> {type(e).__name__}: {e}")
        log.error(msg_err)
        log_event("error", "📧 " + t("log.email_test_failure", name=name, host=host, port=port,
                                       username=username, password_len=len(password),
                                       ssl=verify_ssl, error=f"{type(e).__name__}: {e}"))
        return jsonify({"ok": False, "error": str(type(e).__name__) + ": " + str(e)}), 502




@bp.route("/api/email/reset_ids/<config_id>", methods=["POST"])
def api_email_reset_ids(config_id):
    """POST /api/email/reset_ids/<config_id> - vide processed_ids et
    processed_ids_oldest pour la configuration email `config_id`, et leve le
    blocage polling_blocked s'il etait actif (cf. _email_check_limit).

    A utiliser apres avoir ajuste un filtre trop large ou apres une longue
    periode d'inactivite : les messages deja "vus" seront retelecharges au
    prochain cycle de polling (selon l'action configuree, ils pourraient
    donc etre re-traites). 404 si `config_id` est inconnu."""
    cfg     = get_config()
    configs = cfg.get("email_configs", [])
    ec      = next((c for c in configs if c.get("id") == config_id), None)
    if ec is None:
        return jsonify({"ok": False, "error": t("email.error_not_found")}), 404
    old_count = len(ec.get("processed_ids", []))
    # Also count from dedicated file (may differ from in-memory cache)
    _file_ids, _ = _proc_ids_load(config_id)
    old_count = max(old_count, len(_file_ids))
    ec["processed_ids"]        = []
    ec["processed_ids_oldest"] = None
    ec["polling_blocked"]      = False
    update_config({"email_configs": configs})
    _proc_ids_save(config_id, [], None)   # also reset dedicated file
    log_event("info", t("log.email_ids_reset", name=ec.get("name", "?"), count=old_count))
    return jsonify({"ok": True})



