"""
test_python_core.py
--------------------
Tests unitaires Python couvrant les fonctions qui ont ete source de bugs
en production. Chaque groupe de tests est relie a un incident specifique.

Bugs couverts :
  - Regex cassees dans _validate_and_sanitize_config (fix: 12beb3c)
  - FORBIDDEN_CHARS NameError et crash a l'import (fix: cfb1531)
  - Cle ephemere _get_email_secret sans cache (fix: 9c7cfa6)
  - Roundtrip Fernet (chiffrement mot de passe email)
  - TypeError validate_filename_tokens / build_filename (fix: 906e1a8)
  - Labels hardcodes FR dans generate_separator_pdf (fix: fdccc35)
"""

import importlib
import json
import os
import pathlib
import sys
import tempfile

# ── Setup : app.py a besoin de DATA_DIR et EMAIL_SECRET ─────────────────────
_TMPDIR = tempfile.mkdtemp(prefix="pdf_dispatch_test_")
os.environ.setdefault("DATA_DIR",     _TMPDIR)
os.environ.setdefault("EMAIL_SECRET", "a" * 64)

# Ajouter splitter/ au path pour l'import
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "splitter"))


def _import_app():
    """Importe (ou recharge) le module app. Un crash ici signale une
    regression au niveau du module init (FORBIDDEN_CHARS, CONFIG_DEFAULTS,
    etc.)."""
    if "app" in sys.modules:
        return sys.modules["app"]
    return importlib.import_module("app")


# ── Helpers ──────────────────────────────────────────────────────────────────
passed = failed = 0

def ok(label):
    global passed
    print(f"  ✓ {label}")
    passed += 1

def fail(label, detail=""):
    global failed
    msg = f"  ✗ {label}"
    if detail:
        msg += f"\n      {detail}"
    print(msg)
    failed += 1

def check(label, condition, detail=""):
    if condition:
        ok(label)
    else:
        fail(label, detail)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Import du module (regression : init order / NameError)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] Import du module (regression init order / NameError)")
try:
    app = _import_app()
    ok("import app sans exception")
    check("FORBIDDEN_CHARS defini", hasattr(app, "FORBIDDEN_CHARS"))
    check("CONFIG_DEFAULTS defini", hasattr(app, "CONFIG_DEFAULTS"))
except Exception as e:
    fail("import app", str(e))
    print("\nECHEC CRITIQUE : impossible de continuer sans le module.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _validate_and_sanitize_config (fix: regex cassees)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] _validate_and_sanitize_config")

fn = app._validate_and_sanitize_config

# 2a. Config valide → passee telle quelle
cfg_valid = {
    "filename_separator": "_",
    "counter": 5,
    "filename_tokens": [
        {"type": "trigger",   "enabled": True},
        {"type": "timestamp", "enabled": True, "format": "%Y%m%d"},
        {"type": "counter",   "enabled": True, "digits": 4},
    ],
    "dirs": {
        "input":     "input",
        "output":    "output",
        "error":     "output/error",
        "processed": "output/processed",
        "no_code":   "output/no_code",
    },
    "split_values": [{"value": "NEWDOC", "case_sensitive": True, "page_handling": "keep"}],
    "email_configs": [],
}
import copy
result = fn(copy.deepcopy(cfg_valid))
check("config valide : separator conserve",  result["filename_separator"] == "_")
check("config valide : counter conserve",    result["counter"] == 5)
check("config valide : 3 tokens conserves",  len(result["filename_tokens"]) == 3)

# 2b. Separator invalide → remplace par _
cfg_sep = copy.deepcopy(cfg_valid)
cfg_sep["filename_separator"] = ";"
result = fn(cfg_sep)
check("separator invalide ';' → '_'", result["filename_separator"] == "_")

# 2c. Counter negatif → 0
cfg_ctr = copy.deepcopy(cfg_valid)
cfg_ctr["counter"] = -1
result = fn(cfg_ctr)
check("counter negatif → 0", result["counter"] == 0)

# 2d. Caracteres de controle dans un token string → nettoyes (regex fixee)
cfg_ctrl = copy.deepcopy(cfg_valid)
cfg_ctrl["filename_tokens"].append({"type": "string", "enabled": True, "value": "ab\x01cd\x1fef"})
result = fn(cfg_ctrl)
string_token = next((t for t in result["filename_tokens"] if t["type"] == "string"), None)
check("caracteres de controle dans token string → nettoyes",
      string_token is not None and string_token["value"] == "abcdef")

# 2e. Chemin hors DATA_DIR → reset (securite path traversal)
cfg_trav = copy.deepcopy(cfg_valid)
cfg_trav["dirs"]["input"] = "../../etc"
result = fn(cfg_trav)
check("path traversal '../..' → reset au defaut",
      ".." not in result["dirs"].get("input", ""))

# 2f. Caracteres interdits dans le nom de chemin (regex r'[<>:"|?*]' fixee)
cfg_chars = copy.deepcopy(cfg_valid)
cfg_chars["dirs"]["output"] = 'out|put'
result = fn(cfg_chars)
check("caractere '|' interdit dans chemin → nettoye",
      "|" not in result["dirs"].get("output", "|"))

# 2g. digits counter hors plage → 6
cfg_digits = copy.deepcopy(cfg_valid)
cfg_digits["filename_tokens"][2]["digits"] = 99
result = fn(cfg_digits)
ctr_tok = next(t for t in result["filename_tokens"] if t["type"] == "counter")
check("digits=99 (hors plage 3-8) → 6", ctr_tok["digits"] == 6)

# 2h. Token de type inconnu → ignore
cfg_unk = copy.deepcopy(cfg_valid)
cfg_unk["filename_tokens"].append({"type": "unknown", "enabled": True})
result = fn(cfg_unk)
types = [t["type"] for t in result["filename_tokens"]]
check("token de type inconnu → ignore", "unknown" not in types)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Chiffrement email (fix: cle ephemere)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3] Chiffrement email")

# 3a. _get_email_secret() stable entre deux appels
app._EMAIL_SECRET_KEY = None  # reset du cache pour le test
key1 = app._get_email_secret()
key2 = app._get_email_secret()
check("_get_email_secret() retourne la meme cle (cache)", key1 == key2)
check("cle est de type bytes", isinstance(key1, bytes))
check("cle a la bonne longueur (44 chars base64url)", len(key1) == 44)

# 3b. Roundtrip Fernet
plain = "MonMotDePasse@123!"
enc   = app.encrypt_password(plain)
check("encrypt produit une chaine non vide", bool(enc))
check("encrypt produit du format Fernet (gAAAAA...)", enc.startswith("gAAAAA"))
dec = app.decrypt_password(enc)
check("roundtrip encrypt→decrypt", dec == plain)

# 3c. Deux chiffrements du meme texte → tokens differents (IV aleatoire)
enc2 = app.encrypt_password(plain)
check("deux chiffrements produisent des tokens differents (IV)", enc != enc2)
check("les deux se dechiffrent correctement", app.decrypt_password(enc2) == plain)

# 3d. Dechiffrement d'un token invalide → chaine vide ou None (pas d'exception)
result_invalid = app.decrypt_password("ceci_nest_pas_du_fernet")
check("token invalide → sans exception (retourne '' ou None)",
      result_invalid == "" or result_invalid is None)


# ─────────────────────────────────────────────────────────────────────────────
# 4. validate_filename_tokens + build_filename (fix: TypeError v1.14.0)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[4] validate_filename_tokens + build_filename")

from datetime import datetime as _dt

# 4a. Configuration valide
tokens_ok = [
    {"type": "trigger",   "enabled": True},
    {"type": "timestamp", "enabled": True, "format": "%Y%m%d"},
    {"type": "counter",   "enabled": True, "digits": 4},
]
ok_result, err = app.validate_filename_tokens(tokens_ok)
check("tokens valides → (True, '')", ok_result and err == "")

# 4b. Sans counter → erreur
tokens_no_ctr = [{"type": "trigger", "enabled": True}]
ok_result, err = app.validate_filename_tokens(tokens_no_ctr)
check("sans counter → (False, message)", not ok_result and len(err) > 0)

# 4c. Counter desactive → traite comme absent
tokens_ctr_off = [
    {"type": "trigger", "enabled": True},
    {"type": "counter", "enabled": False, "digits": 4},
]
ok_result, _ = app.validate_filename_tokens(tokens_ctr_off)
check("counter desactive → invalide (meme que absent)", not ok_result)

# 4d. digits hors plage → erreur
tokens_bad_digits = [
    {"type": "trigger", "enabled": True},
    {"type": "counter", "enabled": True, "digits": 99},
]
ok_result, err = app.validate_filename_tokens(tokens_bad_digits)
check("digits=99 → (False, message)", not ok_result and len(err) > 0)

# 4e. build_filename produit le nom attendu
now = _dt(2026, 6, 15, 10, 30, 0)
name = app.build_filename("NEWDOC", tokens_ok, counter=7, now=now, separator="_")
check("build_filename : contient le trigger",   "NEWDOC" in name)
check("build_filename : contient la date",      "20260615" in name)
check("build_filename : contient le compteur",  "0007" in name)

# 4f. build_filename avec caracteres interdits dans le trigger
name2 = app.build_filename("DOC<>?", tokens_ok, counter=1, now=now, separator="_")
check("build_filename : caracteres interdits dans trigger → remplaces", "<" not in name2 and ">" not in name2)


# ─────────────────────────────────────────────────────────────────────────────
# 5. generate_separator_pdf : labels traduits (fix: fdccc35)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[5] generate_separator_pdf — labels i18n")

cfg_path = pathlib.Path(_TMPDIR) / ".splitter_config.json"

try:
    from pypdf import PdfReader
    from io import BytesIO

    for lang, expected_sep, expected_trig in [
        ("fr", "SÉPARATEUR",  "DÉCLENCHEUR DE FRACTIONNEMENT"),
        ("en", "SEPARATOR",   "SPLIT TRIGGER"),
    ]:
        base_cfg = {"language": lang, "split_values": [], "filename_tokens": [],
                    "email_configs": [], "counter": 0, "filename_separator": "_",
                    "dirs": {"input":"input","output":"output","error":"output/error",
                             "processed":"output/processed","no_code":"output/no_code"}}
        cfg_path.write_text(json.dumps(base_cfg))
        # Réinitialiser le cache dans dispatch.config (pas app._config_cache qui
        # est une liaison locale distincte depuis l'étape 2 de la scission).
        import dispatch.config as _dc; _dc._config_cache = None
        pdf_bytes = app.generate_separator_pdf("TESTCODE", code_type="qr")
        text = PdfReader(BytesIO(pdf_bytes)).pages[0].extract_text()
        check(f"[{lang}] separator label '{expected_sep}'",    expected_sep  in text)
        check(f"[{lang}] trigger label '{expected_trig}'",      expected_trig in text)
        check(f"[{lang}] valeur du code 'TESTCODE'",            "TESTCODE"    in text)
        check(f"[{lang}] taille raisonnable (> 1 KB)",          len(pdf_bytes) > 1000)

except ImportError:
    print("  (pypdf non disponible — test generate_separator_pdf ignore)")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Per-file config overrides (Chantier 1-4)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[6] Per-file config overrides")

# ── 6a. _store_file_override / _pop_file_override ────────────────────────────
app._file_config_overrides.clear()

override = {"separator_placement": "after", "subdirs_by_trigger": True}
app._store_file_override("test.pdf", override)
check("store: clé présente dans le dict",
      "test.pdf" in app._file_config_overrides)

popped = app._pop_file_override("test.pdf")
check("pop: retourne l'override stocké",
      popped == override)
check("pop: entrée supprimée du dict",
      "test.pdf" not in app._file_config_overrides)
check("pop: second appel → None",
      app._pop_file_override("test.pdf") is None)
check("pop: fichier inconnu → None",
      app._pop_file_override("inexistant.pdf") is None)

# ── 6b. _parse_upload_override — cas valides ──────────────────────────────────
class FakeForm(dict):
    """Imite flask.request.form (dict-like avec .get())."""

# Champ vide → override vide
ov, errs = app._parse_upload_override(FakeForm())
check("form vide → override vide",   ov == {} and errs == [])

# split_values JSON valide
ov, errs = app._parse_upload_override(FakeForm({
    "split_values": '[{"value":"FAC","page_handling":"delete","case_sensitive":true}]'
}))
check("split_values valide → parsé",
      errs == [] and ov.get("split_values") == [
          {"value": "FAC", "page_handling": "delete", "case_sensitive": True}
      ])

# split_values tableau vide → valide (tout code déclenche)
ov, errs = app._parse_upload_override(FakeForm({"split_values": "[]"}))
check("split_values [] → valide, override contient liste vide",
      errs == [] and ov.get("split_values") == [])

# page_handling absent → défaut "keep"
ov, errs = app._parse_upload_override(FakeForm({
    "split_values": '[{"value":"TEST"}]'
}))
check("page_handling absent → défaut keep",
      errs == [] and ov["split_values"][0]["page_handling"] == "keep")

# case_sensitive absent → défaut True
check("case_sensitive absent → défaut True",
      errs == [] and ov["split_values"][0]["case_sensitive"] is True)

# separator_placement valides
for sp in ("before", "after"):
    ov, errs = app._parse_upload_override(FakeForm({"separator_placement": sp}))
    check(f"separator_placement={sp!r} → valide",
          errs == [] and ov.get("separator_placement") == sp)

# booléens
for key in ("subdirs_by_trigger", "delete_source", "log_verbose"):
    for truthy in ("true", "1", "yes", "True"):
        ov, errs = app._parse_upload_override(FakeForm({key: truthy}))
        check(f"{key}={truthy!r} → True",
              errs == [] and ov.get(key) is True)
    for falsy in ("false", "0", "no", "False"):
        ov, errs = app._parse_upload_override(FakeForm({key: falsy}))
        check(f"{key}={falsy!r} → False",
              errs == [] and ov.get(key) is False)

# ── 6c. _parse_upload_override — cas invalides ───────────────────────────────
# JSON invalide
ov, errs = app._parse_upload_override(FakeForm({"split_values": "not-json"}))
check("split_values JSON invalide → erreur",
      len(errs) > 0 and ov == {})

# split_values non-liste
ov, errs = app._parse_upload_override(FakeForm({"split_values": '{"value":"X"}'}))
check("split_values objet (non-liste) → erreur",
      len(errs) > 0)

# value vide
ov, errs = app._parse_upload_override(FakeForm({
    "split_values": '[{"value":"","page_handling":"keep"}]'
}))
check("split_values[].value vide → erreur",
      len(errs) > 0)

# page_handling invalide
ov, errs = app._parse_upload_override(FakeForm({
    "split_values": '[{"value":"X","page_handling":"keep_and_delete"}]'
}))
check("split_values[].page_handling invalide → erreur",
      len(errs) > 0)

# separator_placement invalide
ov, errs = app._parse_upload_override(FakeForm({"separator_placement": "center"}))
check("separator_placement invalide → erreur",
      len(errs) > 0 and ov == {})

# plusieurs erreurs : uniquement split_values en erreur, separator_placement OK
# (erreur dans split_values doit laisser separator_placement hors de ov aussi)
ov, errs = app._parse_upload_override(FakeForm({
    "split_values": "bad-json",
    "separator_placement": "after",
}))
check("plusieurs champs : split_values invalide → erreur, separator_placement OK",
      len(errs) > 0 and ov.get("separator_placement") == "after")

# ── 6d. _task_create avec config_override ────────────────────────────────────
override_sample = {"separator_placement": "after",
                   "split_values": [{"value": "FAC", "page_handling": "delete",
                                     "case_sensitive": True}]}
tid = app._task_create("override_test.pdf", config_override=override_sample)
with app._tasks_lock:
    task = dict(app._tasks[tid])

check("task créée avec config_override",
      task.get("config_override") == override_sample)

# Sans override → config_override est {} (jamais absent)
tid2 = app._task_create("no_override.pdf")
with app._tasks_lock:
    task2 = dict(app._tasks[tid2])
check("task sans override → config_override == {}",
      task2.get("config_override") == {})
check("config_override toujours présent dans la tâche",
      "config_override" in task2)

# ── 6e. _build_webhook_payload avec config_override ──────────────────────────
payload_no = app._build_webhook_payload(status="success", source_file="a.pdf")
check("payload sans override → pas de clé config_override",
      "config_override" not in payload_no)

payload_ov = app._build_webhook_payload(
    status="success", source_file="a.pdf",
    config_override={"separator_placement": "after"}
)
check("payload avec override → config_override présent",
      payload_ov.get("config_override") == {"separator_placement": "after"})

payload_empty = app._build_webhook_payload(
    status="success", source_file="a.pdf",
    config_override={}
)
check("payload avec override vide {} → pas de clé config_override",
      "config_override" not in payload_empty)


# ─────────────────────────────────────────────────────────────────────────────
# Bilan
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'─' * 50}")
print(f"Resultats : {passed} OK, {failed} ECHEC(s)")
if failed:
    sys.exit(1)
print("OK — tous les tests Python passent.")
