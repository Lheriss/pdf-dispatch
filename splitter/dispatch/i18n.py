"""
dispatch/i18n.py
-----------------
Internationalisation (i18n): loads translation dictionaries and exposes
t(), the function used throughout the project to produce messages in the
active language.

Architecture note — circular import avoided:
    t() needs get_config() to determine the active language, and
    config.py needs t() for error messages. This is resolved by lazily
    importing get_config() inside the body of t() (executed at call time,
    not at module import time). In Python, imports inside a function body
    are only resolved when the function is called, which breaks the cycle
    at module-load time.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("pdf-dispatch.i18n")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES: tuple[str, ...] = ("fr", "en")
DEFAULT_LANGUAGE: str = "fr"

# Translation JSON files live in the i18n/ directory at the deployment root
# (/app/i18n/ inside the container). dispatch/i18n.py is one level deeper
# (/app/dispatch/i18n.py), so we go up twice to reach /app/.
I18N_DIR: Path = Path(__file__).resolve().parent.parent / "i18n"


# ---------------------------------------------------------------------------
# Load translations
# ---------------------------------------------------------------------------

def _load_translations() -> dict:
    """Load translation dictionaries from the i18n/ directory.

    Returns a dict {lang_code: {key: text}}. If a file is missing or
    invalid, the affected language gets an empty dict (t() will then fall
    back to French, then to the raw key). Called once at startup to
    populate TRANSLATIONS.
    """
    translations: dict = {}
    for lang in SUPPORTED_LANGUAGES:
        path = I18N_DIR / f"{lang}.json"
        try:
            translations[lang] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(
                f"i18n : impossible de charger {path} ({e}) - dictionnaire vide."
            )
            translations[lang] = {}
    return translations


TRANSLATIONS: dict = _load_translations()


# ---------------------------------------------------------------------------
# Translation function
# ---------------------------------------------------------------------------

def t(key: str, lang: str = None, **params) -> str:
    """Translate *key* into the given language (or the configured one).

    Falls back to French, then to the raw key if the translation is absent
    everywhere. Named parameters are substituted via str.format
    (e.g. t('x', n=3)).

    *lang* is resolved from get_config() when not supplied.
    get_config is imported lazily (inside the function body) to avoid a
    circular import between i18n and config.
    """
    if lang is None:
        try:
            # Import tardif : évite le cycle i18n → config → i18n.
            # Résolution en cascade :
            #   1. dispatch.config — available in the fully modularised package
            #      (import succeeds as long as the module has been loaded).
            #   2. 'app' module in sys.modules — fallback for environments
            #      where get_config() is still defined in app.py itself.
            #   3. DEFAULT_LANGUAGE — fallback absolu (ne devrait jamais
            #      survenir en conditions normales).
            try:
                from dispatch.config import get_config  # noqa: PLC0415
            except ImportError:
                import sys as _sys
                _app = _sys.modules.get("app")
                get_config = getattr(_app, "get_config", None) if _app else None  # type: ignore[assignment]
            if get_config is not None:
                lang = get_config().get("language", DEFAULT_LANGUAGE)
            else:
                lang = DEFAULT_LANGUAGE
        except Exception:
            lang = DEFAULT_LANGUAGE

    text = TRANSLATIONS.get(lang, {}).get(key)
    if text is None:
        text = TRANSLATIONS.get(DEFAULT_LANGUAGE, {}).get(key, key)
    if params:
        try:
            text = text.format(**params)
        except Exception:
            pass
    return text
