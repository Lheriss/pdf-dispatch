#!/usr/bin/env python3
"""Check i18n translation key consistency.

Verifies:
- Every key used via t('...')/t("...") in app.py, templates/ or static/
  exists in both i18n/fr.json and i18n/en.json.
- Every key defined in fr.json/en.json but never used is reported as orphaned.

Usage: python3 i18n/check_keys.py
"""
import json
import re
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
I18N    = ROOT / "i18n"
SOURCES = [ROOT / "app.py", ROOT / "templates", ROOT / "static"]

# Concatenate the content of all relevant source files (app.py,
# templates/**, static/**) — keys may come from the Python backend
# (log_event / error messages), the Jinja template (data-i18n=...) or
# the JavaScript frontend (t('key')).
combined = ""
for src in SOURCES:
    if src.is_file():
        combined += src.read_text(encoding="utf-8")
    elif src.is_dir():
        for f in src.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".html", ".js"):
                combined += f.read_text(encoding="utf-8")

# Extract keys used in Python / JS: t('category.key') or t("category.key")
# All i18n keys contain at least one dot (e.g. "log.something", "email.error").
# This avoids false positives from dict accesses like cfg['trigger'] or msg['From'].
used_py_js = set(re.findall(r"""t\(['"]([\w]+\.[\w.]+)['"]\s*[,)]""", combined))

# Extract keys used in HTML templates: data-i18n="key" or data-i18n-placeholder="key"
used_html = set(re.findall(r"""data-i18n(?:-\w+)?=["']([\w.]+)["']""", combined))

used_keys = used_py_js | used_html

# Load translation files
fr_keys = set(json.load(open(I18N / "fr.json", encoding="utf-8")).keys())
en_keys = set(json.load(open(I18N / "en.json", encoding="utf-8")).keys())

ok = True

# Keys used in code but missing from translation files
missing_fr = used_keys - fr_keys
missing_en = used_keys - en_keys
if missing_fr or missing_en:
    ok = False
    for k in sorted(missing_fr | missing_en):
        missing_in = []
        if k in missing_fr: missing_in.append("fr.json")
        if k in missing_en: missing_in.append("en.json")
        print(f"MISSING  {k!r} — absent from {', '.join(missing_in)}")

# Keys defined in translation files but never used (orphaned)
orphans_fr = fr_keys - used_keys
orphans_en = en_keys - used_keys
orphans = orphans_fr | orphans_en
if orphans:
    for k in sorted(orphans):
        sources = []
        if k in orphans_fr: sources.append("fr.json")
        if k in orphans_en: sources.append("en.json")
        print(f"ORPHAN   {k!r} — defined in {', '.join(sources)} but never used")

# Keys present in fr.json but not in en.json and vice-versa
only_fr = fr_keys - en_keys
only_en = en_keys - fr_keys
if only_fr or only_en:
    ok = False
    for k in sorted(only_fr):
        print(f"FR ONLY  {k!r} — present in fr.json but missing from en.json")
    for k in sorted(only_en):
        print(f"EN ONLY  {k!r} — present in en.json but missing from fr.json")

if ok:
    total = len(used_keys)
    print(f"OK - {total} key(s) used, all present in fr.json and en.json.")
else:
    sys.exit(1)
