"""
test_i18n_keys.py
-----------------
Verifie la coherence des cles de traduction i18n entre fr.json, en.json
et les fichiers source (app.py, templates/, static/).

Regles verifiees :
  1. Toute cle utilisee dans le code (t('cle'), data-i18n="cle", ...) existe
     dans fr.json ET en.json.
  2. Les deux fichiers JSON ont exactement le meme ensemble de cles.
  3. Aucune valeur n'est vide (chaine vide ou uniquement des espaces).
  4. Aucune cle definie n'est completement absente de tout fichier source
     (cles orphelines — signalees en WARNING, pas en ERREUR, car certaines
     peuvent etre generees dynamiquement).
"""

import json
import re
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent / "splitter"
I18N    = ROOT / "i18n"
SOURCES = [ROOT / "app.py", ROOT / "templates", ROOT / "static"]

KEY_PATTERN = re.compile(r"[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+")

USAGE_PATTERNS = [
    re.compile(r"""\bt\(\s*['"]([a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)['"]"""),
    re.compile(r"""data-i18n=["']([a-zA-Z0-9_.]+)["']"""),
    re.compile(r"""data-i18n-placeholder=["']([a-zA-Z0-9_.]+)["']"""),
    re.compile(r"""data-i18n-title=["']([a-zA-Z0-9_.]+)["']"""),
]


def collect_source_text() -> str:
    parts = []
    for source in SOURCES:
        if source.is_file():
            parts.append(source.read_text(encoding="utf-8"))
        elif source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file() and path.suffix in (".py", ".html", ".js", ".css"):
                    parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def collect_used_keys(src: str) -> set[str]:
    used = set()
    for pat in USAGE_PATTERNS:
        used |= set(pat.findall(src))
    return used


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten(d: dict, prefix: str = "") -> dict:
    """Aplatit un JSON potentiellement imbrique en 'ns.cle'."""
    out = {}
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, full))
        else:
            out[full] = v
    return out


def main() -> int:
    fr_path = I18N / "fr.json"
    en_path = I18N / "en.json"

    fr = flatten(load_json(fr_path))
    en = flatten(load_json(en_path))

    src      = collect_source_text()
    used     = collect_used_keys(src)
    fr_keys  = set(fr.keys())
    en_keys  = set(en.keys())

    errors   = []
    warnings = []

    # 1. Cles utilisees absentes de fr.json
    missing_fr = used - fr_keys
    for k in sorted(missing_fr):
        errors.append(f"Cle utilisee mais absente de fr.json : {k!r}")

    # 2. Cles utilisees absentes de en.json
    missing_en = used - en_keys
    for k in sorted(missing_en):
        errors.append(f"Cle utilisee mais absente de en.json : {k!r}")

    # 3. Cles presentes dans fr.json mais absentes de en.json (et vice-versa)
    only_fr = fr_keys - en_keys
    only_en = en_keys - fr_keys
    for k in sorted(only_fr):
        errors.append(f"Cle presente dans fr.json uniquement (manque dans en.json) : {k!r}")
    for k in sorted(only_en):
        errors.append(f"Cle presente dans en.json uniquement (manque dans fr.json) : {k!r}")

    # 4. Valeurs vides
    for k, v in sorted(fr.items()):
        if isinstance(v, str) and not v.strip():
            errors.append(f"Valeur vide dans fr.json pour la cle : {k!r}")
    for k, v in sorted(en.items()):
        if isinstance(v, str) and not v.strip():
            errors.append(f"Valeur vide dans en.json pour la cle : {k!r}")

    # 0. Verifier qu'aucune valeur dans les JSON bruts n'est un dict.
    # Le runtime JS fait window.I18N[key] (lookup plat) ; une valeur de type
    # dict serait invisible et causerait l'affichage de la cle brute dans l'UI.
    for lang_name, raw_dict in [("fr", load_json(fr_path)), ("en", load_json(en_path))]:
        for k, v in raw_dict.items():
            if isinstance(v, dict):
                errors.append(
                    f"{lang_name}.json : la cle '{k}' a une valeur de type dict "
                    f"(imbriquee). Le runtime JS fait un lookup plat — cette cle "
                    f"serait invisible et afficherait la cle brute dans l'UI. "
                    f"Réécrire en cles plates : '{k}.sous_cle': 'valeur'."
                )

    # 5. Cles orphelines (WARNING uniquement)
    orphans = (fr_keys | en_keys) - used
    for k in sorted(orphans):
        warnings.append(f"Cle definie mais jamais utilisee dans les sources : {k!r}")

    # Rapport
    total_keys = len(fr_keys | en_keys)
    print(f"Cles totales : {total_keys} (fr:{len(fr_keys)} en:{len(en_keys)}) | Cles utilisees : {len(used)}")

    if warnings:
        print(f"\nWARNING — {len(warnings)} cle(s) orpheline(s) :")
        for w in warnings:
            print(f"  {w}")

    if errors:
        print(f"\nECHEC — {len(errors)} erreur(s) :")
        for e in errors:
            print(f"  {e}")
        return 1

    print(f"OK — coherence i18n verifiee ({len(used)} cles utilisees, presentes dans fr.json et en.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
