"""
dispatch/processing.py
-----------------------
PDF processing pipeline: all code that transforms a source file into
named, split, and routed output documents.

Contains:
  - Filename validation and construction (validate_filename_tokens,
    build_filename, MAX_FILENAME_LEN)
  - Output folder routing (ensure_dirs, get_output_dir, NO_CODE_TRIGGER)
  - File stabilisation and validity (wait_until_stable, is_valid_pdf,
    move_to_error)
  - Code detection (decode_zxing/pyzbar/page, find_split_pages)
  - PDF metadata (add_pdf_metadata)
  - Separator page generation (generate_separator_pdf)
  - Main processing pipeline (process_file)

Internal dependencies:
  - dispatch.config  (env constants, get_config, get_dirs, next_counter, ...)
  - dispatch.i18n    (t)
  - dispatch.state   (log_event, _task_update, state, locks, _pop_file_override)
  - dispatch.hook    (_run_post_process_hook)
  - dispatch.webhook (_fire_webhook)
"""

import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path

from pdf2image import convert_from_path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, create_string_object

from dispatch.config import (
    API_TASK_TIMEOUT, BARCODE_DPI_SCAN, CONFIG_DEFAULTS,
    DATA_DIR, DPI, ERROR_DIR, FILE_STABLE_INTERVAL,
    FILE_STABLE_TIMEOUT, FORBIDDEN_CHARS, INPUT_DIR,
    MAX_UPLOAD_MB, MAX_WORKER_THREADS, MAX_PAGES,
    OUTPUT_DIR, PROCESSED_DIR, SCANNER, UPSCALE,
    _is_glob_pattern, _match_trigger, _save_stats,
    get_config, get_dirs, log, next_counter, update_config,
    update_dir_paths,
)
from dispatch.i18n import t
from dispatch.state import (
    _email_triggers, _email_triggers_lock,
    _pop_file_override, _processing_semaphore, _task_update,
    log_event, processing, processing_lock, state, state_lock,
)
from dispatch.hook    import _run_post_process_hook
from dispatch.webhook import _fire_webhook

# ---------------------------------------------------------------------------
# Filename validation and construction
# ---------------------------------------------------------------------------

MAX_FILENAME_LEN = 200


def validate_filename_tokens(tokens: list) -> tuple[bool, str]:
    """Validate the token configuration. Returns (ok, error_message)."""
    has_counter = any(t.get("type") == "counter" and t.get("enabled", True)
                      for t in tokens)
    if not has_counter:
        return False, t("filename.counter_required")

    for tok in tokens:
        if tok.get("type") == "counter":
            digits = tok.get("digits", 6)
            if not (3 <= digits <= 8):
                return False, t("filename.error_counter_digits", digits=digits)
        if tok.get("type") == "string" and tok.get("enabled", True):
            val = tok.get("value", "")
            if re.search(FORBIDDEN_CHARS, val):
                return False, t("filename.error_invalid_chars", value=val)

    # Test with a dummy name
    test_name = build_filename("TEST", tokens, counter=1, now=datetime(2026, 1, 1, 12, 0, 0))
    if len(test_name) > MAX_FILENAME_LEN:
        return False, t("filename.error_too_long", length=len(test_name), max=MAX_FILENAME_LEN)

    return True, ""


def build_filename(trigger: str, tokens: list, counter: int,
                   now: datetime = None, separator: str = "_") -> str:
    """Build the output filename (without extension) from the configured tokens."""
    if now is None:
        now = datetime.now()

    sep = separator if separator in ("_", "-", ".", "") else "_"
    safe_trigger = re.sub(FORBIDDEN_CHARS, "_", trigger)[:40]
    parts = []

    for token in tokens:
        if not token.get("enabled", True):
            continue
        t = token.get("type")
        if t == "trigger":
            parts.append(safe_trigger)
        elif t == "string":
            # String tokens have no toggle — always included when non-empty
            val = re.sub(FORBIDDEN_CHARS, sep or "_", token.get("value", "")).strip(sep or "_")
            if val:
                parts.append(val)
        elif t == "timestamp":
            fmt = token.get("format", "%Y%m%d-%H%M%S")
            try:
                parts.append(now.strftime(fmt))
            except Exception:
                parts.append(now.strftime("%Y%m%d-%H%M%S"))
        elif t == "counter":
            digits = max(3, min(8, token.get("digits", 6)))
            parts.append(str(counter).zfill(digits))

    name = sep.join(p for p in parts if p)
    if sep:
        name = re.sub(re.escape(sep) + "+", sep, name).strip(sep)
    return name or f"doc_{now.strftime('%Y%m%d-%H%M%S')}_{counter}"


# ---------------------------------------------------------------------------
# Output folders
# ---------------------------------------------------------------------------

def ensure_dirs():
    """Create all required folders if they do not exist."""
    update_dir_paths()
    dirs = get_dirs()
    for k, d in dirs.items():
        try:
            d.mkdir(parents=True, exist_ok=True)
            try: os.chmod(d, 0o777)
            except: pass
        except PermissionError:
            if not d.exists():
                log.error(f"Folder missing and could not be created: {d}")


NO_CODE_TRIGGER = "no_code"

def _pattern_to_dirname(pattern: str) -> str:
    """
    Convert a glob pattern to a valid folder name.
    Glob special characters (* ? [ ] !) are replaced with _.
    Example: FK* → FK_  |  REF[0-9][0-9][0-9] → REF_______
    """
    # Replace glob characters then forbidden characters
    cleaned = re.sub(r'[*?\[\]!]', '_', pattern)
    cleaned = re.sub(FORBIDDEN_CHARS, '_', cleaned)
    # Collapse consecutive underscores
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned[:60] or "trigger"


def get_output_dir(trigger: str, cfg: dict, matched_pattern: str = None) -> Path:
    """Return the output folder for a given trigger.
    - Always /output/no_code for files with no trigger code.
    - /output/<sanitised_pattern> when the subfolders option is enabled.
      The folder name is based on the configured pattern (not the actual code
      value), so all codes matching FK* go into FK_/.
    - /output otherwise.
    """
    if trigger == NO_CODE_TRIGGER:
        subdir = OUTPUT_DIR / NO_CODE_TRIGGER
        try:
            subdir.mkdir(parents=True, exist_ok=True)
            try: os.chmod(subdir, 0o777)
            except Exception: pass
        except OSError as _e:
            log.warning(f"Could not create no_code dir {subdir}: {_e}")
        return subdir
    if cfg.get("subdirs_by_trigger", False) and trigger:
        # Use the configured pattern as the folder name
        base   = matched_pattern if matched_pattern else trigger
        safe   = _pattern_to_dirname(base)
        subdir = OUTPUT_DIR / safe
        subdir.mkdir(parents=True, exist_ok=True)
        try: os.chmod(subdir, 0o777)
        except Exception: pass
        return subdir
    return OUTPUT_DIR


# ---------------------------------------------------------------------------
# File stabilisation
# ---------------------------------------------------------------------------

def wait_until_stable(path: Path) -> bool:
    """Wait for the file at `path` to stop changing before processing it
    (avoids reading a file still being written by a scanner or network transfer).

    Checks the file size every FILE_STABLE_INTERVAL seconds; if it matches
    the previous reading (and is non-zero), waits once more then confirms.
    Returns True if the file is deemed stable, False if:
      - the file disappears before stabilising (logs "file_disappeared");
      - FILE_STABLE_TIMEOUT seconds elapse without stabilisation
        (logs "stabilization_timeout")."""
    deadline  = time.time() + FILE_STABLE_TIMEOUT
    prev_size = -1
    while time.time() < deadline:
        try:
            size = path.stat().st_size
            if size == prev_size and size > 0:
                time.sleep(FILE_STABLE_INTERVAL)
                if path.stat().st_size == size:
                    return True
            prev_size = size
        except FileNotFoundError:
            log_event("warning", t("log.file_disappeared", filename=path.name), path.name)
            return False
        time.sleep(FILE_STABLE_INTERVAL)
    log_event("error",
              t("log.stabilization_timeout", timeout=FILE_STABLE_TIMEOUT, filename=path.name),
              path.name)
    return False


def is_valid_pdf(path: Path) -> bool:
    """Check that a file is a readable PDF (opens it with pypdf and counts
    pages). Returns False for any corrupted file or non-PDF."""
    try:
        r = PdfReader(str(path))
        _ = len(r.pages)
        return True
    except Exception:
        return False


def move_to_error(path: Path, reason: str):
    """Copy `path` to /output/error/ with a timestamped suffix
    (`<name>_<YYYYMMDD-HHMMSS>_ERROR<ext>`), delete the original, and log
    `reason` (already translated by the caller via t()). If the copy/delete
    itself fails (permissions, disk full...), logs the error without raising."""
    ensure_dirs()
    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ERROR_DIR / f"{path.stem}_{ts}_ERROR{path.suffix}"
    try:
        shutil.copy2(str(path), str(dest))
        os.chmod(dest, 0o664)
        path.unlink()
        log_event("error", t("log.moved_to_error", filename=path.name, reason=reason), path.name)
    except Exception as e:
        log_event("error", t("log.move_to_error_failed", message=e), path.name)


# ---------------------------------------------------------------------------
# Code detection (1D barcodes and QR codes)
# ---------------------------------------------------------------------------

def decode_zxing(img):
    """Decode all codes (1D barcodes and QR codes) in `img` (PIL image) using
    zxing-cpp (BARCODE_SCANNER=ZXING, default — faster and more tolerant than
    pyzbar on average-quality scans). Returns a list of decoded strings
    (possibly empty). Falls back to decode_pyzbar if zxingcpp is not installed."""
    try:
        import zxingcpp, numpy as np
        return [r.text for r in zxingcpp.read_barcodes(np.array(img))]
    except ImportError:
        return decode_pyzbar(img)


def decode_pyzbar(img):
    """Decode all codes (1D barcodes and QR codes) in `img` (PIL image) using
    pyzbar/ZBar. Used when BARCODE_SCANNER=ZBAR or zxing-cpp is unavailable.
    Returns a list of decoded strings (possibly empty)."""
    from pyzbar.pyzbar import decode as _d
    return [r.data.decode("utf-8", errors="replace") for r in _d(img)]


def decode_page(img):
    return decode_zxing(img) if SCANNER == "ZXING" else decode_pyzbar(img)


# ---------------------------------------------------------------------------
# Detailed code detection (value + symbology + position) — used by the
# /api/detect diagnostic endpoint. Kept separate from decode_page so the
# production splitting path stays untouched.
# ---------------------------------------------------------------------------

def _decode_detailed_zxing(img) -> list[dict]:
    """Like decode_zxing but returns dicts with value, symbology type and
    bounding box (pixels, in the coordinate space of `img`)."""
    try:
        import zxingcpp, numpy as np
    except ImportError:
        return _decode_detailed_pyzbar(img)
    out = []
    for r in zxingcpp.read_barcodes(np.array(img)):
        try:
            pts = [r.position.top_left, r.position.top_right,
                   r.position.bottom_right, r.position.bottom_left]
            xs, ys = [p.x for p in pts], [p.y for p in pts]
            bbox = {"x": min(xs), "y": min(ys),
                    "w": max(xs) - min(xs), "h": max(ys) - min(ys)}
        except Exception:
            bbox = None
        out.append({
            "value": r.text,
            "type":  str(r.format).split(".")[-1],   # e.g. QRCode, Code128
            "bbox":  bbox,
        })
    return out


def _decode_detailed_pyzbar(img) -> list[dict]:
    """Like decode_pyzbar but returns dicts with value, symbology type and
    bounding box (pixels, in the coordinate space of `img`)."""
    from pyzbar.pyzbar import decode as _d
    out = []
    for r in _d(img):
        try:
            bbox = {"x": r.rect.left, "y": r.rect.top,
                    "w": r.rect.width, "h": r.rect.height}
        except Exception:
            bbox = None
        out.append({
            "value": r.data.decode("utf-8", errors="replace"),
            "type":  getattr(r, "type", "unknown"),
            "bbox":  bbox,
        })
    return out


def decode_page_detailed(img) -> list[dict]:
    """Detailed variant of decode_page: list of {value, type, bbox} dicts."""
    return (_decode_detailed_zxing(img) if SCANNER == "ZXING"
            else _decode_detailed_pyzbar(img))


def find_split_pages(pdf_path: Path, trigger_map: list,
                     separator_placement: str = "before") -> list[dict]:
    """Two-pass barcode scan for efficient processing of multi-page PDFs.

    Pass 1 — all pages rasterised at BARCODE_DPI_SCAN (default 150 DPI).
              Identifies positive pages (code detected) and negative pages
              (content, no code). Fast: 4× less data than 300 DPI.

    Pass 2 — only positive pages, re-rasterised at DPI (default 300 DPI)
              page by page using pdf2image first_page/last_page. Provides
              maximum decode accuracy for separator sheets. Skipped when
              BARCODE_DPI_SCAN == DPI (single-pass mode).

    Content pages are never rasterised at full DPI, so a 12-page all-content
    PDF processes in ~15 s instead of >60 s at BARCODE_DPI_SCAN=150.

    trigger_map : list of {value, page_handling, case_sensitive} dicts.
    separator_placement : "before" or "after" (global option).
    Returns : list of {page (0-indexed), value, matched_pattern, page_handling}

    Effective page_handling (placement × per-trigger keep/delete):
      before + keep   → "start"      (separator is first page of document)
      before + delete → "delete"     (separator removed entirely)
      after  + keep   → "end"        (separator is last page of document)
      after  + delete → "end_delete" (separator removed, names preceding segment)
    """
    def _upscale(imgs):
        if UPSCALE > 1.0:
            from PIL import Image
            return [img.resize((int(img.width * UPSCALE), int(img.height * UPSCALE)),
                               Image.LANCZOS) for img in imgs]
        return imgs

    # ── Pass 1: fast scan at BARCODE_DPI_SCAN ────────────────────────────────
    images_fast = _upscale(convert_from_path(str(pdf_path), dpi=BARCODE_DPI_SCAN))
    n_pages = len(images_fast)

    # fast_codes[i] = list of decoded strings on page i (may be empty)
    fast_codes: dict[int, list] = {i: decode_page(img) for i, img in enumerate(images_fast)}
    positive_pages = sorted(i for i, codes in fast_codes.items() if codes)

    # ── Pass 2: re-decode positive pages at full DPI ─────────────────────────
    # Skipped when both DPIs are identical (single-pass / test-override mode).
    if positive_pages and BARCODE_DPI_SCAN != DPI:
        verified: dict[int, list] = {}
        for i in positive_pages:
            # pdf2image first_page/last_page are 1-indexed
            page_imgs = _upscale(
                convert_from_path(str(pdf_path), dpi=DPI,
                                  first_page=i + 1, last_page=i + 1)
            )
            verified[i] = decode_page(page_imgs[0]) if page_imgs else []
    else:
        # Single-pass: trust fast-scan results for positive pages
        verified = {i: fast_codes[i] for i in positive_pages}

    # ── Trigger matching ──────────────────────────────────────────────────────
    # Only positive pages carry codes; negative (content) pages produce no hits.
    hits = []
    for i in range(n_pages):
        all_codes = verified.get(i, [])
        page_matches = []
        for code in all_codes:
            if not trigger_map:
                # Permissive mode: every code triggers a split (one hit per page)
                if not page_matches:
                    page_matches.append({
                        "value":           code,
                        "matched_pattern": code,
                        "page_handling":   "keep",
                    })
            else:
                matched = False
                for trig in trigger_map:
                    if _match_trigger(code, trig):
                        pattern       = trig.get("value", code)
                        is_glob       = _is_glob_pattern(pattern)
                        glob_info     = t("log.glob_info_suffix", pattern=pattern) if is_glob else ""
                        page_handling = trig.get("page_handling", "keep")
                        del_info      = t("log.page_removed_suffix") if page_handling == "delete" else ""
                        log_event("info",
                                  t("log.page_split", page=i + 1, code=code,
                                    glob_info=glob_info, del_info=del_info),
                                  pdf_path.name, verbose=True)
                        page_matches.append({
                            "value":           code,
                            "matched_pattern": pattern,
                            "page_handling":   page_handling,
                        })
                        matched = True
                        # Keep going — multiple triggers may match the same code
                if not matched:
                    log_event("info", t("log.page_ignored", page=i + 1, code=code),
                              pdf_path.name, verbose=True)

        if page_matches:
            if len(page_matches) > 1:
                log_event("info",
                          t("log.multiple_triggers", page=i + 1, count=len(page_matches)),
                          pdf_path.name, verbose=True)
            for m in page_matches:
                _keep  = m["page_handling"] == "keep"
                _after = separator_placement == "after"
                eff_ph = ("end"        if _after and _keep  else
                          "end_delete" if _after and not _keep else
                          "start"      if _keep else "delete")
                hits.append({
                    "page":            i,
                    "value":           m["value"],
                    "matched_pattern": m["matched_pattern"],
                    "page_handling":   eff_ph,
                })
    return hits


# ---------------------------------------------------------------------------
# PDF metadata
# ---------------------------------------------------------------------------

def add_pdf_metadata(writer: PdfWriter, trigger: str, out_filename: str, now: datetime):
    """
    Write standard PDF metadata:
      /Title        = output filename (without extension)
      /Subject      = trigger code value
      /Author       = pdf-dispatch
      /CreationDate = processing date/time in PDF format (D:YYYYMMDDHHmmss)
    """
    # Standard PDF date format: D:YYYYMMDDHHmmss
    pdf_date = now.strftime("D:%Y%m%d%H%M%S")
    try:
        writer.add_metadata({
            "/Title":        Path(out_filename).stem,
            "/Subject":      trigger,
            "/Author":       "pdf-dispatch",
            "/CreationDate": pdf_date,
            "/ModDate":      pdf_date,
            "/Creator":      "pdf-dispatch",
            "/Producer":     "pdf-dispatch",
        })
    except Exception as e:
        log.warning(f"Could not write PDF metadata: {e}")




# ---------------------------------------------------------------------------
# Separator page generation
# ---------------------------------------------------------------------------

def generate_separator_pdf(trigger_value: str, code_type: str = "qr") -> bytes:
    """
    Generate a one-page A4 PDF containing the trigger's barcode or QR code.
    code_type: 'qr' (default) or 'barcode' (Code128)
    Returns the PDF bytes.
    """
    try:
        import qrcode as _qrcode
        import barcode as _barcode
        from barcode.writer import ImageWriter
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm, mm
        from reportlab.pdfgen import canvas as _canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as e:
        raise RuntimeError(f"Missing dependency for separator generation: {e}")

    W, H = A4  # 595 x 842 pts
    buf_pdf = BytesIO()
    c = _canvas.Canvas(buf_pdf, pagesize=A4)

    # ── Colours ───────────────────────────────────────────────────────────
    BLACK  = (0, 0, 0)
    GREEN  = (0, 1, 0.53)   # #00ff87
    GRAY1  = (0.53, 0.53, 0.53)
    GRAY2  = (0.75, 0.75, 0.75)
    GRAY3  = (0.93, 0.93, 0.93)

    # ── Header ────────────────────────────────────────────────────────────
    header_y = H - 2*cm

    # Logo hexagone PD
    hex_size = 0.55 * cm
    hex_cx   = 2.2 * cm
    hex_cy   = header_y
    import math
    hex_pts  = [(hex_cx + hex_size * math.sin(math.radians(60*i)),
                 hex_cy + hex_size * math.cos(math.radians(60*i))) for i in range(6)]
    c.setFillColorRGB(*GREEN)
    p = c.beginPath()
    p.moveTo(*hex_pts[0])
    for pt in hex_pts[1:]:
        p.lineTo(*pt)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # "PD" inside the hexagon
    c.setFillColorRGB(*BLACK)
    c.setFont("Helvetica-Bold", 5.5)
    c.drawCentredString(hex_cx, hex_cy - 2, "PD")

    # Application name
    c.setFillColorRGB(*GRAY1)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(2.2*cm + hex_size + 0.3*cm, header_y - 2.5, "PDF-DISPATCH")

    # Right-side label (SEPARATOR)
    c.setFillColorRGB(*GRAY2)
    c.setFont("Helvetica", 7)
    c.drawRightString(W - 2*cm, header_y - 2.5, t("separator.label_separator"))

    # Separator line
    line_y = header_y - 0.7*cm
    c.setStrokeColorRGB(*GRAY3)
    c.setLineWidth(0.5)
    c.line(2*cm, line_y, W - 2*cm, line_y)

    # ── Trigger label ────────────────────────────────────────────────────
    label_y = line_y - 1.4*cm
    c.setFillColorRGB(*GRAY2)
    c.setFont("Helvetica", 7)
    c.drawCentredString(W/2, label_y + 0.5*cm, t("separator.label_trigger"))

    c.setFillColorRGB(*BLACK)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(W/2, label_y - 0.3*cm, trigger_value)

    # ── Code (QR or barcode) ─────────────────────────────────────────────
    code_y_center = H / 2 - 0.5*cm

    if code_type == "qr":
        qr = _qrcode.QRCode(
            version=None,
            error_correction=_qrcode.constants.ERROR_CORRECT_M,
            box_size=12, border=2
        )
        qr.add_data(trigger_value)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf_img = BytesIO()
        img.save(buf_img, format="PNG")
        buf_img.seek(0)
        code_size = 6 * cm
        c.drawImage(ImageReader(buf_img),
                    W/2 - code_size/2, code_y_center - code_size/2,
                    width=code_size, height=code_size,
                    preserveAspectRatio=True)

    else:  # Code128
        CODE128 = _barcode.get_barcode_class("code128")
        bc = CODE128(trigger_value, writer=ImageWriter())
        buf_img = BytesIO()
        bc.write(buf_img, options={
            "module_height": 20,
            "text_distance": 3,
            "font_size":     10,
            "quiet_zone":    6,
            "write_text":    False,
        })
        buf_img.seek(0)
        bc_w = 10 * cm
        bc_h = 3  * cm
        c.drawImage(ImageReader(buf_img),
                    W/2 - bc_w/2, code_y_center - bc_h/2,
                    width=bc_w, height=bc_h,
                    preserveAspectRatio=False)

    # Plain-text value below the code
    c.setFillColorRGB(*GRAY1)
    c.setFont("Courier-Bold", 12)
    c.drawCentredString(W/2, code_y_center - (3.5 if code_type == "qr" else 1.8)*cm, trigger_value)

    # ── Footer line ──────────────────────────────────────────────────────
    foot_y = 2.5 * cm
    c.setStrokeColorRGB(*GRAY3)
    c.line(2*cm, foot_y, W - 2*cm, foot_y)

    c.setFillColorRGB(*GRAY2)
    c.setFont("Helvetica", 6)
    c.drawCentredString(W/2, foot_y - 0.4*cm,
                        t("separator.footer_text"))

    c.save()
    return buf_pdf.getvalue()


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Post-processing hook  →  dispatch/hook.py
# ---------------------------------------------------------------------------

from dispatch.hook import _run_post_process_hook


# ---------------------------------------------------------------------------
# Outbound webhook  →  dispatch/webhook.py
# ---------------------------------------------------------------------------

from dispatch.webhook import (
    _build_webhook_payload,
    _ssrf_safe, _ssrf_blocked_response,
    _deliver_webhook, _fire_webhook,
)


def process_file(pdf_path: Path):
    """Process a source PDF file: detect triggers, split, name and route
    the resulting documents, then archive or delete the source. This is
    the core function called for every file, whether it came from the
    watched directory (PDFHandler), the web upload API
    (api_upload) ou d'une piece jointe email (_imap_process).

    Etapes :
      1. Garde anti-doublon (`processing`/`processing_lock`) : si `fname`
         est deja en cours de traitement, retourne immediatement (evite un
         double traitement si l'evenement watchdog se declenche plusieurs
         fois pour le meme fichier).
      2. wait_until_stable : attend la fin de l'ecriture du fichier.
      3. is_valid_pdf : un fichier corrompu est deplace vers /output/error/
         (move_to_error) et compte dans les statistiques d'erreurs.
      4. Lit la configuration courante (declencheurs, options, tokens de
         nommage) et recupere un eventuel declencheur par defaut associe a
         ce fichier par le polling email (_email_triggers, pour les PDFs
         telecharges par email qui ne contiennent eux-memes aucun
         barcode/QR code).
      5. find_split_pages localise les pages contenant un declencheur. Si
         aucune n'est trouvee : utilise le declencheur par defaut email s'il
         existe, sinon classe le fichier sous NO_CODE_TRIGGER (copie dans
         /output/no_code/).
      6. Pour chaque segment de pages (entre deux declencheurs successifs,
         ou jusqu'a la fin du document) :
           - si "supprimer la page barcode" est actif et que le segment ne
             contiendrait plus que cette page, le segment est ignore
             (log.empty_after_removal) ;
           - sinon, construit un nouveau PDF (PdfWriter) avec les pages du
             segment, determine le dossier de sortie (get_output_dir, avec
             sous-dossier par declencheur si configure), genere le nom de
             fichier (build_filename + compteur global next_counter), evite
             les collisions de nom (suffixe _NNN), ecrit les metadonnees PDF
             (add_pdf_metadata) et le fichier sur disque.
      7. Selon l'option "Archiver le fichier source" : supprime la source
         (delete_src) ou la copie horodatee dans /output/processed/ avant de
         la supprimer (archive_src).
      8. Met a jour les statistiques persistantes (state["stats"] :
         processed, split_docs, errors, last_file, last_time).

    Toute exception inattendue est capturee : journalisee
    (log.unexpected_error), comptee dans les erreurs, et le fichier source
    (s'il existe encore) est deplace vers /output/error/ avec le message
    d'exception comme raison. Dans tous les cas (succes, erreur ou retour
    anticipe), le `finally` retire `fname` de `processing` et de la file
    displayed in the web interface (state["queue"])."""
    fname = pdf_path.name
    _file_override: dict | None = None   # populated after wait_until_stable

    with processing_lock:
        if fname in processing:
            return
        processing.add(fname)

    with state_lock:
        state["queue"][fname] = True

    # Acquire the concurrency semaphore — blocks if MAX_CONCURRENT_PROCESSING
    # files are already being rendered.  Released in the finally block.
    _processing_semaphore.acquire()
    _task_deadline = time.monotonic() + API_TASK_TIMEOUT

    try:
        log_event("info", t("log.waiting_stable", filename=fname), fname, verbose=True)
        if not wait_until_stable(pdf_path):
            if pdf_path.exists():
                move_to_error(pdf_path,
                    t("log.stabilization_timeout",
                      timeout=FILE_STABLE_TIMEOUT,
                      filename=fname))
            return

        # ── Resource-limit guards (watchdog + email paths) ────────────────
        # api_upload() has an early check before writing to disk.  For files
        # that arrive via the watched folder (direct drop, email attachment)
        # the check runs here — after stabilisation but before any DPI
        # rendering (find_split_pages).  At BARCODE_DPI=300 each A4 page
        # requires ~26 MB RAM; a 200-page PDF needs ~5 GB and crashes Docker.
        _size_mb = pdf_path.stat().st_size / (1024 * 1024)
        if _size_mb > MAX_UPLOAD_MB:
            move_to_error(pdf_path,
                t("log.file_too_large",
                  filename=fname,
                  size_mb=f"{_size_mb:.1f}",
                  max_mb=str(MAX_UPLOAD_MB)))
            return
        try:
            _rdr   = PdfReader(str(pdf_path))
            _npages = len(_rdr.pages)
            if _npages > MAX_PAGES:
                move_to_error(pdf_path,
                    t("log.too_many_pages",
                      filename=fname,
                      count=str(_npages),
                      max_pages=str(MAX_PAGES)))
                return
        except Exception:
            pass  # malformed PDF — let is_valid_pdf() handle it below
        # ─────────────────────────────────────────────────────────────────

        # Consume the per-file config override stored by api_upload.
        # Placed AFTER wait_until_stable to avoid a race condition:
        # the watchdog fires as soon as the file is created (file.save),
        # but _store_file_override is called by the upload handler a few
        # microseconds later.  wait_until_stable provides the buffer needed
        # to guarantee the override is present when we pop it.
        _file_override = _pop_file_override(fname)

        _task_update(fname, status="processing")

        if not is_valid_pdf(pdf_path):
            move_to_error(pdf_path, t("log.reason_invalid_pdf"))
            with state_lock:
                state["stats"]["errors"] += 1
            _task_update(fname, status="error", error="invalid or unreadable PDF")
            _run_post_process_hook(source_file=fname, status="error", error_msg="invalid or unreadable PDF")
            _fire_webhook(source_file=fname, status="error", error_msg="invalid or unreadable PDF",
                          config_override=_file_override)
            return

        cfg = get_config()
        if _file_override:
            cfg = {**cfg, **_file_override}
            log_event("info",
                      t("log.file_override_applied", keys=", ".join(_file_override)),
                      fname, verbose=True)
        sv_list     = cfg.get("split_values", [])
        # trigger_map: normalised list of dicts for find_split_pages
        trigger_map = [t for t in sv_list if isinstance(t, dict) and "value" in t]
        # Read the email default trigger from the in-memory dict
        email_default_trigger = None
        with _email_triggers_lock:
            email_default_trigger = _email_triggers.pop(pdf_path.name, None)
        tokens       = cfg.get("filename_tokens", CONFIG_DEFAULTS["filename_tokens"])
        # delete_source=True → archive to /processed; False → delete
        archive_src  = cfg.get("delete_source", False)
        delete_src   = not archive_src

        log_event("info", t("log.processing", filename=fname), fname)
        sep_placement = cfg.get("separator_placement", "before")
        _page_handling_icon = {"keep": "", "delete": "✂"}
        if sep_placement == "after":
            _page_handling_icon = {"keep": "↩", "delete": "↩✂"}
        triggers_summary = [
            f"{tv['value']}"
            f"{'~' if _is_glob_pattern(tv['value']) else ''}"
            f"{_page_handling_icon.get(tv.get('page_handling','keep'), '')}"
            f"{'[i]' if not tv.get('case_sensitive', True) else ''}"
            for tv in sv_list if isinstance(tv, dict)
        ]
        log_event("info",
                  t("log.active_config", triggers=triggers_summary,
                    delete_src=delete_src, subdirs=cfg.get('subdirs_by_trigger', False)),
                  fname, verbose=True)
        reader = PdfReader(str(pdf_path))
        total  = len(reader.pages)

        # Check deadline before the most expensive operation (full-DPI rendering).
        if time.monotonic() > _task_deadline:
            _reason = f"Processing timeout ({API_TASK_TIMEOUT}s): {fname}"
            log_event("error", _reason, fname)
            _task_update(fname, status="error", error=_reason)
            if pdf_path.exists():
                move_to_error(pdf_path, _reason)
            return

        splits = find_split_pages(pdf_path, trigger_map, sep_placement)

        # Check deadline after rendering — guards against very slow scans.
        if time.monotonic() > _task_deadline:
            _reason = f"Processing timeout ({API_TASK_TIMEOUT}s): {fname}"
            log_event("error", _reason, fname)
            _task_update(fname, status="error", error=_reason)
            if pdf_path.exists():
                move_to_error(pdf_path, _reason)
            return

        if not splits and email_default_trigger:
            splits = [{"page": 0, "value": email_default_trigger,
                       "matched_pattern": email_default_trigger,
                       "page_handling": "start"}]  # always "start": no real trigger, include page 0 as first page of output
            log_event("info",
                      t("log.email_default_trigger_applied", trigger=email_default_trigger),
                      fname)
        if not splits:
            log_event("warning",
                      t("log.no_barcode_found", filename=fname),
                      fname)
            splits = [{"page": 0, "value": NO_CODE_TRIGGER, "page_handling": "start"}]  # "start" → page 0 prepended; final _flush collects all pages as one no_code doc

        # ── Segment building ─────────────────────────────────────────────────
        # Build output segments using a state machine that handles three behaviours:
        #
        #   "start"  (default) : trigger page is the FIRST page of the named document.
        #                        — separator as cover page, placed before each document.
        #   "delete"           : trigger page is removed from the output entirely.
        #                        — separator is a pure marker, not part of any document.
        #   "end"    (new)     : trigger page is the LAST page of the named document.
        #                        — separator as closing sheet, placed after each document.
        #
        # Multiple triggers on the same page produce multiple independent output
        # documents (one per trigger hit), each covering the same page range.

        from collections import defaultdict as _dd
        by_page = _dd(list)
        for h in splits:
            by_page[h["page"]].append(h)

        # State
        seg_start    = 0                # 0-indexed start of current pending segment
        pending_name = NO_CODE_TRIGGER  # name for the current pending segment
        pending_pat  = NO_CODE_TRIGGER  # matched_pattern for the current pending segment
        prepend_idx  = None             # page index to prepend ("start" mode carry-over)

        # Output specs: list of {pages: [0-indexed ints], trigger, pattern, was_delete}
        output_specs = []

        def _flush(excl_end, name, pat, include_prepend=True, was_delete=False):
            """Flush accumulated pages [prepend_idx?] + range(seg_start, excl_end)."""
            nonlocal prepend_idx
            pages = []
            if include_prepend and prepend_idx is not None:
                pages.append(prepend_idx)
                prepend_idx = None
            pages.extend(range(seg_start, excl_end))
            output_specs.append({"pages": pages, "trigger": name,
                                  "pattern": pat, "was_delete": was_delete})

        for P in sorted(by_page.keys()):
            hits = by_page[P]

            # "end" hits: close current segment at P (inclusive), named by hit value
            for hit in [h for h in hits if h.get("page_handling", "start") == "end"]:
                pages = (([prepend_idx] if prepend_idx is not None else [])
                         + list(range(seg_start, P + 1)))
                output_specs.append({
                    "pages": pages, "trigger": hit["value"],
                    "pattern": hit.get("matched_pattern", hit["value"]),
                    "was_delete": False,
                })
                prepend_idx  = None
                seg_start    = P + 1
                pending_name = NO_CODE_TRIGGER
                pending_pat  = NO_CODE_TRIGGER

            # "end_delete" hits: close current segment at P-1 (P deleted), named by hit value
            for hit in [h for h in hits if h.get("page_handling", "start") == "end_delete"]:
                pages = (([prepend_idx] if prepend_idx is not None else [])
                         + list(range(seg_start, P)))
                output_specs.append({
                    "pages": pages, "trigger": hit["value"],
                    "pattern": hit.get("matched_pattern", hit["value"]),
                    "was_delete": True,
                })
                prepend_idx  = None
                seg_start    = P + 1
                pending_name = NO_CODE_TRIGGER
                pending_pat  = NO_CODE_TRIGGER

            # "start" / "delete" hits: close preceding segment, start a new one
            # ("end" and "end_delete" hits are fully handled by the two loops
            #  above and must not be processed a second time here — otherwise
            #  the following segment inherits the trigger name instead of
            #  falling back to no_code.)
            for hit in [h for h in hits
                        if h.get("page_handling", "start") not in ("end", "end_delete")]:
                H = hit.get("page_handling", "start")
                # Flush content up to P-1 (exclusive of trigger page)
                _flush(P, pending_name, pending_pat,
                       was_delete=(H == "delete" and seg_start == P))
                seg_start    = P + 1
                pending_name = hit["value"]
                pending_pat  = hit.get("matched_pattern", hit["value"])
                if H == "start":
                    prepend_idx = P   # trigger page will be first of the new segment
                # else "delete": trigger page discarded (prepend_idx stays None)

        # Flush final remaining segment
        _flush(total, pending_name, pending_pat)

        # ── Write output documents ────────────────────────────────────────────
        now      = datetime.now()
        produced = 0
        hook_outputs  = []   # absolute paths of produced files (for hook)
        hook_page_ranges = []  # parallel page-range strings for task API
        hook_triggers = []   # trigger codes detected (for hook)

        for spec in output_specs:
            page_indices    = spec["pages"]
            trigger         = spec["trigger"]
            matched_pattern = spec["pattern"]

            if not page_indices:
                # Empty segment: two consecutive triggers with nothing between them
                if spec.get("was_delete"):
                    log_event("warning",
                              t("log.empty_after_removal", trigger=trigger, page="?"),
                              fname)
                continue

            writer = PdfWriter()
            for p in page_indices:
                writer.add_page(reader.pages[p])

            out_dir  = get_output_dir(trigger, cfg, matched_pattern)
            cnt      = next_counter()
            sep      = cfg.get("filename_separator", "_")
            stem     = build_filename(trigger, tokens, counter=cnt, now=now, separator=sep)
            out_path = out_dir / f"{stem}.pdf"

            attempt = 0
            while out_path.exists():
                attempt += 1
                out_path = out_dir / f"{stem}_{attempt:03d}.pdf"

            add_pdf_metadata(writer, trigger, out_path.name, now)

            with open(out_path, "wb") as f:
                writer.write(f)
            os.chmod(out_path, 0o664)
            first_page = page_indices[0] + 1   # 1-indexed for log
            end_page   = page_indices[-1] + 1
            pages_str  = (f"page {first_page}"
                          if first_page == end_page
                          else f"pages {first_page}\u2013{end_page}")
            log_event("info",
                      t("log.split_output", path=out_path.relative_to(OUTPUT_DIR.parent),
                        pages=pages_str),
                      fname)
            produced += 1
            # Build human-readable page-range string (1-based) for API
            _idxs = [_p + 1 for _p in page_indices]
            if len(_idxs) == 1:
                _pr = f"page {_idxs[0]}"
            else:
                _pr = f"pages {_idxs[0]}–{_idxs[-1]}"
            hook_page_ranges.append(_pr)
            hook_outputs.append(str(out_path))
            if trigger not in hook_triggers:
                hook_triggers.append(trigger)

        # Source file: delete or archive
        if delete_src:
            pdf_path.unlink()
            log_event("info", t("log.source_deleted", filename=fname, produced=produced), fname)
        elif archive_src:
            PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
            ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = PROCESSED_DIR / f"{pdf_path.stem}_{ts}{pdf_path.suffix}"
            shutil.copy2(str(pdf_path), str(dest))
            os.chmod(dest, 0o664)
            pdf_path.unlink()
            log_event("info",
                      t("log.source_archived", filename=fname, produced=produced, dest=dest),
                      fname)

        # Post-processing hook + webhook — success
        _task_update(
            fname, status="success",
            triggers=hook_triggers,
            outputs=[
                {"filename": Path(p).name,
                 "path":     str(Path(p).relative_to(DATA_DIR)),
                 "download_url": f"/api/file/{Path(p).relative_to(DATA_DIR)}",
                 "pages":    pr}
                for p, pr in zip(hook_outputs, hook_page_ranges)
            ],
            docs_count=produced,
        )
        _run_post_process_hook(
            source_file=fname, status="success",
            triggers=hook_triggers, outputs=hook_outputs, docs_count=produced,
        )
        _fire_webhook(
            source_file=fname, status="success",
            triggers=hook_triggers, outputs=hook_outputs, docs_count=produced,
            config_override=_file_override,
        )

        with state_lock:
            s = state["stats"]
            s["processed"]  += 1
            s["split_docs"] += produced
            s["last_file"]   = fname
            s["last_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_stats(dict(s))

    except Exception as e:
        log_event("error", t("log.unexpected_error", filename=fname, message=e), fname)
        with state_lock:
            state["stats"]["errors"] += 1
            _save_stats(dict(state["stats"]))
        if pdf_path.exists():
            move_to_error(pdf_path, str(e))
        # Post-processing hook + webhook — error
        _task_update(fname, status="error", error=str(e))
        _run_post_process_hook(source_file=fname, status="error", error_msg=str(e))
        _fire_webhook(source_file=fname, status="error", error_msg=str(e),
                      config_override=_file_override)

    finally:
        _processing_semaphore.release()
        with processing_lock:
            processing.discard(fname)
        with state_lock:
            state["queue"].pop(fname, None)   # O(1) vs O(n) list.remove


