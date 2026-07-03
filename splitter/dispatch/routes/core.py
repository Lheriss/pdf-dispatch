"""
dispatch/routes/core.py
Core blueprints: SPA, activity log, stats, dirs, config, recent, state.
"""
import json
import os
import re
import secrets
import time
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from dispatch.config import (
    APP_VERSION, BARCODE_DPI_SCAN, CONFIG_DEFAULTS, DATA_DIR,
    DEFAULT_LANGUAGE, DPI, ERROR_DIR, INPUT_DIR, OUTPUT_DIR,
    PROCESSED_DIR, SCANNER, SSRF_PROTECTION, UPSCALE,
    _safe_relative_path, _save_stats,
    get_config, get_dirs, update_config, update_dir_paths,
)
from dispatch.email_poller import start_email_poller
from dispatch.i18n import DEFAULT_LANGUAGE, TRANSLATIONS, t
from dispatch.processing import build_filename, validate_filename_tokens
from dispatch.state import log_event, state, state_lock
from dispatch.watcher import _restart_watcher

bp = Blueprint("core", __name__)

@bp.route("/")
def index():
    """Main page (SPA): serves templates/index.html with translations for the
    active language injected into window.I18N (client-side)."""
    lang = get_config().get("language", DEFAULT_LANGUAGE)
    i18n_json = json.dumps(TRANSLATIONS.get(lang, TRANSLATIONS.get(DEFAULT_LANGUAGE, {})), ensure_ascii=False)
    return render_template("index.html", lang=lang, i18n_json=i18n_json)




@bp.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    """POST /api/stats/reset — reset persistent statistics to zero
    (processed, split_docs, errors, last_file, last_time), both in memory
    (state["stats"]) and on disk (via _save_stats). Does not affect the
    configuration or the activity log."""
    empty = {"processed": 0, "split_docs": 0, "errors": 0, "last_file": None, "last_time": None}
    with state_lock:
        state["stats"] = dict(empty)
    _save_stats(empty)
    log_event("info", t("log.stats_reset"))
    return jsonify({"ok": True})




@bp.route("/api/log", methods=["POST"])
def api_log():
    """POST /api/log — append an entry to the activity log from the frontend.

    JSON body: {"level": "info"|"warning"|"error", "message": "..."}.
    Used by the frontend to log its own actions (parameter change,
    trigger add/remove, etc.). The message is already translated
    client-side (via t()) at call time and is logged as-is. Level falls
    back to "info" if absent or invalid; message is truncated to 500
    characters as a safeguard."""
    data  = request.get_json(force=True)
    level = data.get("level", "info")
    if level not in ("info", "warning", "error"):
        level = "info"
    # G — strip control chars (CRLF, ANSI escapes) to prevent log injection
    message = re.sub(r'[\x00-\x1f\x7f]', ' ', str(data.get("message", "")))[:500]
    log_event(level, message)
    return jsonify({"ok": True})




@bp.route("/api/dirs/rename", methods=["POST"])
def api_dirs_rename():
    """Rename a folder in the configuration and on the filesystem."""
    global INPUT_DIR, OUTPUT_DIR, ERROR_DIR, PROCESSED_DIR
    from flask import request as req
    data     = req.get_json(force=True)
    key      = data.get("key", "")
    new_path = data.get("path", "").strip().strip("/")

    if key not in ("input", "output", "error", "processed", "no_code"):
        return jsonify({"ok": False, "error": t("dirs.error_unknown_key")}), 400

    # Security validation
    ok, err = _safe_relative_path(new_path)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    cfg       = get_config()
    dirs      = cfg.get("dirs", dict(CONFIG_DEFAULTS["dirs"]))
    old_path  = dirs.get(key, "")
    old_abs   = DATA_DIR / old_path
    new_abs   = DATA_DIR / new_path

    # Check for duplicate paths
    for k, v in dirs.items():
        if k != key and v == new_path:
            return jsonify({"ok": False,
                "error": t("dirs.error_path_in_use", key=k)}), 400

    # Check that no processing is in progress
    with state_lock:
        if state["queue"]:
            return jsonify({"ok": False,
                "error": t("dirs.error_processing_in_progress")}), 409

    # Check the target folder does not already exist
    if new_abs.exists() and new_abs != old_abs:
        return jsonify({"ok": False,
            "error": t("dirs.error_already_exists", path=new_path)}), 400

    # Prevent output folders from being placed inside the input folder
    if key != "input":
        input_abs = DATA_DIR / dirs.get("input", "input")
        try:
            new_abs.resolve().relative_to(input_abs.resolve())
            return jsonify({"ok": False,
                "error": t("dirs.error_inside_input")}), 400
        except ValueError:
            pass

    # Rename on the filesystem when the old path exists
    if old_abs.exists():
        old_abs.rename(new_abs)
        try: os.chmod(new_abs, 0o777)
        except: pass
    else:
        new_abs.mkdir(parents=True, exist_ok=True)
        try: os.chmod(new_abs, 0o777)
        except: pass

    # Persist the new path in the config
    dirs[key] = new_path
    update_config({"dirs": dirs})
    update_dir_paths()

    # If it is the input folder, restart the monitor
    if key == "input":
        log_event("info", t("log.input_dir_renamed", path=new_path))
        _restart_watcher()

    log_event("info", t("log.dir_renamed", key=key, old=old_path, new=new_path))
    return jsonify({"ok": True, "dirs": dirs})




@bp.route("/api/dirs/recreate", methods=["POST"])
def api_dirs_recreate():
    """POST /api/dirs/recreate - recree un dossier de sortie manquant
    (signale `exists: false` dans /api/state -> dirs_status, par exemple
    apres une suppression manuelle sur le NAS).

    Corps JSON : {"key": "input"|"output"|"error"|"processed"|"no_code"}.
    Cree le dossier (mkdir -p) avec les permissions 0o777 (acces large
    necessaire pour les ACL Synology) et journalise l'operation. 400 si
    `key` is not a known directory alias."""
    from flask import request as req
    data = req.get_json(force=True)
    key  = data.get("key", "")
    dirs = get_config().get("dirs", dict(CONFIG_DEFAULTS["dirs"]))
    if key not in dirs:
        return jsonify({"ok": False, "error": t("dirs.error_unknown_key_short")}), 400
    d = DATA_DIR / dirs[key]
    d.mkdir(parents=True, exist_ok=True)
    try: os.chmod(d, 0o777)
    except: pass
    log_event("info", t("log.dir_recreated", folder=key, path=d))
    return jsonify({"ok": True})




@bp.route("/api/settings/regenerate-api-key", methods=["POST"])
def api_regenerate_api_key():
    """POST /api/settings/regenerate-api-key — Generate a new API key.

    Only works when the key is stored in config (not when set via API_KEY
    env var). Returns the new key so the UI can display it immediately.
    """
    if os.getenv("API_KEY", "").strip():
        return jsonify({"ok": False,
                        "error": "API key is set via API_KEY env var; regeneration disabled."}), 400
    new_key = secrets.token_hex(32)
    update_config({"api_key": new_key})
    log_event("info", t("apikey.log_regenerated"))
    return jsonify({"ok": True, "key": new_key})




@bp.route("/api/recent")
def api_recent():
    """GET /api/recent[?n=20] — List recently produced output files.

    Scans OUTPUT_DIR recursively (excluding /error and /processed sub-dirs)
    for PDF files and returns them sorted by modification time, newest first.
    The `n` query parameter caps the list (max 100, default 20).

    Each entry: {filename, path (relative to DATA_DIR), size_bytes, modified (ISO 8601)}
    """
    try:
        n = min(int(request.args.get("n", 20)), 100)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "n must be an integer"}), 400
    files = []
    try:
        exclude = {str(ERROR_DIR), str(PROCESSED_DIR)}
        for p in OUTPUT_DIR.rglob("*.pdf"):
            # Skip error and processed sub-directories
            if any(str(p).startswith(e) for e in exclude):
                continue
            st  = p.stat()
            rel = str(p.relative_to(DATA_DIR))
            files.append({
                "filename":     p.name,
                "path":         rel,
                "download_url": f"/api/file/{rel}",
                "size_bytes":   st.st_size,
                "modified":     datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"ok": True, "files": files[:n], "total": len(files)})




def _sanitize_config_for_client(cfg: dict) -> dict:
    """Copie de la config sans donnees sensibles avant envoi au navigateur :
    jamais de mot de passe chiffre, jamais la liste complete des Message-IDs traites.
    Ajoute _api_key_env_set pour que le frontend sache si l'env var API_KEY est active."""
    import copy as _copy
    sanitized = _copy.deepcopy(cfg)
    for ec in sanitized.get("email_configs", []):
        ec.pop("password_enc", None)
        ec.pop("processed_ids", None)
    # Expose whether API_KEY is set via env var (key itself is already included)
    sanitized["_api_key_env_set"] = bool(os.getenv("API_KEY", "").strip())
    # If env var is set, show its value rather than the config value
    if sanitized["_api_key_env_set"]:
        sanitized["api_key"] = os.getenv("API_KEY", "")
    return sanitized



@bp.route("/api/state")
def api_state():
    """GET /api/state - point d'entree principal consomme par le frontend,
    interroge toutes les 3 secondes (auto-refresh) et au chargement de la
    page.

    Retourne en un seul appel tout ce dont l'interface a besoin pour se
    mettre a jour :
      - "stats" : statistiques persistantes (traites, documents produits,
        erreurs, dernier fichier) ;
      - "events" : les MAX_LOG_ENTRIES dernieres entrees du journal
        d'activite (deja traduites a l'ecriture, voir log_event) ;
      - "queue" : noms des fichiers actuellement en cours de traitement ;
      - "config" : informations techniques en lecture seule (chemins
        absolus, scanner de codes-barres, version de l'application, statut
        d'existence de chaque dossier, et un resume des configurations email
        - sans secrets - pour afficher les indicateurs de blocage/activation) ;
      - "app_config" : la configuration utilisateur complete (declencheurs,
        options, dossiers, tokens de nommage, configurations email),
        nettoyee des donnees sensibles par _sanitize_config_for_client avant
        envoi au navigateur."""
    cfg = get_config()
    with state_lock:
        return jsonify({
            "stats":      dict(state["stats"]),
            "events":     list(state["events"]),
            "queue":      list(state["queue"].keys()),
            "config": {
                "data_dir":      str(DATA_DIR),
                "input_dir":     str(INPUT_DIR),
                "output_dir":    str(OUTPUT_DIR),
                "error_dir":     str(ERROR_DIR),
                "processed_dir": str(PROCESSED_DIR),
                "scanner":       SCANNER,
                "dpi":           DPI,
                "upscale":       UPSCALE,
                "version":       os.getenv("APP_VERSION", "1.8.1"),
                "dirs_status":   {k: {"path": str(v), "exists": v.exists()}
                                  for k, v in get_dirs().items()},
                "email_configs_status": [
                    {
                        "id":                   ec.get("id"),
                        "name":                 ec.get("name", "?"),
                        "enabled":              ec.get("enabled", False),
                        "polling_blocked":      ec.get("polling_blocked", False),
                        "processed_ids_count":  len(ec.get("processed_ids", [])),
                        "processed_ids_oldest": ec.get("processed_ids_oldest"),
                    }
                    for ec in get_config().get("email_configs", [])
                ],
            },
            "app_config": _sanitize_config_for_client(cfg),
        })




@bp.route("/api/config", methods=["POST"])
def api_config_update():
    """POST /api/config - met a jour un ou plusieurs parametres de
    configuration en une seule fois.

    Corps JSON : un dict fusionne au premier niveau dans la configuration
    persistante via update_config (ex: {"language": "en"},
    {"subdirs_by_trigger": true}, {"split_values": [...]},
    {"filename_tokens": [...]}). Pour modifier une sous-cle d'un dict
    imbrique (par exemple "dirs"), le frontend doit fournir le dict complet
    mis a jour - update_config ne fusionne pas recursivement. Retourne la
    configuration complete resultante (non assainie : reservee aux appels
    it has just saved)."""
    data = request.get_json(force=True)

    # ── Security: keys managed by dedicated endpoints cannot be set here ──
    _BLOCKED = frozenset({"email_configs", "stats", "counter",
                           "processed_ids", "processed_ids_oldest"})
    blocked = sorted(k for k in data if k in _BLOCKED)
    if blocked:
        return jsonify({
            "ok": False,
            "error": (
                "The following key(s) cannot be set via POST /api/config "
                f"(use the dedicated endpoint): {', '.join(blocked)}"
            ),
        }), 400

    # ── Security: validate dirs paths to prevent filesystem escape ──────
    if "dirs" in data:
        dirs = data["dirs"]
        if not isinstance(dirs, dict):
            return jsonify({"ok": False, "error": "dirs must be an object"}), 400
        _valid_dir_keys = {"input", "output", "error", "processed", "no_code"}
        for dir_key, path_val in dirs.items():
            if dir_key not in _valid_dir_keys:
                return jsonify({"ok": False,
                                "error": f"Unknown dirs key: {dir_key!r}"}), 400
            ok_path, err_path = _safe_relative_path(str(path_val).strip())
            if not ok_path:
                return jsonify({"ok": False,
                                "error": f"dirs.{dir_key}: {err_path}"}), 400

    cfg = update_config(data)
    return jsonify({"ok": True, "config": cfg})




@bp.route("/api/config/validate_tokens", methods=["POST"])
def api_validate_tokens():
    """POST /api/config/validate_tokens - valide une liste de tokens de
    nommage de fichier sans les enregistrer (appele a chaque modification du
    constructeur de nom de fichier, avant l'enregistrement effectif).

    Corps JSON : {"tokens": [...]} (meme format que filename_tokens, voir
    build_filename). Delegue a validate_filename_tokens et retourne
    {"ok": true} ou {"ok": false, "error": "<message traduit>"}."""
    tokens = request.get_json(force=True).get("tokens", [])
    ok, err = validate_filename_tokens(tokens)
    return jsonify({"ok": ok, "error": err})


# ---------------------------------------------------------------------------
