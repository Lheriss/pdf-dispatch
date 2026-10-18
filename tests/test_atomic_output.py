"""
test_atomic_output.py — split outputs only ever appear complete.

Regression (pdf-dispatch-tester run of 2026-10-18): a program watching the
output folder read a document while pdf-dispatch was still writing it
(pypdf could not parse it). Outputs are now written to a hidden ".part"
file and renamed into place.
"""

import io
import stat

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

import app as _app   # noqa: F401  (initialises the app)
from dispatch import processing


def _writer(pages=2):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    return w


class TestWritePdfAtomic:
    def test_final_file_complete_and_no_leftover(self, tmp_path):
        out = tmp_path / "DOC_001.pdf"
        processing.write_pdf_atomic(_writer(3), out)
        assert len(PdfReader(str(out)).pages) == 3
        assert [p.name for p in tmp_path.iterdir()] == ["DOC_001.pdf"]
        assert stat.S_IMODE(out.stat().st_mode) == 0o664

    def test_final_name_absent_while_writing(self, tmp_path):
        out = tmp_path / "DOC_002.pdf"
        seen = {}

        class Spy(PdfWriter):
            def write(self, stream):
                seen["final_exists"] = out.exists()
                seen["tmp_name"] = getattr(stream, "name", "")
                return super().write(stream)

        w = Spy()
        w.add_blank_page(width=595, height=842)
        processing.write_pdf_atomic(w, out)
        assert seen["final_exists"] is False
        assert seen["tmp_name"].endswith("/.DOC_002.pdf.part")
        assert out.exists()

    def test_failure_leaves_nothing(self, tmp_path):
        out = tmp_path / "DOC_003.pdf"

        class Boom(PdfWriter):
            def write(self, stream):
                stream.write(b"%PDF-1.7 partial")
                raise OSError("disk full")

        with pytest.raises(OSError):
            processing.write_pdf_atomic(Boom(), out)
        assert list(tmp_path.iterdir()) == []

    def test_temporary_name_is_not_a_pdf(self, tmp_path):
        # Watchers filter on *.pdf: the temporary file must not match.
        p = processing._partial_path(tmp_path / "X.pdf")
        assert p.name.startswith(".") and not p.name.endswith(".pdf")


class TestStalePartials:
    def test_only_partials_removed(self, tmp_path):
        sub = tmp_path / "FK3"
        sub.mkdir()
        keep = [tmp_path / "a.pdf", sub / "b.pdf", sub / ".hidden.pdf", sub / "c.pdf.part"]
        drop = [tmp_path / ".a2.pdf.part", sub / ".b2.pdf.part"]
        for p in keep + drop:
            p.write_bytes(b"x")
        assert processing.remove_stale_partials(tmp_path) == 2
        assert all(p.exists() for p in keep)
        assert not any(p.exists() for p in drop)


class TestPipeline:
    def test_process_file_leaves_no_partial(self, client, monkeypatch):
        # Real pipeline with rendering stubbed: two content pages around a
        # blank separator → two outputs, both complete, no ".part" left.
        def img(blank):
            im = Image.new("RGB", (400, 560), "white")
            if not blank:
                im.paste((20, 20, 20), (80, 120, 320, 400))
            return im
        kinds = [False, True, False]

        def fake_convert(path, dpi=None, first_page=None, last_page=None, **_kw):
            imgs = [img(b) for b in kinds]
            if first_page is not None:
                imgs = imgs[first_page - 1:(last_page or first_page)]
            return imgs

        monkeypatch.setattr(processing, "convert_from_path", fake_convert)
        monkeypatch.setattr(processing, "BLANK_PAGE_SPLIT", True)
        monkeypatch.setattr(processing, "FILE_STABLE_INTERVAL", 0.05, raising=False)
        before_cfg = _app.get_config()
        _app.update_config({"split_values": [], "separator_placement": "before",
                            "subdirs_by_trigger": False})
        calls = []
        real = processing.write_pdf_atomic
        monkeypatch.setattr(processing, "write_pdf_atomic",
                            lambda w, p: (calls.append(p), real(w, p))[1])
        try:
            buf = io.BytesIO()
            _writer(3).write(buf)
            src = processing.INPUT_DIR / "atomic_pipeline.pdf"
            src.write_bytes(buf.getvalue())
            processing.process_file(src)
        finally:
            _app.update_config({k: before_cfg[k] for k in
                                ("split_values", "separator_placement", "subdirs_by_trigger")})
        assert len(calls) == 2                       # every output goes through it
        assert all(p.exists() and len(PdfReader(str(p)).pages) == 1 for p in calls)
        assert not list(processing.OUTPUT_DIR.rglob("*.part"))
