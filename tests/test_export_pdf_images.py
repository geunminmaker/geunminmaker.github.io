"""Image and PDF exports, verified by reading the results back with other libraries."""
from __future__ import annotations

import io

import pymupdf
import pytest
from PIL import Image, ImageChops, ImageDraw

from splitcore.session import Session


def poster(w: int, h: int, rows: int, cols: int) -> Image.Image:
    """A picture whose every grid cell has its own flat colour and number."""
    im = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(im)
    for r in range(rows):
        for c in range(cols):
            i = r * cols + c
            colour = ((i * 53) % 200 + 30, (i * 97) % 200 + 30, (i * 151) % 200 + 30)
            box = (c * w // cols, r * h // rows, (c + 1) * w // cols - 1, (r + 1) * h // rows - 1)
            draw.rectangle(box, fill=colour)
            draw.text((box[0] + 8, box[1] + 8), str(i + 1), fill="white")
    return im


def cell_colour(i: int):
    return ((i * 53) % 200 + 30, (i * 97) % 200 + 30, (i * 151) % 200 + 30)


def to_bytes(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def close(a, b, tol=12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


@pytest.fixture()
def session():
    s = Session()
    yield s
    s.clear()


@pytest.mark.parametrize("fmt,ext,pil", [("jpg", "jpg", "JPEG"), ("png", "png", "PNG"), ("jpeg", "jpg", "JPEG")])
@pytest.mark.parametrize("rows,cols,size", [
    (3, 3, (600, 400)), (4, 4, (600, 400)),  # square grids: the original size
    (2, 3, (400, 400)),                      # 200 x 200 cells, twice
    (1, 2, (400, 533)),                      # portrait halves grow into the turned 400 x 600 box
])
def test_image_export(session, fmt, ext, pil, rows, cols, size):
    w, h = 600, 400
    sid = session.add_file("포스터 사진.png", to_bytes(poster(w, h, rows, cols), "PNG"))["sources"][0]["id"]
    seen = []
    files = list(session.export(sid, rows, cols, fmt, progress=lambda d, t: seen.append((d, t))))
    n = rows * cols
    assert len(files) == n and seen[-1] == (n, n)
    for i, f in enumerate(files):
        assert f.name == f"포스터 사진_{n}분할_{cols}x{rows}_{i + 1:02d}.{ext}"
        im = Image.open(io.BytesIO(f.data))
        assert im.format == pil
        assert im.size == size
        cx, cy = im.size[0] // 2, im.size[1] * 3 // 4
        assert close(im.convert("RGB").getpixel((cx, cy)), cell_colour(i))


@pytest.mark.parametrize("rows,cols,paper", [(3, 3, "original"), (2, 3, "original"), (3, 2, "a4"), (1, 2, "a4")])
def test_pdf_export_pages_match_tiles(session, rows, cols, paper):
    w, h = 900, 600
    sid = session.add_file("poster.jpg", to_bytes(poster(w, h, rows, cols), "JPEG", quality=95))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, rows, cols, "pdf", {"paper": paper}))
    assert pdf.name == f"poster_{rows * cols}분할_{cols}x{rows}.pdf" and pdf.mime == "application/pdf"
    info = session.describe(sid, rows, cols, "pdf", {"paper": paper})
    page = info["page"]
    doc = pymupdf.open(stream=pdf.data, filetype="pdf")
    assert doc.page_count == rows * cols
    assert doc.metadata["title"] == f"poster_{rows * cols}분할_{cols}x{rows}"
    for i, pg in enumerate(doc):
        assert pg.rect.width == pytest.approx(page["page_w"] * 72 / 25.4, abs=0.05)
        assert pg.rect.height == pytest.approx(page["page_h"] * 72 / 25.4, abs=0.05)
        images = pg.get_images(full=True)
        assert len(images) == 1
        assert (images[0][2], images[0][3]) == (info["plan"]["out_w"], info["plan"]["out_h"])
        bbox = pg.get_image_rects(images[0][0])[0]
        assert bbox.x0 == pytest.approx(page["x"] * 72 / 25.4, abs=0.05)
        assert bbox.y0 == pytest.approx(page["y"] * 72 / 25.4, abs=0.05)
        assert bbox.width == pytest.approx(page["w"] * 72 / 25.4, abs=0.05)
        assert bbox.height == pytest.approx(page["h"] * 72 / 25.4, abs=0.05)
        pix = pg.get_pixmap(dpi=40)
        centre = pix.pixel(pix.width // 2, pix.height * 5 // 8)
        assert close(centre, cell_colour(i), tol=14)
    if paper == "a4":
        assert sorted((round(doc[0].rect.width), round(doc[0].rect.height))) == [595, 842]
    # a strict second opinion on the file structure
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf.data), strict=True)
    assert len(reader.pages) == rows * cols


def test_pdf_transparent_and_graphics_are_lossless(session):
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((20, 20, 280, 280), fill=(220, 30, 30, 255))
    sid = session.add_file("logo.png", to_bytes(im, "PNG"))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=pdf.data, filetype="pdf")
    assert doc.page_count == 4
    xref, smask = doc[0].get_images(full=True)[0][:2]
    assert smask > 0  # transparency kept as a soft mask
    pix = doc[0].get_pixmap(dpi=72, alpha=False)
    assert close(pix.pixel(2, 2), (255, 255, 255), tol=2)  # transparent corner shows white paper
    assert close(pix.pixel(pix.width - 3, pix.height - 3), (220, 30, 30), tol=3)  # lossless red

    flat = Image.new("RGB", (400, 300), (10, 120, 200))
    ImageDraw.Draw(flat).rectangle((50, 50, 350, 250), fill=(250, 250, 250))
    sid = session.add_file("chart.png", to_bytes(flat, "PNG"))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=pdf.data, filetype="pdf")
    assert doc.extract_image(doc[0].get_images()[0][0])["ext"] == "png"  # stored without JPEG loss
    assert doc[0].get_pixmap(dpi=72).pixel(3, 3) == (10, 120, 200)

    photo = Image.effect_noise((400, 300), 60).convert("RGB")
    sid = session.add_file("photo.png", to_bytes(photo, "PNG"))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=pdf.data, filetype="pdf")
    assert doc.extract_image(doc[0].get_images()[0][0])["ext"] == "jpeg"  # photo-like -> JPEG


def test_pdf_keeps_icc_profile_and_grayscale(session):
    from PIL import ImageCms
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    sid = session.add_file("p3.jpg", to_bytes(poster(400, 300, 2, 2), "JPEG", icc_profile=profile))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, 2, 2, "pdf"))
    assert b"/ICCBased" in pdf.data
    doc = pymupdf.open(stream=pdf.data, filetype="pdf")
    assert close(doc[3].get_pixmap(dpi=30).pixel(10, 20), cell_colour(3), tol=14)

    gray = Image.linear_gradient("L").resize((320, 240))
    sid = session.add_file("gray.jpg", to_bytes(gray, "JPEG"))["sources"][0]["id"]
    (pdf,) = list(session.export(sid, 2, 2, "pdf"))
    assert b"/DeviceGray" in pdf.data
    assert pymupdf.open(stream=pdf.data, filetype="pdf").page_count == 4


def test_describe_and_layout_listing(session):
    sid = session.add_file("a.png", to_bytes(poster(800, 600, 1, 1), "PNG"))["sources"][0]["id"]
    layouts = session.all_layouts(sid)
    assert set(layouts) == {"2", "4", "6", "8", "9", "12", "16", "18"}
    assert layouts["6"][0]["key"] == "3x2" and layouts["6"][0]["recommended"]
    info = session.describe(sid, 3, 3, "png")
    assert info["plan"]["fills_original"] and info["kind"] == "images" and "page" not in info
    data, mime = session.preview(sid, 200)
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == 200
