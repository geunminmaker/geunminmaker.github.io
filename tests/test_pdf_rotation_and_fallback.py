"""Rotated / cropped PDF pages and the MuPDF fallback decoder."""
from __future__ import annotations

import builtins
import io

import pymupdf
import pytest
from PIL import Image

from splitcore import loaders
from splitcore import sources as sources_module
from splitcore.errors import SplitError
from splitcore.formats.mupdf_image import load_with_mupdf
from splitcore.session import Session

RED, GREEN, BLUE, YELLOW = (220, 40, 40), (40, 180, 70), (40, 80, 220), (240, 200, 20)


def near(a, b, tol=6):
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


@pytest.fixture()
def session():
    s = Session()
    yield s
    s.clear()


def make_rotated_cropped_pdf() -> bytes:
    """Four pages: 0/90/180/270 degrees, each with a crop box that hides a magenta border."""
    doc = pymupdf.open()
    for rotation in (0, 90, 180, 270):
        page = doc.new_page(width=600, height=400)
        page.draw_rect(page.rect, fill=(1, 0, 1), color=None)  # must never be visible
        inner = pymupdf.Rect(50, 40, 550, 360)
        page.draw_rect(inner, fill=(1, 1, 1), color=None)
        for r in range(2):
            for c in range(2):
                cell = pymupdf.Rect(inner.x0 + c * 250, inner.y0 + r * 160,
                                    inner.x0 + (c + 1) * 250, inner.y0 + (r + 1) * 160)
                colour = [RED, GREEN, BLUE, YELLOW][r * 2 + c]
                page.draw_rect(cell, fill=tuple(v / 255 for v in colour), color=None)
        page.set_cropbox(inner)
        if rotation:
            page.set_rotation(rotation)
    return doc.tobytes()


QUARTERS = [(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]


@pytest.mark.parametrize("index,rotation", [(0, 0), (1, 90), (2, 180), (3, 270)])
def test_rotated_and_cropped_pdf_pages(session, index, rotation):
    data = make_rotated_cropped_pdf()
    truth = pymupdf.open(stream=data, filetype="pdf")[index].get_pixmap(dpi=36, alpha=False)
    truth_image = Image.frombytes("RGB", (truth.width, truth.height), truth.samples)
    meta = session.add_file("rot.pdf", data)["sources"][index]
    expected = (500, 320) if rotation in (0, 180) else (320, 500)
    shown = (round(meta["physical_mm"][0] * 72 / 25.4), round(meta["physical_mm"][1] * 72 / 25.4))
    assert shown == expected

    # the reference bitmap equals what a viewer shows: right orientation, nothing outside the crop box
    reference = session.get(meta["id"]).image().resize(truth_image.size, Image.BOX)
    for fx, fy in QUARTERS:
        point = (int(truth.width * fx), int(truth.height * fy))
        assert near(reference.getpixel(point), truth_image.getpixel(point)), (rotation, point)
    assert not near(reference.getpixel((1, 1)), (255, 0, 255), 40)

    # vector tiles: a 2x2 split gives one flat colour per page, in reading order of the DISPLAYED page
    (out,) = list(session.export(meta["id"], 2, 2, "pdf"))
    pages = pymupdf.open(stream=out.data, filetype="pdf")
    assert pages.page_count == 4
    for tile, (fx, fy) in enumerate(QUARTERS):
        want = truth_image.getpixel((int(truth.width * fx), int(truth.height * fy)))
        pix = pages[tile].get_pixmap(dpi=20, alpha=False)
        for probe in [(pix.width // 2, pix.height // 2), (3, 3), (pix.width - 4, pix.height - 4)]:
            assert near(pix.pixel(*probe), want), (rotation, tile, probe, pix.pixel(*probe), want)

    # raster tiles agree with the vector ones
    tiles = list(session.export(meta["id"], 2, 2, "png"))
    for tile, (fx, fy) in enumerate(QUARTERS):
        want = truth_image.getpixel((int(truth.width * fx), int(truth.height * fy)))
        image = Image.open(io.BytesIO(tiles[tile].data)).convert("RGB")
        assert near(image.getpixel((image.width // 2, image.height // 2)), want), (rotation, tile)


def _tiff(frames, **kw) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=len(frames) > 1, append_images=frames[1:], **kw)
    return buf.getvalue()


def test_mupdf_fallback_decoder_for_tiff_scans():
    frames = [Image.new("RGB", (300, 200), c) for c in (RED, GREEN, BLUE)]
    for frame, colour in zip(frames, (BLUE, RED, GREEN)):
        frame.paste(colour, (0, 0, 150, 100))  # the top-left quarter differs: orientation check
    found = load_with_mupdf(_tiff(frames, compression="tiff_deflate", dpi=(300, 300)), "scan.tif")
    assert [s.size for s in found] == [(300, 200)] * 3
    assert [s.label for s in found] == ["1번째 장", "2번째 장", "3번째 장"]
    for source, frame in zip(found, frames):
        image = source.image()
        assert near(image.getpixel((10, 10)), frame.getpixel((10, 10)), 3)
        assert near(image.getpixel((290, 190)), frame.getpixel((290, 190)), 3)
    (one,) = load_with_mupdf(_tiff(frames[:1], compression="jpeg"), "one.tif")
    assert one.label == "one.tif" and one.size == (300, 200)


def test_picture_that_needs_the_fallback(monkeypatch):
    """In the browser Pillow cannot decode deflate TIFF: the loader asks for PyMuPDF, then succeeds."""
    data = _tiff([Image.new("RGB", (120, 80), GREEN)], compression="tiff_deflate")
    calls = {"n": 0}

    def verify_fails_for_tiff(self):
        calls["n"] += 1
        if self.format == "TIFF":
            raise SplitError("CORRUPT", detail="OSError: decoder error -2")

    monkeypatch.setattr(sources_module.RasterSource, "verify", verify_fails_for_tiff)

    real_import = builtins.__import__

    def without_pymupdf(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("not loaded yet")
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", without_pymupdf)
        with pytest.raises(SplitError) as err:
            loaders.load_file("scan.tif", data)
        assert err.value.code == "NEED_PACKAGE" and err.value.detail == "pymupdf"

    (source,) = loaders.load_file("scan.tif", data)  # package available: converted and usable
    assert source.size == (120, 80) and near(source.image().getpixel((5, 5)), GREEN, 3)

    with pytest.raises(SplitError) as err:  # a really damaged TIFF still reports the real problem
        loaders.load_file("bad.tif", data[:60])
    assert err.value.code in ("CORRUPT", "UNSUPPORTED")
