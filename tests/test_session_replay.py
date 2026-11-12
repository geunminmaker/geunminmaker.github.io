"""Session behaviour the web page relies on when it restarts the engine."""
import io

import pytest
from PIL import Image

from splitcore.formats import pdf as pdf_format
from splitcore.session import Session

fitz = pytest.importorskip("fitz")


def _png(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buffer, "PNG")
    return buffer.getvalue()


def _pdf(pages: int) -> bytes:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=200, height=280)
        page.insert_text((40, 80), f"page {index + 1}", fontsize=20)
    data = doc.tobytes()
    doc.close()
    return data


def test_ids_start_over_after_clear():
    """open A, clear, open B: B must get the id it would get in a fresh engine (replay)."""
    session = Session()
    first = session.add_file("a.png", _png("red"))
    assert [s["id"] for s in first["sources"]] == ["s1"]
    session.clear()
    second = session.add_file("b.png", _png("blue"))
    fresh = Session().add_file("b.png", _png("blue"))
    assert [s["id"] for s in second["sources"]] == [s["id"] for s in fresh["sources"]] == ["s1"]


def test_ids_continue_within_one_session():
    session = Session()
    session.add_file("a.png", _png("red"))
    more = session.add_file("b.png", _png("blue"))
    assert [s["id"] for s in more["sources"]] == ["s2"]


def test_long_pdf_is_reported_as_truncated(monkeypatch):
    monkeypatch.setattr(pdf_format, "MAX_PAGES", 5)
    session = Session()
    result = session.add_file("long.pdf", _pdf(8))
    assert len(result["sources"]) == 5
    assert result["truncated"] is True
    assert result["total"] == 8


def test_short_pdf_is_not_truncated():
    result = Session().add_file("short.pdf", _pdf(3))
    assert len(result["sources"]) == 3
    assert result["truncated"] is False
    assert result["total"] == 3
