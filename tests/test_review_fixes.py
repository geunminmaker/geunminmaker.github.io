"""Regression tests for the engine review findings (E1 ... E12) and the wording rule (P11).

Every test reproduces the reviewed scenario first: it fails on the code as it was reviewed.
"""
from __future__ import annotations

import io
import os
import re
import sys
import types
import zipfile

import pymupdf
import pytest
from PIL import Image, ImageChops, ImageFile, ImageStat

import splitcore
from splitcore import bridge, imaging
from splitcore.errors import SplitError
from splitcore.formats import common as common_format
from splitcore.formats.mupdf_image import load_with_mupdf
from splitcore.session import Session
from splitcore.sources import raster_sources

RED, GREEN, BLUE, YELLOW = (220, 40, 40), (40, 180, 70), (40, 80, 220), (240, 200, 20)

#: what a viewer does for each value of the TIFF / EXIF Orientation tag
UPRIGHT = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def near(a, b, tol=8) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


def to_bytes(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def png(colour, size=(320, 240)) -> bytes:
    return to_bytes(Image.new("RGB", size, colour), "PNG")


def make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def corners_picture(w: int, h: int) -> Image.Image:
    """Four different corners, so every turn and every mirror image is told apart."""
    im = Image.new("RGB", (w, h), "white")
    im.paste(RED, (0, 0, w // 3, h // 3))
    im.paste(GREEN, (w - w // 3, 0, w, h // 3))
    im.paste(BLUE, (0, h - h // 3, w // 3, h))
    im.paste(YELLOW, (w - w // 3, h - h // 3, w, h))
    return im


def corner_colours(im: Image.Image) -> list:
    w, h = im.size
    im = im.convert("RGB")
    return [im.getpixel(p) for p in ((5, 5), (w - 6, 5), (5, h - 6), (w - 6, h - 6))]


def same_corners(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and all(near(x, y) for x, y in zip(corner_colours(a), corner_colours(b)))


def turned(stored: Image.Image, orientation: int) -> Image.Image:
    method = UPRIGHT[orientation]
    return stored if method is None else stored.transpose(method)


def mean_difference(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")).mean[0]


@pytest.fixture()
def session():
    s = Session()
    yield s
    s.clear()


def small_pdf(width=200, height=280) -> bytes:
    """One page: the top-left quarter is red, the rest white with a word on it."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(pymupdf.Rect(0, 0, width / 2, height / 2), fill=tuple(c / 255 for c in RED), color=None)
    page.insert_text((width / 2 + 10, height * 0.75), "hello", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------- E1

def pdf_with_dangling_reference() -> bytes:
    """A healthy page whose resources name an object number the file does not define.

    That is legal (PDF 32000-1, 7.3.10: such a reference is the null object) and every viewer
    shows the page, but MuPDF refuses to copy the page into another document.
    """
    doc = pymupdf.open(stream=small_pdf(), filetype="pdf")
    page = doc[0]
    kind, value = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        doc.xref_set_key(int(value.split()[0]), "ExtGState", "<< /GSunused 99999 0 R >>")
    else:
        doc.xref_set_key(page.xref, "Resources/ExtGState", "<< /GSunused 99999 0 R >>")
    data = doc.tobytes()
    doc.close()
    return data


def test_e1_pdf_that_cannot_be_copied_as_vector_is_exported_as_pictures(session):
    data = pdf_with_dangling_reference()
    assert not pymupdf.open(stream=data, filetype="pdf").is_repaired  # an ordinary, undamaged file
    sid = session.add_file("dangling.pdf", data)["sources"][0]["id"]
    seen = []
    (out,) = list(session.export(sid, 2, 2, "pdf", {}, lambda done, total: seen.append((done, total))))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc.page_count == 4 and seen[-1] == (4, 4)
    assert all(len(page.get_images()) == 1 for page in doc)  # the raster writer took over
    first = doc[0].get_pixmap(dpi=20, alpha=False)
    assert near(first.pixel(first.width // 2, first.height // 2), RED)
    last = doc[3].get_pixmap(dpi=20, alpha=False)
    assert near(last.pixel(3, 3), (255, 255, 255))


def test_e1_healthy_pdf_stays_vector(session):
    sid = session.add_file("ok.pdf", small_pdf())["sources"][0]["id"]
    (out,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc.page_count == 4 and not any(page.get_images() for page in doc)


# ---------------------------------------------------------------- E2

@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_e2_turned_tiff_reports_the_upright_size(session, orientation):
    stored = corners_picture(240, 120)
    upright = turned(stored, orientation)
    assert upright.size == (120, 240)
    meta = session.add_file("scan.tif", to_bytes(stored, "TIFF", tiffinfo={274: orientation}))["sources"][0]
    assert (meta["width"], meta["height"]) == (120, 240)
    assert session.all_layouts(meta["id"])["6"][0]["key"] == "2x3"  # a portrait scan gets more rows
    plan = session.describe(meta["id"], 3, 2)["plan"]
    tiles = [Image.open(io.BytesIO(f.data)) for f in session.export(meta["id"], 3, 2, "png")]
    assert len(tiles) == 6 and all(t.size == (plan["out_w"], plan["out_h"]) for t in tiles)
    mosaic = Image.new("RGB", (120, 240))
    for index, tile in enumerate(tiles):
        mosaic.paste(tile.convert("RGB").resize((60, 80), Image.BOX), ((index % 2) * 60, (index // 2) * 80))
    assert same_corners(mosaic, upright) and mean_difference(mosaic, upright) < 3
    assert session.get(meta["id"]).size == (120, 240)  # the same before and after decoding


def test_e2_every_page_of_a_turned_tiff_and_other_orientations(session):
    frames = [corners_picture(240, 120), Image.new("RGB", (240, 120), BLUE)]
    data = to_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:], tiffinfo={274: 6})
    found = raster_sources(data, origin="scan.tif")
    assert [s.size for s in found] == [(120, 240), (120, 240)]
    assert same_corners(found[0].image(), turned(frames[0], 6))
    for orientation in (1, 2, 3, 4):  # no quarter turn: the size is the stored one
        meta = session.add_file("flat.tif", to_bytes(frames[0], "TIFF", tiffinfo={274: orientation}))["sources"][0]
        assert (meta["width"], meta["height"]) == (240, 120)
        assert same_corners(session.get(meta["id"]).image(), turned(frames[0], orientation))


# ---------------------------------------------------------------- E3

@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_e3_mupdf_fallback_shows_the_picture_like_every_viewer(orientation):
    stored = corners_picture(300, 200)
    upright = turned(stored, orientation)
    data = to_bytes(stored, "TIFF", compression="tiff_adobe_deflate", tiffinfo={274: orientation}, dpi=(300, 300))
    viewer = Image.open(io.BytesIO(data))
    viewer.load()
    assert same_corners(viewer, upright)  # the table above is what Pillow itself shows
    (source,) = load_with_mupdf(data, "scan.tif")
    assert source.size == upright.size
    assert same_corners(source.image(), upright), (orientation, corner_colours(source.image()))


def test_e3_mupdf_fallback_turns_every_page():
    frames = [corners_picture(300, 200), corners_picture(300, 200).transpose(Image.Transpose.FLIP_LEFT_RIGHT)]
    data = to_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:], compression="tiff_adobe_deflate",
                    tiffinfo={274: 8})
    found = load_with_mupdf(data, "scan.tif")
    assert [s.size for s in found] == [(200, 300), (200, 300)]
    for source, frame in zip(found, frames):
        assert same_corners(source.image(), turned(frame, 8))


# ---------------------------------------------------------------- E4 / E6

#: looks like an iPhone photo to the sniffer; the stand-in decoder below reads it as 64 x 48 red pixels
FAKE_HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20 + bytes(RED) * (64 * 48)
FAKE_AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 300


class _StandInHeifFile(ImageFile.ImageFile):
    format = "HEIF"
    format_description = "stand-in for pillow-heif"

    def _open(self):
        if self.fp.read(12)[4:12] != b"ftypheic":
            raise SyntaxError("not HEIF")
        self._size = (64, 48)
        self._mode = "RGB"
        tile = getattr(ImageFile, "_Tile", lambda *parts: parts)
        self.tile = [tile("raw", (0, 0, 64, 48), 32, ("RGB", 0, 1))]


@pytest.fixture()
def heif(monkeypatch):
    """pillow-heif as the browser has it: missing at first, importable once the page has loaded it."""
    monkeypatch.setattr(imaging, "_heif_state", dict.fromkeys(imaging._heif_state, False))
    Image.init()  # every real decoder is registered before the tables are copied for this test
    monkeypatch.setattr(Image, "OPEN", dict(Image.OPEN))
    monkeypatch.setattr(Image, "ID", list(Image.ID))
    monkeypatch.setitem(sys.modules, "pillow_heif", None)  # "import pillow_heif" fails
    calls = []

    def arrive(decodes: bool = True):
        def register():
            calls.append("registered")
            if decodes:
                Image.register_open("HEIF", _StandInHeifFile, lambda head: head[4:12] == b"ftypheic")

        fake = types.ModuleType("pillow_heif")
        fake.options = types.SimpleNamespace(DECODE_THREADS=4)
        fake.register_heif_opener = register
        monkeypatch.setitem(sys.modules, "pillow_heif", fake)
        return fake

    return types.SimpleNamespace(arrive=arrive, calls=calls)


def test_e4_heic_decoder_is_looked_for_again_after_a_failed_attempt(heif):
    assert imaging.enable_heif() is False
    assert imaging.enable_heif() is False
    fake = heif.arrive()
    assert imaging.enable_heif() is True  # the package came later: it must not stay "failed"
    assert fake.options.DECODE_THREADS == 0
    assert imaging.enable_heif() is True and heif.calls == ["registered"]  # success is remembered


def test_e4_heic_opens_after_a_zip_with_a_heic_was_refused(heif):
    with pytest.raises(SplitError):
        Session().add_file("photos.zip", make_zip({"IMG_0001.HEIC": FAKE_HEIC}))
    with pytest.raises(SplitError) as err:  # no decoder yet: the page tries the browser's own
        Session().add_file("IMG_0001.HEIC", FAKE_HEIC)
    assert err.value.code == "UNSUPPORTED"
    heif.arrive()  # the page loads pillow-heif when a .heic is picked (bridge.probe names it)
    assert bridge.probe("IMG_0001.HEIC", FAKE_HEIC[:2048])["packages"] == ["pillow-heif"]
    meta = Session().add_file("IMG_0001.HEIC", FAKE_HEIC)["sources"][0]
    assert (meta["width"], meta["height"]) == (64, 48)


def test_e6_heic_inside_a_zip_asks_for_the_decoder_and_then_opens(heif, session):
    data = make_zip({"IMG_0001.HEIC": FAKE_HEIC, "notes.txt": b"hi"})
    with pytest.raises(SplitError) as err:
        session.add_file("photos.zip", data)
    assert (err.value.code, err.value.detail) == ("NEED_PACKAGE", "pillow-heif")
    answer = bridge.dispatch("add_file", {"name": "photos.zip", "data": data})
    assert answer["ok"] is False and answer["error"]["code"] == "NEED_PACKAGE"
    assert answer["error"]["detail"] == "pillow-heif"  # the page loads exactly this package and retries

    heif.arrive()
    result = session.add_file("photos.zip", data)  # the retry
    assert [(m["label"], m["width"], m["height"]) for m in result["sources"]] == [("IMG_0001.HEIC", 64, 48)]
    assert near(session.get(result["sources"][0]["id"]).image().getpixel((10, 10)), RED)


def test_e6_heic_inside_a_document_asks_for_the_decoder(heif, session):
    document = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>",
                         "word/media/image1.heic": FAKE_HEIC})
    with pytest.raises(SplitError) as err:
        session.add_file("report.docx", document)
    assert (err.value.code, err.value.detail) == ("NEED_PACKAGE", "pillow-heif")


def test_e6_pictures_that_cannot_be_decoded_are_not_called_missing(heif, session):
    heif.arrive(decodes=False)  # the decoder is there and still cannot read the picture
    with pytest.raises(SplitError) as err:
        session.add_file("photos.zip", make_zip({"IMG_0001.HEIC": FAKE_HEIC}))
    assert err.value.code == "NO_IMAGE"
    assert "HEIC" in err.value.message and "골라 주세요" in err.value.message
    assert "찾지 못했어요" not in err.value.message

    with pytest.raises(SplitError) as err:  # AVIF: no package can help, say what the pictures are
        session.add_file("web.zip", make_zip({"a.avif": FAKE_AVIF}))
    assert err.value.code == "NO_IMAGE" and "AVIF" in err.value.message
    assert "찾지 못했어요" not in err.value.message


def test_e6_readable_pictures_next_to_a_heic_are_offered_as_before(heif, session):
    result = session.add_file("mixed.zip", make_zip({"a.png": png(GREEN), "IMG_0001.HEIC": FAKE_HEIC}))
    assert [m["label"] for m in result["sources"]] == ["a.png"]
    with pytest.raises(SplitError) as err:  # nothing at all: still the plain message
        session.add_file("empty.zip", make_zip({"a.txt": b"x"}))
    assert err.value.code == "NO_IMAGE" and "찾지 못했어요" in err.value.message


# ---------------------------------------------------------------- E7

def _no_memory(*_args, **_kwargs):
    """What MuPDF raises when malloc fails (an FzErrorSystem, not a MemoryError)."""
    raise pymupdf.mupdf.FzErrorSystem("malloc (74996175 bytes) failed")


def test_e7_mupdf_allocation_failure_while_rendering_is_out_of_memory(session, monkeypatch):
    sid = session.add_file("a.pdf", small_pdf())["sources"][0]["id"]
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", _no_memory)
    for run in (lambda: list(session.export(sid, 2, 2, "jpg")),
                lambda: list(session.export(sid, 2, 2, "pdf", {"rasterize": True})),
                lambda: session.thumbnail(sid, 200)):
        with pytest.raises(SplitError) as err:
            run()
        assert err.value.code == "OUT_OF_MEMORY", err.value.detail


def test_e7_mupdf_allocation_failure_in_the_vector_export_is_out_of_memory(session, monkeypatch):
    sid = session.add_file("a.pdf", small_pdf())["sources"][0]["id"]
    rendered = []
    real = pymupdf.Page.get_pixmap
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda *a, **k: rendered.append(1) or real(*a, **k))
    monkeypatch.setattr(pymupdf.Page, "show_pdf_page", _no_memory)
    with pytest.raises(SplitError) as err:
        list(session.export(sid, 2, 2, "pdf"))
    assert err.value.code == "OUT_OF_MEMORY", err.value.detail
    assert not rendered  # no second attempt with bitmaps in a heap that is already full


def test_e7_mupdf_allocation_failure_in_the_fallback_decoder_is_out_of_memory(monkeypatch):
    data = to_bytes(Image.new("RGB", (120, 80), GREEN), "TIFF", compression="tiff_adobe_deflate")
    assert load_with_mupdf(data, "scan.tif")[0].size == (120, 80)
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", _no_memory)
    with pytest.raises(SplitError) as err:
        load_with_mupdf(data, "scan.tif")
    assert err.value.code == "OUT_OF_MEMORY", err.value.detail


def test_e7_other_mupdf_errors_keep_their_meaning(session, monkeypatch):
    sid = session.add_file("a.pdf", small_pdf())["sources"][0]["id"]

    def broken(*_args, **_kwargs):
        raise pymupdf.mupdf.FzErrorSystem("cannot find object in xref")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", broken)
    with pytest.raises(SplitError) as err:
        list(session.export(sid, 2, 2, "jpg"))
    assert err.value.code == "CORRUPT"


@pytest.mark.parametrize("text,code", [
    ("RangeError: Array buffer allocation failed", "OUT_OF_MEMORY"),
    ("RangeError: Invalid typed array length: 2147483648", "OUT_OF_MEMORY"),
    ("Error: Out of memory", "OUT_OF_MEMORY"),
    ("TypeError: emit is not a function", "INTERNAL"),
])
def test_e7_bridge_reports_a_failed_allocation_outside_python_as_out_of_memory(text, code):
    class JsException(Exception):
        """Stands in for pyodide.ffi.JsException: a JavaScript error raised inside a callback."""

    def emit(_name, _mime, _data):
        raise JsException(text)

    bridge.dispatch("clear")
    try:
        sid = bridge.dispatch("add_file", {"name": "a.png", "data": png(RED)})["sources"][0]["id"]
        answer = bridge.dispatch("export", {"id": sid, "rows": 1, "cols": 2, "format": "png"},
                                 types.SimpleNamespace(emit=emit))
    finally:
        bridge.dispatch("clear")
    assert answer["ok"] is False and answer["error"]["code"] == code


# ---------------------------------------------------------------- E8

def blank_pdf(width_mm: float, height_mm: float) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=width_mm * 72 / 25.4, height=height_mm * 72 / 25.4)
    page.draw_rect(pymupdf.Rect(20, 20, 200, 120), fill=(0.1, 0.4, 0.8), color=None)
    page.insert_text((40, 300), "POSTER", fontsize=120)
    data = doc.tobytes()
    doc.close()
    return data


def test_e8_large_pdf_pages_keep_their_paper_size_where_the_format_allows(session):
    sid = session.add_file("A2.pdf", blank_pdf(420, 594))["sources"][0]["id"]
    small = {"max_pixels": 1_000_000}  # keeps the bitmap formats quick

    page = session.describe(sid, 2, 2, "pdf")["page"]
    assert (page["page_w"], page["page_h"]) == (pytest.approx(420, abs=0.1), pytest.approx(594, abs=0.1))
    (out,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc[0].rect.width == pytest.approx(420 * 72 / 25.4, abs=0.5)
    assert doc[0].rect.height == pytest.approx(594 * 72 / 25.4, abs=0.5)
    assert not doc[0].get_images() and "POSTER" in "".join(pg.get_text() for pg in doc)

    import pptx
    from pptx.util import Emu
    page = session.describe(sid, 2, 2, "pptx", small)["page"]
    assert (page["page_w"], page["page_h"]) == (pytest.approx(420, abs=0.1), pytest.approx(594, abs=0.1))
    (out,) = list(session.export(sid, 2, 2, "pptx", small))
    presentation = pptx.Presentation(io.BytesIO(out.data))
    assert Emu(presentation.slide_width).mm == pytest.approx(420, abs=0.2)
    assert Emu(presentation.slide_height).mm == pytest.approx(594, abs=0.2)

    for fmt in ("docx", "hwpx"):  # Word stops at 22 inches (558 mm): those keep fitting the page into A4
        page = session.describe(sid, 2, 2, fmt, small)["page"]
        assert (round(page["page_w"]), round(page["page_h"])) == (210, 297), fmt
        list(session.export(sid, 2, 2, fmt, small))


def test_e8_limits_per_format(session):
    banner = session.add_file("banner.pdf", blank_pdf(5000, 900))["sources"][0]["id"]
    page = session.describe(banner, 1, 2, "pdf")["page"]
    assert (page["page_w"], page["page_h"]) == (pytest.approx(5000, abs=0.5), pytest.approx(900, abs=0.5))
    page = session.describe(banner, 1, 2, "pptx")["page"]  # PowerPoint stops at 56 inches (1422 mm)
    assert max(page["page_w"], page["page_h"]) <= 1422
    a0 = session.add_file("A0.pdf", blank_pdf(841, 1189))["sources"][0]["id"]
    for fmt, size in (("pdf", (841, 1189)), ("pptx", (841, 1189)), ("docx", (210, 297)), ("hwpx", (210, 297))):
        page = session.describe(a0, 3, 3, fmt)["page"]
        assert (round(page["page_w"]), round(page["page_h"])) == size, fmt
    a3 = session.add_file("A3.pdf", blank_pdf(297, 420))["sources"][0]["id"]
    for fmt in ("pdf", "pptx", "docx", "hwpx"):  # unchanged: fits everywhere
        page = session.describe(a3, 3, 3, fmt)["page"]
        assert (round(page["page_w"]), round(page["page_h"])) == (297, 420), fmt


# ---------------------------------------------------------------- E10

def numbered_pictures(count: int) -> dict:
    return {f"IMG_{i:04d}.png": png((10 + i * 20, 200 - i * 10, 90)) for i in range(count)}


def test_e10_zip_cut_at_the_picture_limit_is_reported_as_truncated(session, monkeypatch):
    monkeypatch.setattr(common_format, "MAX_PICTURES", 5)
    result = session.add_file("album.zip", make_zip(numbered_pictures(8)))
    assert (len(result["sources"]), result["truncated"], result["total"]) == (5, True, 8)
    session.clear()
    whole = session.add_file("album.zip", make_zip(numbered_pictures(5)))
    assert (len(whole["sources"]), whole["truncated"], whole["total"]) == (5, False, 5)


def test_e10_documents_cut_at_the_picture_limit_are_reported_as_truncated(session, monkeypatch):
    monkeypatch.setattr(common_format, "MAX_PICTURES", 5)
    media = {f"word/media/image{i + 1}.png": data for i, data in enumerate(numbered_pictures(8).values())}
    document = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>", **media})
    result = session.add_file("many.docx", document)
    assert (len(result["sources"]), result["truncated"], result["total"]) == (5, True, 8)
    session.clear()

    import pptx
    from pptx.util import Emu
    presentation = pptx.Presentation()
    for data in numbered_pictures(8).values():  # one picture per slide: collected slide by slide
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(data), Emu(914400), Emu(914400))
    buf = io.BytesIO()
    presentation.save(buf)
    result = session.add_file("many.pptx", buf.getvalue())
    assert (len(result["sources"]), result["truncated"], result["total"]) == (5, True, 8)


def test_e10_tiff_cut_at_the_page_limit_is_reported_as_truncated(session, monkeypatch):
    monkeypatch.setattr(imaging, "MAX_FRAMES", 4)
    frames = [Image.new("L", (200, 280), 30 * i) for i in range(7)]
    data = to_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:], compression="tiff_lzw")
    result = session.add_file("fax.tif", data)
    assert (len(result["sources"]), result["truncated"], result["total"]) == (4, True, 7)
    session.clear()
    short = to_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:4], compression="tiff_lzw")
    result = session.add_file("fax.tif", short)
    assert (len(result["sources"]), result["truncated"], result["total"]) == (4, False, 4)

    deflate = to_bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:], compression="tiff_adobe_deflate")
    found = load_with_mupdf(deflate, "fax.tif")  # the browser's way for this kind of TIFF
    assert len(found) == 4 and found[0].origin_total == 7


# ---------------------------------------------------------------- E11

@pytest.mark.parametrize("size", [(360, 30000), (30000, 350)])
def test_e11_long_strip_pages_stay_wide_enough_for_every_format(session, size):
    sid = session.add_file("long.png", to_bytes(Image.new("L", size, 200), "PNG", compress_level=1))["sources"][0]["id"]
    layout = session.all_layouts(sid)["9"][0]
    rows, cols = layout["rows"], layout["cols"]
    assert max(rows, cols) == 9  # nine pieces along the strip
    for fmt in ("pdf", "docx", "pptx", "hwpx"):
        page = session.describe(sid, rows, cols, fmt)["page"]
        assert min(page["page_w"], page["page_h"]) >= 26, (fmt, page)
        assert max(page["page_w"], page["page_h"]) <= 550.001, (fmt, page)
        assert page["w"] <= page["page_w"] + 1e-6 and page["h"] <= page["page_h"] + 1e-6
        assert page["w"] / page["h"] == pytest.approx(layout["out_w"] / layout["out_h"], rel=0.001)  # shape kept
        (out,) = list(session.export(sid, rows, cols, fmt))  # 한글 used to fail here
        if fmt == "pptx":
            xml = zipfile.ZipFile(io.BytesIO(out.data)).read("ppt/presentation.xml").decode("utf-8")
            cx, cy = map(int, re.search(r'<p:sldSz cx="(\d+)" cy="(\d+)"', xml).groups())
            assert 914400 <= min(cx, cy) and max(cx, cy) <= 51206400  # PowerPoint's 1 - 56 inches
            assert min(cx, cy) == pytest.approx(min(page["page_w"], page["page_h"]) * 36000, abs=40)
        if fmt == "hwpx":
            section = zipfile.ZipFile(io.BytesIO(out.data)).read("Contents/section0.xml").decode("utf-8")
            assert section.count("<hp:pic ") == 9


def test_e11_ordinary_and_moderately_long_pictures_keep_their_page(session):
    from splitcore.splitter import make_plan, page_spec
    spec = page_spec(make_plan(6000, 1200, 2, 2), "original")  # 5:1 panorama: nothing to correct
    assert (round(spec.page_w), round(spec.page_h, 1)) == (297, 59.4)
    spec = page_spec(make_plan(1000, 15000, 3, 3), "original")  # 15:1: grown to 30 mm, still in proportion
    assert (round(spec.page_w), round(spec.page_h)) == (30, 450)
    spec = page_spec(make_plan(500, 15000, 3, 3), "original")  # 30:1: the long side stops at the limit
    assert (round(spec.page_w), round(spec.page_h)) == (30, 550)
    assert spec.w / spec.h == pytest.approx(1 / 30, rel=0.001) and spec.h == pytest.approx(550)


# ---------------------------------------------------------------- E12

@pytest.mark.parametrize("folder", ["ppt", "word", "xl", "PPT", "Word"])
def test_e12_zip_with_a_folder_named_like_an_office_part_is_an_archive(session, folder):
    data = make_zip({f"{folder}/photo.png": png(RED), "other/photo2.png": png(GREEN), "readme.txt": b"hi"})
    result = session.add_file("pictures.zip", data)
    assert sorted(m["label"] for m in result["sources"]) == ["photo.png", "photo2.png"]
    assert {m["group"] for m in result["sources"]} == {"압축 파일 속 이미지"}


def test_e12_office_files_are_still_recognised(session):
    types_xml = {"[Content_Types].xml": b"<Types/>"}
    cases = [
        ("a.docx", {"word/document.xml": b"<w:document/>", "word/media/image1.png": png(RED)}),
        ("b.docx", {**types_xml, "word/document2.xml": b"<w:document/>", "word/media/image1.png": png(RED)}),
        ("c.pptx", {"ppt/presentation.xml": b"<p:presentation/>", "ppt/media/image1.png": png(RED)}),
        ("d.xlsx", {"xl/workbook.xml": b"<workbook/>", "xl/media/image1.png": png(RED), "notes/photo.png": png(BLUE)}),
        ("e.xlsb", {**types_xml, "xl/workbook.bin": b"\x00", "xl/media/image1.png": png(RED)}),
    ]
    for name, entries in cases:
        result = session.add_file(name, make_zip(entries))
        assert len(result["sources"]) == 1, name
        assert result["sources"][0]["group"] in ("문서 속 이미지", "슬라이드 속 이미지"), name
    with pytest.raises(SplitError) as err:  # a document without pictures keeps its own advice
        session.add_file("text.docx", make_zip({**types_xml, "word/document.xml": b"<w:document/>"}))
    assert err.value.code == "NO_IMAGE" and "PDF" in err.value.message


# ---------------------------------------------------------------- P11

def test_p11_engine_never_asks_to_upload():
    root = os.path.dirname(os.path.abspath(splitcore.__file__))
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    text = handle.read()
                assert "올려 주세요" not in text, name  # nothing is uploaded: files are picked
                assert "사진이나 그림을 찾지 못했어요" not in text, name


def test_p11_messages_people_see(session):
    with pytest.raises(SplitError) as err:
        session.add_file("text.docx", make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"}))
    assert "문서에서 이미지를 찾지 못했어요" in err.value.message and err.value.message.endswith("그 PDF를 골라 주세요.")

    emf = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 100
    with pytest.raises(SplitError) as err:
        session.add_file("vector.docx", make_zip({"word/document.xml": b"<w:document/>", "word/media/image1.emf": emf}))
    assert "벡터" in err.value.message and "골라 주세요" in err.value.message

    locked = bytearray(make_zip({"a.png": png(RED)}))
    locked[6] |= 1  # "encrypted" flag in the local header ...
    at = bytes(locked).find(b"PK\x01\x02")
    locked[at + 8] |= 1  # ... and in the central directory
    with pytest.raises(SplitError) as err:
        session.add_file("locked.zip", bytes(locked))
    assert err.value.code == "PROTECTED" and err.value.message.endswith("이미지를 직접 골라 주세요.")
