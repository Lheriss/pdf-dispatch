"""
dispatch/routes/detect.py
Blueprint: barcode detection diagnostics.

POST /api/detect analyses an uploaded PDF *without* feeding it into the
processing pipeline: nothing is written to /data/input/, no document is
produced, no statistics are updated. The response reports, page by page,
every code found (value, symbology, position), whether it survives the
production two-pass scan (BARCODE_DPI_SCAN gate → full-DPI verification),
and whether its value matches a configured trigger.

Intended for the "Detection test" panel in the web interface: it turns
"why is my barcode not detected?" from guesswork into a 30-second check.
"""
import os
import tempfile
from pathlib import Path

from flask import Blueprint, jsonify, request

from dispatch.config import (
    BARCODE_DPI_SCAN, DPI, MAX_PAGES, MAX_UPLOAD_MB, SCANNER, UPSCALE,
    BLANK_PAGE_SPLIT, BLANK_PAGE_THRESHOLD,
    _is_glob_pattern, _match_trigger, get_config, log,
)
from dispatch.i18n import t
from dispatch.processing import decode_page_detailed, blank_page_metrics

bp = Blueprint("detect", __name__)

# Diagnostic cap: unlike production, /api/detect rasterises EVERY page at
# full DPI (so it can flag codes that would be missed by the fast-scan
# gate). This is expensive, so the analysis is limited to the first
# DETECT_MAX_PAGES pages — plenty for testing a separator sheet.
DETECT_MAX_PAGES = int(os.getenv("DETECT_MAX_PAGES", "10"))


def _upscale_img(img):
    if UPSCALE > 1.0:
        from PIL import Image
        return img.resize((int(img.width * UPSCALE), int(img.height * UPSCALE)),
                          Image.LANCZOS)
    return img


def _match_info(code_value: str, trigger_map: list, permissive: bool,
                separator_placement: str) -> tuple[list, bool]:
    """Return (matches, would_split) for a decoded value against the
    currently configured triggers. Mirrors find_split_pages semantics:
    permissive mode (empty trigger list) splits on every code."""
    _after = separator_placement == "after"

    def _effective(keep: bool) -> str:
        return ("end" if _after and keep else
                "end_delete" if _after and not keep else
                "start" if keep else "delete")

    if permissive:
        return ([{
            "pattern":        code_value,
            "is_glob":        False,
            "page_handling":  "keep",
            "case_sensitive": True,
            "effective":      _effective(True),
            "permissive":     True,
        }], True)

    matches = []
    for trig in trigger_map:
        if _match_trigger(code_value, trig):
            keep = trig.get("page_handling", "keep") == "keep"
            matches.append({
                "pattern":        trig.get("value", ""),
                "is_glob":        _is_glob_pattern(trig.get("value", "")),
                "page_handling":  trig.get("page_handling", "keep"),
                "case_sensitive": trig.get("case_sensitive", True),
                "effective":      _effective(keep),
                "permissive":     False,
            })
    return matches, bool(matches)


@bp.route("/api/detect", methods=["POST"])
def api_detect():
    """POST /api/detect — Diagnose barcode detection on an uploaded PDF.

    Multipart form field: file (a PDF; typically a single separator page).
    The file is analysed in a temporary location and never enters the
    processing pipeline.

    Response (200):
      ok                  — true
      filename            — uploaded filename
      scanner             — ZXING | ZBAR
      dpi_scan / dpi      — pass-1 / pass-2 DPI (equal = single-pass mode)
      upscale             — BARCODE_UPSCALE factor applied before decoding
      permissive          — true when the trigger list is empty
      separator_placement — before | after (current config)
      pages_total         — page count of the uploaded PDF
      pages_analyzed      — pages actually rasterised (capped)
      truncated           — true when pages_total > pages_analyzed
      pages               — list of per-page results:
        page (1-based), width, height (pixels at full DPI, after upscale),
        codes: list of {value, type, bbox{x,y,w,h}, at_scan_dpi,
                        at_full_dpi, production_detected, would_split,
                        matches[...]}

    production_detected mirrors the real pipeline: the page must be
    positive during the fast scan (or single-pass mode), and the code must
    decode at full DPI. A code with at_full_dpi=true but
    production_detected=false means the fast-scan gate would drop it —
    raise BARCODE_DPI_SCAN or improve print quality.

    Errors: 400 no/invalid file, 413 file exceeds MAX_UPLOAD_MB.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": t("upload.error_no_file")}), 400
    up = request.files["file"]
    fname = Path(up.filename or "document.pdf").name

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    data = up.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        return jsonify({"ok": False,
                        "error": f"file exceeds the {MAX_UPLOAD_MB} MB upload limit"}), 413
    if not data.startswith(b"%PDF"):
        return jsonify({"ok": False, "error": t("upload.error_not_pdf",
                                                filename=fname)}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()

        from pypdf import PdfReader
        try:
            pages_total = len(PdfReader(tmp.name).pages)
        except Exception:
            return jsonify({"ok": False, "error": t("log.reason_invalid_pdf")}), 400

        cap = min(pages_total, DETECT_MAX_PAGES, MAX_PAGES)

        cfg = get_config()
        sv_list     = cfg.get("split_values", [])
        trigger_map = [tr for tr in sv_list
                       if isinstance(tr, dict) and "value" in tr]
        permissive  = not trigger_map
        placement   = cfg.get("separator_placement", "before")

        from pdf2image import convert_from_path
        single_pass = BARCODE_DPI_SCAN == DPI

        # Pass 1 — fast scan (same DPI as production pass 1)
        fast_imgs = [_upscale_img(i) for i in
                     convert_from_path(tmp.name, dpi=BARCODE_DPI_SCAN,
                                       first_page=1, last_page=cap)]
        fast_values = [set(c["value"] for c in decode_page_detailed(img))
                       for img in fast_imgs]

        # Pass 2 — full DPI on EVERY analysed page (diagnostic superset of
        # production, which only re-scans fast-positive pages)
        pages_out = []
        for i in range(cap):
            if single_pass:
                img = fast_imgs[i]
            else:
                pi  = convert_from_path(tmp.name, dpi=DPI,
                                        first_page=i + 1, last_page=i + 1)
                img = _upscale_img(pi[0]) if pi else None

            codes_out = []
            full_detailed = decode_page_detailed(img) if img is not None else []
            page_fast_positive = bool(fast_values[i])
            seen_full = set()
            for c in full_detailed:
                seen_full.add(c["value"])
                matches, would_split = _match_info(c["value"], trigger_map,
                                                   permissive, placement)
                at_scan = c["value"] in fast_values[i]
                codes_out.append({
                    "value":               c["value"],
                    "type":                c["type"],
                    "bbox":                c["bbox"],
                    "at_scan_dpi":         at_scan,
                    "at_full_dpi":         True,
                    "production_detected": page_fast_positive or single_pass,
                    "would_split":         would_split
                                           and (page_fast_positive or single_pass),
                    "matches":             matches,
                })
            # Codes found ONLY by the fast scan (rare: full-DPI decode lost
            # them) — production would lose them too.
            for v in fast_values[i] - seen_full:
                matches, would_split = _match_info(v, trigger_map,
                                                   permissive, placement)
                codes_out.append({
                    "value":               v,
                    "type":                "unknown",
                    "bbox":                None,
                    "at_scan_dpi":         True,
                    "at_full_dpi":         False,
                    "production_detected": single_pass,
                    "would_split":         would_split and single_pass,
                    "matches":             matches,
                })

            # Blank-page diagnostic — measured on the fast-scan image, as the
            # production pipeline does. Exposes ink_ratio and the threshold so
            # the panel can be used to calibrate BLANK_PAGE_THRESHOLD. A page
            # with a code is never a blank separator, so is_blank is forced
            # false there (matches production).
            bm = blank_page_metrics(fast_imgs[i])
            has_code = bool(codes_out)
            is_blank = (not has_code) and bm["ink_ratio"] < BLANK_PAGE_THRESHOLD
            blank_info = {
                "ink_ratio":        round(bm["ink_ratio"], 5),
                "mean":             bm["mean"],
                "threshold_used":   BLANK_PAGE_THRESHOLD,
                "is_blank":         is_blank,
                "would_split_here": is_blank and BLANK_PAGE_SPLIT,
            }

            pages_out.append({
                "page":   i + 1,
                "width":  img.width  if img is not None else 0,
                "height": img.height if img is not None else 0,
                "codes":  codes_out,
                "blank":  blank_info,
            })

        return jsonify({
            "ok":                  True,
            "filename":            fname,
            "scanner":             SCANNER,
            "dpi_scan":            BARCODE_DPI_SCAN,
            "dpi":                 DPI,
            "upscale":             UPSCALE,
            "permissive":          permissive,
            "separator_placement": placement,
            "blank_page_split":    BLANK_PAGE_SPLIT,
            "blank_page_threshold": BLANK_PAGE_THRESHOLD,
            "pages_total":         pages_total,
            "pages_analyzed":      cap,
            "truncated":           pages_total > cap,
            "pages":               pages_out,
        })
    except Exception as e:
        log.error(f"/api/detect failed for {fname}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
