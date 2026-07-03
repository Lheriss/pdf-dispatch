"""
dispatch/routes/separator.py
Blueprint: separator page generation.
"""
import re

from flask import Blueprint, Response, abort, request

from dispatch.config import get_config, _is_glob_pattern
from dispatch.processing import generate_separator_pdf

bp = Blueprint("separator", __name__)

@bp.route("/api/separator/<int:trigger_idx>")
def api_separator(trigger_idx: int):
    """Generate and return the separator PDF for the given trigger index."""
    from flask import Response, abort
    cfg = get_config()
    sv  = cfg.get("split_values", [])
    if trigger_idx < 0 or trigger_idx >= len(sv):
        abort(404)
    trig = sv[trigger_idx]
    if not isinstance(trig, dict):
        abort(400)
    value    = trig.get("value", "")
    if _is_glob_pattern(value):
        abort(400, "Glob pattern — value undefined, cannot generate a separator.")
    code_type = request.args.get("type", "qr")  # ?type=qr or ?type=barcode
    try:
        pdf_bytes = generate_separator_pdf(value, code_type)
    except Exception as e:
        abort(500, str(e))
    safe_name = re.sub(r"[^\w\-.]", "_", value)
    filename  = f"separator_{safe_name}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )



