"""
test_js_t_collision.py
----------------------
Verifie qu'aucune variable locale nommee 't' ne masque la fonction globale
de traduction t('cle') dans splitter/static/js/app.js.

Cette classe de bug (t() shadowing) a cause deux incidents en production
(v1.15.1 et v1.17.1) ; ce test garantit qu'elle ne sera pas reintroduite
silencieusement.

Algorithme :
  1. Trouver toutes les declarations locales de 't' (const/let/var t =,
     parametres de fonction, arrow functions).
  2. Pour chaque declaration, localiser la fonction JavaScript englobante
     et verifier si son corps contient des appels t('...') ou t("...").
  3. Echouer si une telle collision est trouvee, en indiquant la ligne
     et la fonction concernees.
"""

import re
import sys
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "splitter" / "static" / "js" / "app.js"

# Patterns de declaration locale de 't'
LOCAL_T = re.compile(
    r'\b(?:const|let|var)\s+t\b'           # const/let/var t = ...
    r'|(?<!\w)function\s*\(\s*t\s*[,)]'    # function(t, ...) ou function(t)
    r'|\(\s*t\s*,|\(\s*t\s*\)\s*=>'        # (t, ...) ou (t) =>
    r'|\bfor\s*\(\s*(?:const|let)\s+t\b'   # for(const t of ...)
)

# Appel de la fonction de traduction globale
T_CALL = re.compile(r"""\bt\(\s*['"]""")


def find_fn_bounds(lines: list[str], decl_idx: int) -> tuple[int, int, str]:
    """Remonte vers la declaration de fonction englobante et trouve sa fin.

    Retourne (start_line_1indexed, end_line_1indexed, fn_name).
    """
    depth = 0
    fn_start = 0
    fn_name  = "(anonymous)"

    for j in range(decl_idx, -1, -1):
        for ch in reversed(lines[j]):
            if ch == '}':  depth += 1
            elif ch == '{': depth -= 1
        if depth < 0:
            fn_start = j
            for k in range(j, max(-1, j - 5), -1):
                m = re.match(r'\s*(?:async\s+)?function\s+(\w+)', lines[k])
                if m:
                    fn_name  = m.group(1)
                    fn_start = k
                    break
            break

    # Trouver la fin (accolades equilibrees depuis fn_start)
    depth = 0; started = False
    fn_end = fn_start
    for j in range(fn_start, len(lines)):
        for ch in lines[j]:
            if ch == '{':   depth += 1; started = True
            elif ch == '}': depth -= 1
        if started and depth == 0:
            fn_end = j
            break

    return fn_start + 1, fn_end + 1, fn_name


def check_collisions(src: str) -> list[str]:
    """Retourne la liste des collisions trouvees (vide = OK)."""
    lines    = src.splitlines()
    failures = []

    for i, line in enumerate(lines):
        if not LOCAL_T.search(line):
            continue
        fn_start, fn_end, fn_name = find_fn_bounds(lines, i)
        body  = "\n".join(lines[fn_start - 1 : fn_end])
        calls = T_CALL.findall(body)
        if calls:
            failures.append(
                f"L{i+1} dans {fn_name}() [L{fn_start}-{fn_end}] : "
                f"variable locale 't' masque t() — {len(calls)} appel(s) t('...')\n"
                f"    declaration : {line.strip()!r}"
            )
    return failures


def main() -> int:
    src      = APP_JS.read_text(encoding="utf-8")
    failures = check_collisions(src)

    if failures:
        print(f"ECHEC — {len(failures)} collision(s) t() detectee(s) dans {APP_JS.name}:\n")
        for f in failures:
            print(f"  {f}")
        print(
            "\nCorrigez en renommant la variable locale (ex. 'trig', 'tok', 'tv')"
            " pour ne pas masquer la fonction globale t('cle')."
        )
        return 1

    print(f"OK — aucune collision t() dans {APP_JS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
