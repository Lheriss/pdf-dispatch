"""
test_blank_split.py — blank-page separators through the real pipeline.

Rasterisation is replaced by prepared images (white = blank sheet, a dark
block = content) so the test needs no poppler and runs in CI; everything
else — blank detection, run grouping, segment building, writing the output
PDFs — is the production code path (find_split_pages + process_file).

Regression covered: in a run of consecutive blank pages (duplex-scanned
separator), only the first blank was discarded; the following ones were
kept as ordinary pages and ended up in the next document.
"""

import io

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

import app as _app
from dispatch import processing

W, H = 400, 560   # small A4-ratio images are enough for the ink measurement


def _image(kind: str) -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    if kind == "content":
        # A dark block in the middle: far above the blank threshold.
        img.paste((20, 20, 20), (80, 120, W - 80, H - 160))
    return img


def _pdf(n_pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def layout(monkeypatch):
    """Install a page layout: list of "content" / "blank" for the next PDF."""
    state = {"kinds": []}

    def fake_convert(path, dpi=None, first_page=None, last_page=None, **_kw):
        imgs = [_image(k) for k in state["kinds"]]
        if first_page is not None:
            imgs = imgs[first_page - 1:(last_page or first_page)]
        return imgs

    monkeypatch.setattr(processing, "convert_from_path", fake_convert)
    monkeypatch.setattr(processing, "BLANK_PAGE_SPLIT", True)
    monkeypatch.setattr(processing, "FILE_STABLE_INTERVAL", 0.05, raising=False)

    def set_kinds(kinds):
        state["kinds"] = list(kinds)
    return set_kinds


@pytest.fixture
def clean_config(client):
    """No trigger codes: only blank pages drive the splitting."""
    before = _app.get_config()
    _app.update_config({"split_values": [], "separator_placement": "before",
                        "subdirs_by_trigger": False})
    yield
    _app.update_config({"split_values": before["split_values"],
                        "separator_placement": before["separator_placement"],
                        "subdirs_by_trigger": before["subdirs_by_trigger"]})


def _outputs():
    out = processing.OUTPUT_DIR
    return {p for p in out.rglob("*.pdf")
            if not any(part in ("processed", "error") for part in p.relative_to(out).parts)}


def _process(kinds, name):
    path = processing.INPUT_DIR / f"{name}.pdf"
    path.write_bytes(_pdf(len(kinds)))
    before = _outputs()
    processing.process_file(path)
    new = sorted(_outputs() - before)
    return [len(PdfReader(str(p)).pages) for p in new]


class TestFindSplitPages:
    def test_duplex_run_gives_one_split_and_one_drop(self, layout, tmp_path):
        layout(["content", "blank", "blank", "content"])
        f = tmp_path / "x.pdf"; f.write_bytes(_pdf(4))
        hits = processing.find_split_pages(f, [], "before")
        assert [(h["page"], h["page_handling"]) for h in hits] == [(1, "delete"), (2, "drop")]

    def test_single_blank_has_no_drop(self, layout, tmp_path):
        layout(["content", "blank", "content"])
        f = tmp_path / "x.pdf"; f.write_bytes(_pdf(3))
        hits = processing.find_split_pages(f, [], "before")
        assert [h["page_handling"] for h in hits] == ["delete"]


class TestBlankSeparatorPipeline:
    def test_single_blank_separator(self, layout, clean_config):
        layout(["content", "blank", "content", "content"])
        assert sorted(_process(["content", "blank", "content", "content"], "single")) == [1, 2]

    def test_duplex_separator_both_blanks_discarded(self, layout, clean_config):
        """[content, BLANK, BLANK, content] → 2 documents of 1 page each."""
        kinds = ["content", "blank", "blank", "content"]
        layout(kinds)
        pages = _process(kinds, "duplex")
        assert len(pages) == 2, f"expected 2 documents, got {pages}"
        assert sum(pages) == 2, f"both blank sheets must be discarded, got {pages}"

    def test_triple_blank_run(self, layout, clean_config):
        kinds = ["content", "blank", "blank", "blank", "content", "content"]
        layout(kinds)
        assert sorted(_process(kinds, "triple")) == [1, 2]

    def test_leading_duplex_blanks(self, layout, clean_config):
        kinds = ["blank", "blank", "content", "content"]
        layout(kinds)
        assert _process(kinds, "leading") == [2]

    def test_after_placement(self, layout, clean_config):
        _app.update_config({"separator_placement": "after"})
        kinds = ["content", "blank", "blank", "content"]
        layout(kinds)
        pages = _process(kinds, "after")
        assert sorted(pages) == [1, 1], f"got {pages}"
