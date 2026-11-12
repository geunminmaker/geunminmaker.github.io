"""Document inputs (PDF, DOCX, PPTX, HWPX, OLE, ZIP) and document outputs (DOCX, PPTX)."""
from __future__ import annotations

import io
import zipfile

import docx
import pptx
import pymupdf
import pytest
from docx.shared import Mm
from PIL import Image
from pptx.util import Emu

from splitcore.errors import SplitError
from splitcore.formats.ole import carve_images
from splitcore.loaders import detect
from splitcore.session import Session


def img_bytes(colour, size=(320, 240), fmt="PNG", **kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, fmt, **kw)
    return buf.getvalue()


def photo_bytes(size=(640, 480), seed=1) -> bytes:
    """Noisy picture (compresses like a photo)."""
    im = Image.effect_noise(size, 50 + seed).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


RED, GREEN, BLUE = (220, 40, 40), (40, 180, 70), (40, 80, 220)


@pytest.fixture()
def session():
    s = Session()
    yield s
    s.clear()


def colours_of(session, result):
    out = []
    for meta in result["sources"]:
        im = session.get(meta["id"]).image().convert("RGB")
        out.append(im.getpixel((im.width // 2, im.height // 2)))
    return out


def near(a, b, tol=10):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ------------------------------------------------------------------ DOCX in

def make_docx(pictures) -> bytes:
    document = docx.Document()
    document.add_paragraph("머리말")
    for data in pictures:
        document.add_picture(io.BytesIO(data), width=Mm(60))
        document.add_paragraph("설명")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_docx_pictures_in_document_order(session):
    data = make_docx([img_bytes(BLUE), img_bytes(RED, fmt="JPEG", quality=95), img_bytes(GREEN)])
    assert detect("보고서.docx", data) == "zip"
    result = session.add_file("보고서.docx", data)
    assert [m["label"] for m in result["sources"]] == ["이미지 1", "이미지 2", "이미지 3"]
    assert all(m["kind"] == "embedded" for m in result["sources"])
    got = colours_of(session, result)
    assert near(got[0], BLUE) and near(got[1], RED) and near(got[2], GREEN)


def test_docx_without_pictures_gives_a_helpful_error(session):
    document = docx.Document()
    document.add_paragraph("글자만 있는 문서")
    buf = io.BytesIO()
    document.save(buf)
    with pytest.raises(SplitError) as err:
        session.add_file("text.docx", buf.getvalue())
    assert err.value.code == "NO_IMAGE" and "PDF" in err.value.message


def test_tiny_decorations_are_ignored_when_real_pictures_exist(session):
    data = make_docx([img_bytes(RED, size=(16, 16)), img_bytes(GREEN, size=(400, 300)), img_bytes(GREEN, size=(400, 300))])
    result = session.add_file("a.docx", data)
    assert len(result["sources"]) == 1  # the icon is dropped, the duplicate is merged
    only_icons = make_docx([img_bytes(RED, size=(16, 16))])
    assert len(session.add_file("b.docx", only_icons)["sources"]) == 1


# ------------------------------------------------------------------ PPTX in

def make_pptx(slides) -> bytes:
    presentation = pptx.Presentation()
    blank = presentation.slide_layouts[6]
    for pictures in slides:
        slide = presentation.slides.add_slide(blank)
        for i, data in enumerate(pictures):
            slide.shapes.add_picture(io.BytesIO(data), Emu(914400 * (i + 1)), Emu(914400))
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


def test_pptx_pictures_are_labelled_by_slide(session):
    data = make_pptx([[img_bytes(RED)], [], [img_bytes(GREEN), img_bytes(BLUE)]])
    result = session.add_file("발표.pptx", data)
    assert [m["label"] for m in result["sources"]] == ["슬라이드 1", "슬라이드 3 · 이미지 1", "슬라이드 3 · 이미지 2"]
    got = colours_of(session, result)
    assert near(got[0], RED) and near(got[1], GREEN) and near(got[2], BLUE)


# ------------------------------------------------------------------ HWPX / ODF / ZIP in

def make_zip(entries: dict, mimetype: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if mimetype is not None:
            zf.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_hwpx_pictures_follow_section_references(session):
    hpf = (b'<opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:manifest>'
           b'<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
           b'<opf:item id="image2" href="BinData/image2.jpg" media-type="image/jpg" isEmbeded="1"/>'
           b'</opf:manifest></opf:package>')
    section = b'<hs:sec><hp:pic><hc:img binaryItemIDRef="image2"/></hp:pic><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hs:sec>'
    data = make_zip({
        "Contents/content.hpf": hpf,
        "Contents/section0.xml": section,
        "BinData/image1.png": img_bytes(RED),
        "BinData/image2.jpg": img_bytes(BLUE, fmt="JPEG", quality=95),
    }, mimetype=b"application/hwp+zip")
    result = session.add_file("가정통신문.hwpx", data)
    got = colours_of(session, result)
    assert len(got) == 2 and near(got[0], BLUE) and near(got[1], RED)


def test_plain_zip_of_pictures_and_odf(session):
    data = make_zip({"b10.png": img_bytes(GREEN), "b9.png": img_bytes(RED), "readme.txt": b"hi",
                     "__MACOSX/._b9.png": b"junk"})
    result = session.add_file("pics.zip", data)
    assert [m["label"] for m in result["sources"]] == ["b9.png", "b10.png"]

    odt = make_zip({"content.xml": b"<x/>", "Pictures/100000.png": img_bytes(BLUE)},
                   mimetype=b"application/vnd.oasis.opendocument.text")
    assert len(session.add_file("a.odt", odt)["sources"]) == 1

    with pytest.raises(SplitError) as err:
        session.add_file("empty.zip", make_zip({"a.txt": b"x"}))
    assert err.value.code == "NO_IMAGE"
    with pytest.raises(SplitError) as err:
        session.add_file("broken.zip", b"PK\x03\x04" + b"\x00" * 100)
    assert err.value.code == "CORRUPT"


def test_metafile_only_document_explains_itself(session):
    emf = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 100
    document = make_docx([img_bytes(RED)])
    # swap the picture for a metafile
    src = zipfile.ZipFile(io.BytesIO(document))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for item in src.infolist():
            payload = src.read(item.filename)
            out.writestr(item, emf if item.filename.startswith("word/media/") else payload)
    with pytest.raises(SplitError) as err:
        session.add_file("vector.docx", buf.getvalue())
    assert err.value.code == "NO_IMAGE" and "벡터" in err.value.message


# ------------------------------------------------------------------ OLE (doc / ppt / hwp)

def test_carving_finds_whole_pictures_and_skips_exif_thumbnails():
    jpeg, png = photo_bytes(), img_bytes(GREEN)
    blob = b"\x00" * 300 + jpeg + b"junk\xff\xd8\xffbroken" + png + b"\x11" * 50
    found = carve_images(blob)
    assert found == [jpeg, png]


def make_ole_like_hwp(pictures, compressed=True, flags_extra=0) -> bytes | None:
    """Build a real OLE2 file with HWP-style streams using pywin32 structured storage."""
    try:
        import pythoncom
        from win32com import storagecon
    except ImportError:
        return None
    import os
    import struct
    import tempfile
    import zlib

    path = os.path.join(tempfile.mkdtemp(), "t.hwp")
    mode = storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE
    root = pythoncom.StgCreateDocfile(path, mode, 0)
    header = bytearray(256)
    header[:17] = b"HWP Document File"
    struct.pack_into("<I", header, 32, 0x05000300)
    struct.pack_into("<I", header, 36, (1 if compressed else 0) | flags_extra)
    stream = root.CreateStream("FileHeader", mode, 0, 0)
    stream.Write(bytes(header))
    stream = None
    storage = root.CreateStorage("BinData", mode, 0, 0)
    for i, (ext, data) in enumerate(pictures, 1):
        if compressed:
            engine = zlib.compressobj(6, zlib.DEFLATED, -15)
            data = engine.compress(data) + engine.flush()
        s = storage.CreateStream(f"BIN{i:04X}.{ext}", mode, 0, 0)
        s.Write(data)
        s = None
    storage.Commit(0)
    storage = None
    root.Commit(0)
    root = None
    with open(path, "rb") as handle:
        return handle.read()


@pytest.mark.parametrize("compressed", [True, False])
def test_hwp5_bindata_extraction(session, compressed):
    data = make_ole_like_hwp([("png", img_bytes(RED)), ("jpg", img_bytes(BLUE, fmt="JPEG", quality=95))], compressed)
    if data is None:
        pytest.skip("needs pywin32 to build an OLE file")
    assert detect("x.hwp", data) == "ole"
    result = session.add_file("안내문.hwp", data)
    got = colours_of(session, result)
    assert len(got) == 2 and near(got[0], RED) and near(got[1], BLUE)


def test_hwp5_password_and_empty(session):
    locked = make_ole_like_hwp([("png", img_bytes(RED))], flags_extra=0x2)
    if locked is None:
        pytest.skip("needs pywin32 to build an OLE file")
    with pytest.raises(SplitError) as err:
        session.add_file("locked.hwp", locked)
    assert err.value.code == "PROTECTED"
    with pytest.raises(SplitError) as err:
        session.add_file("text.hwp", make_ole_like_hwp([]))
    assert err.value.code == "NO_IMAGE"


def test_hwp3_and_unknown_files(session):
    with pytest.raises(SplitError) as err:
        session.add_file("old.hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\x00" * 100)
    assert err.value.code == "UNSUPPORTED" and "3.x" in err.value.message
    with pytest.raises(SplitError) as err:
        session.add_file("notes.txt", "그냥 글자".encode("utf-8") * 20)
    assert err.value.code == "UNSUPPORTED"


# ------------------------------------------------------------------ PDF in

def make_pdf(pages=3, password=None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        colour = [RED, GREEN, BLUE][i % 3]
        page.draw_rect(page.rect, fill=tuple(c / 255 for c in colour), color=None)
        page.insert_text((60, 100), f"Page {i + 1}", fontsize=40, color=(1, 1, 1))
    if password:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw="owner")
    return doc.tobytes()


def test_pdf_pages_are_candidates_with_physical_size(session):
    result = session.add_file("책자.pdf", make_pdf(3))
    metas = result["sources"]
    assert [m["label"] for m in metas] == ["1쪽", "2쪽", "3쪽"]
    assert all(m["kind"] == "page" and m["vector"] for m in metas)
    assert (metas[0]["width"], metas[0]["height"]) == (2479, 3508)  # A4 at 300 dpi
    assert metas[0]["physical_mm"][0] == pytest.approx(209.9, abs=0.2)
    data, mime = session.thumbnail(metas[1]["id"], 200)
    thumb = Image.open(io.BytesIO(data))
    assert max(thumb.size) == 200 and near(thumb.convert("RGB").getpixel((100, 180)), GREEN, 12)
    single = session.add_file("한장.pdf", make_pdf(1))
    assert single["sources"][0]["label"] == "한장.pdf"


def test_pdf_to_pdf_stays_vector_and_other_formats_are_sharp(session):
    sid = session.add_file("a.pdf", make_pdf(1))["sources"][0]["id"]
    (out,) = list(session.export(sid, 2, 2, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc.page_count == 4
    assert doc[0].rect.width == pytest.approx(595, abs=0.5) and doc[0].rect.height == pytest.approx(842, abs=0.5)
    assert not doc[0].get_images()  # no bitmap: still vector
    assert "Page 1" in doc[0].get_text() and "Page 1" not in doc[3].get_text()
    assert len(out.data) < 20_000
    # the title is enlarged twice: its glyphs are ~80 pt tall on the tile page
    spans = [s for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    assert spans and spans[0]["size"] == pytest.approx(80, rel=0.05)

    tiles = list(session.export(sid, 2, 2, "png"))
    assert len(tiles) == 4
    first = Image.open(io.BytesIO(tiles[0].data))
    assert first.size == (2479, 3508)

    forced = list(session.export(sid, 2, 2, "pdf", {"rasterize": True}))[0]
    assert pymupdf.open(stream=forced.data, filetype="pdf")[0].get_images()


def test_pdf_password_flow(session):
    locked = make_pdf(2, password="비밀1234")
    with pytest.raises(SplitError) as err:
        session.add_file("locked.pdf", locked)
    assert err.value.code == "ENCRYPTED"
    with pytest.raises(SplitError) as err:
        session.add_file("locked.pdf", locked, "wrong")
    assert err.value.code == "WRONG_PASSWORD"
    assert len(session.add_file("locked.pdf", locked, "비밀1234")["sources"]) == 2


def test_broken_pdf(session):
    with pytest.raises(SplitError) as err:
        session.add_file("bad.pdf", b"%PDF-1.7\n" + b"\x00" * 400)
    assert err.value.code == "CORRUPT"


# ------------------------------------------------------------------ DOCX / PPTX out

@pytest.mark.parametrize("rows,cols,paper", [(3, 3, "original"), (2, 3, "original"), (3, 2, "a4")])
def test_docx_export_structure(session, rows, cols, paper):
    sid = session.add_file("사진.jpg", photo_bytes((1200, 900)))["sources"][0]["id"]
    (out,) = list(session.export(sid, rows, cols, "docx", {"paper": paper}))
    assert out.name.endswith(".docx")
    page = session.describe(sid, rows, cols, "docx", {"paper": paper})["page"]
    document = docx.Document(io.BytesIO(out.data))
    section = document.sections[0]
    assert section.page_width.mm == pytest.approx(page["page_w"], abs=0.1)
    assert section.page_height.mm == pytest.approx(page["page_h"], abs=0.1)
    assert len(document.paragraphs) == rows * cols
    xml = zipfile.ZipFile(io.BytesIO(out.data)).read("word/document.xml").decode("utf-8")
    assert xml.count("<wp:anchor ") == rows * cols
    assert xml.count("<w:pageBreakBefore/>") == rows * cols - 1
    import re
    extents = set(re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml))
    assert len(extents) == 1
    cx, cy = map(int, extents.pop())
    assert cx == pytest.approx(page["w"] * 36000, abs=40) and cy == pytest.approx(page["h"] * 36000, abs=40)
    media = [n for n in zipfile.ZipFile(io.BytesIO(out.data)).namelist() if n.startswith("word/media/")]
    assert len(media) == rows * cols and all(n.endswith(".jpg") for n in media)
    plan = session.describe(sid, rows, cols)["plan"]
    first = Image.open(io.BytesIO(zipfile.ZipFile(io.BytesIO(out.data)).read("word/media/image1.jpg")))
    assert first.size == (plan["out_w"], plan["out_h"])


@pytest.mark.parametrize("rows,cols,paper", [(3, 3, "original"), (2, 3, "original"), (3, 2, "a4")])
def test_pptx_export_structure(session, rows, cols, paper):
    sid = session.add_file("사진.jpg", photo_bytes((1200, 900)))["sources"][0]["id"]
    (out,) = list(session.export(sid, rows, cols, "pptx", {"paper": paper}))
    page = session.describe(sid, rows, cols, "pptx", {"paper": paper})["page"]
    presentation = pptx.Presentation(io.BytesIO(out.data))
    assert Emu(presentation.slide_width).mm == pytest.approx(page["page_w"], abs=0.1)
    assert Emu(presentation.slide_height).mm == pytest.approx(page["page_h"], abs=0.1)
    assert len(presentation.slides) == rows * cols
    for slide in presentation.slides:
        (shape,) = list(slide.shapes)
        assert shape.shape_type == 13  # picture
        assert Emu(shape.left).mm == pytest.approx(page["x"], abs=0.1)
        assert Emu(shape.top).mm == pytest.approx(page["y"], abs=0.1)
        assert Emu(shape.width).mm == pytest.approx(page["w"], abs=0.1)
        assert Emu(shape.height).mm == pytest.approx(page["h"], abs=0.1)
        assert slide.slide_layout.name == "Blank"


def test_documents_use_png_for_transparency_and_flat_art(session):
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    im.paste((10, 200, 90, 255), (0, 0, 150, 150))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    sid = session.add_file("logo.png", buf.getvalue())["sources"][0]["id"]
    for fmt, folder in (("docx", "word/media/"), ("pptx", "ppt/media/")):
        (out,) = list(session.export(sid, 2, 2, fmt))
        names = [n for n in zipfile.ZipFile(io.BytesIO(out.data)).namelist() if n.startswith(folder)]
        assert len(names) == 4 and all(n.endswith(".png") for n in names)
        types = zipfile.ZipFile(io.BytesIO(out.data)).read("[Content_Types].xml").decode()
        assert 'Extension="png"' in types and 'Extension="jpg"' not in types


# ------------------------------------------------------------------ real Hangul documents

import os

HANGUL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hangul")


@pytest.mark.parametrize("name,sizes", [
    ("hwplib_그림.hwp", [(138, 110)]),
    ("hwplib_이미지추가.hwp", [(138, 110), (210, 78)]),
    ("hwplib_changing-image.hwp", [(360, 188), (1023, 866)]),          # document order, not storage order
    ("hwpjs_noori.hwp", [(1200, 153), (115, 720), (92, 410)]),          # a 4th, tiny decoration is dropped
    ("hwpxlib_SimplePicture.hwpx", [(544, 184)]),
    ("hwpxlib_SimpleContainer.hwpx", [(360, 188), (544, 184)]),
    ("pythonhwpx_two_pictures.hwpx", [(40, 20), (40, 20)]),             # only small pictures: still offered
])
def test_real_hangul_documents(session, name, sizes):
    with open(os.path.join(HANGUL, name), "rb") as handle:
        result = session.add_file(name, handle.read())
    metas = result["sources"]
    assert [(m["width"], m["height"]) for m in metas] == sizes
    assert [m["label"] for m in metas] == [f"이미지 {i + 1}" for i in range(len(sizes))]
    for meta in metas:
        session.thumbnail(meta["id"], 96)  # decodes


@pytest.mark.parametrize("name,code", [
    ("hwplib_distribution.hwp", "PROTECTED"),
    ("hwplib_blank.hwp", "NO_IMAGE"),
    ("hwpxlib_blank.hwpx", "NO_IMAGE"),
])
def test_real_hangul_documents_without_usable_pictures(session, name, code):
    with open(os.path.join(HANGUL, name), "rb") as handle:
        data = handle.read()
    with pytest.raises(SplitError) as err:
        session.add_file(name, data)
    assert err.value.code == code


def test_hwpx_export_round_trips_through_the_reader(session):
    sid = session.add_file("사진.jpg", photo_bytes((1200, 900)))["sources"][0]["id"]
    (out,) = list(session.export(sid, 3, 3, "hwpx", {"paper": "a4"}))
    assert out.name == "사진_9분할_3x3.hwpx" and out.mime == "application/hwp+zip"
    archive = zipfile.ZipFile(io.BytesIO(out.data))
    assert archive.namelist()[0] == "mimetype" and archive.read("mimetype") == b"application/hwp+zip"
    section = archive.read("Contents/section0.xml").decode("utf-8")
    assert section.count("<hp:pic ") == 9 and section.count('pageBreak="1"') == 8
    back = Session()
    pictures = back.add_file("결과.hwpx", out.data)["sources"]
    assert len(pictures) == 9 and (pictures[0]["width"], pictures[0]["height"]) == (1200, 900)
    back.clear()
