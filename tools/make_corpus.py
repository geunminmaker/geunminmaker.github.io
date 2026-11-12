"""Build the test corpus: pictures and documents of every kind the app must handle.

    .venv/Scripts/python tools/make_corpus.py [--office] [out_dir]

``--office`` additionally authors files with the real Microsoft Word and
PowerPoint (Windows only) so the corpus contains genuine Office output,
including the legacy .doc / .ppt formats.

Every picture is a smooth, non-repeating design (so a misplaced, mirrored or
rescaled tile is detectable numerically) with large numbered cells (so a human
can check a print-out at a glance).
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _font(size: int):
    for name in ("malgunbd.ttf", "malgun.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def scene(width: int, height: int, seed: int = 0, label: str = "", photo: bool = True) -> Image.Image:
    """A picture with a sky-like gradient, soft blobs, a labelled 6x6 reference grid and a frame."""
    rng = random.Random(seed)
    small_w, small_h = max(8, width // 8), max(8, height // 8)
    base = Image.new("RGB", (small_w, small_h))
    px = base.load()
    c1 = [rng.randint(20, 235) for _ in range(3)]
    c2 = [rng.randint(20, 235) for _ in range(3)]
    c3 = [rng.randint(20, 235) for _ in range(3)]
    for y in range(small_h):
        for x in range(small_w):
            u, v = x / (small_w - 1), y / (small_h - 1)
            px[x, y] = tuple(int(c1[i] * (1 - u) * (1 - v) + c2[i] * u * (1 - v) + c3[i] * v) for i in range(3))
    im = base.resize((width, height), Image.BICUBIC)
    draw = ImageDraw.Draw(im, "RGBA")
    for _ in range(14):
        r = rng.randint(min(width, height) // 14, min(width, height) // 4)
        cx, cy = rng.randint(0, width), rng.randint(0, height)
        colour = tuple(rng.randint(0, 255) for _ in range(3)) + (rng.randint(60, 150),)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)
    if photo:
        im = im.filter(ImageFilter.GaussianBlur(max(1, min(width, height) // 300)))
        noise = Image.effect_noise((width, height), 14).convert("RGB")
        im = Image.blend(im, noise, 0.06)
        draw = ImageDraw.Draw(im, "RGBA")
    # reference grid with cell numbers
    cells = 6
    font = _font(max(10, min(width, height) // 22))
    line = max(1, min(width, height) // 500)
    for i in range(1, cells):
        draw.line((width * i // cells, 0, width * i // cells, height), fill=(255, 255, 255, 110), width=line)
        draw.line((0, height * i // cells, width, height * i // cells), fill=(255, 255, 255, 110), width=line)
    for r in range(cells):
        for c in range(cells):
            text = f"{chr(65 + r)}{c + 1}"
            x, y = width * c // cells + width // (cells * 8), height * r // cells + height // (cells * 10)
            draw.text((x + line, y + line), text, font=font, fill=(0, 0, 0, 150))
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 235))
    frame = max(2, min(width, height) // 150)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(20, 20, 20, 255), width=frame)
    if label:
        big = _font(max(12, min(width, height) // 12))
        box = draw.textbbox((0, 0), label, font=big)
        tx, ty = (width - (box[2] - box[0])) // 2, (height - (box[3] - box[1])) // 2
        draw.text((tx + 2, ty + 2), label, font=big, fill=(0, 0, 0, 160))
        draw.text((tx, ty), label, font=big, fill=(255, 255, 255, 255))
    return im


def flat_ui(width: int, height: int, seed: int = 0) -> Image.Image:
    """Screenshot-like flat graphics with text."""
    rng = random.Random(seed)
    im = Image.new("RGB", (width, height), (247, 248, 250))
    draw = ImageDraw.Draw(im)
    font = _font(max(12, width // 28))
    draw.rectangle((0, 0, width, height // 12), fill=(32, 96, 220))
    draw.text((width // 20, height // 40), "알림장 · 2026년 11월", font=font, fill="white")
    y = height // 10
    row = 0
    while y < height - height // 14:
        h = rng.randint(height // 16, height // 9)
        draw.rounded_rectangle((width // 20, y, width - width // 20, y + h), radius=width // 40,
                               fill="white", outline=(222, 226, 232), width=2)
        draw.text((width // 12, y + h // 4), f"{row + 1}. 준비물과 일정 안내 문장입니다", font=font, fill=(30, 34, 40))
        y += h + height // 60
        row += 1
    return im


def save(im: Image.Image, path: str, fmt: str | None = None, **kw) -> str:
    im.save(path, fmt, **kw)
    return path


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def giant_png(width: int, height: int) -> bytes:
    """A valid PNG header announcing a huge picture (tests the size guard without the pixels)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


def build(out: str, office: bool) -> dict:
    os.makedirs(out, exist_ok=True)
    manifest: dict[str, dict] = {}

    def note(name: str, **info):
        manifest[name] = info
        return os.path.join(out, name)

    # ---------------- pictures
    save(scene(4032, 3024, 1, "12MP 가로 사진"), note("01_사진_가로_4032x3024.jpg", kind="image", size=[4032, 3024]),
         "JPEG", quality=90, dpi=(72, 72))
    # stored landscape, shown portrait through EXIF orientation 6
    portrait = scene(3024, 4032, 2, "세로 사진 (EXIF 회전)")
    exif = Image.Exif()
    exif[0x0112] = 6
    save(portrait.transpose(Image.ROTATE_90), note("02_사진_세로_EXIF회전.jpg", kind="image", size=[3024, 4032]),
         "JPEG", quality=90, exif=exif)
    save(scene(2000, 2000, 3, "정사각형", photo=False), note("03_정사각형_2000.png", kind="image", size=[2000, 2000]), "PNG")
    logo = Image.new("RGBA", (1200, 800), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
    d.ellipse((60, 60, 1140, 740), fill=(230, 60, 60, 255))
    d.rectangle((420, 250, 780, 550), fill=(255, 255, 255, 255))
    d.polygon([(600, 120), (700, 240), (500, 240)], fill=(40, 60, 200, 255))
    save(logo, note("04_투명배경_로고_1200x800.png", kind="image", size=[1200, 800], alpha=True), "PNG")
    save(flat_ui(1170, 2532, 5), note("05_휴대폰_캡처_1170x2532.png", kind="image", size=[1170, 2532], graphics=True), "PNG")
    save(scene(6000, 1200, 6, "파노라마"), note("06_파노라마_6000x1200.jpg", kind="image", size=[6000, 1200]), "JPEG", quality=88)
    frames = [scene(90, 60, 70 + i, photo=False).convert("P", palette=Image.ADAPTIVE) for i in range(3)]
    frames[0].save(note("07_작은_움직이는_90x60.gif", kind="image", size=[90, 60]), "GIF", save_all=True,
                   append_images=frames[1:], duration=200, loop=0)
    save(scene(1800, 1200, 8, "CMYK 인쇄용").convert("CMYK"), note("08_CMYK_1800x1200.jpg", kind="image", size=[1800, 1200]),
         "JPEG", quality=92)
    gray = Image.linear_gradient("L").resize((1000, 1000)).convert("I").point(lambda v: v * 257).convert("I;16")
    save(gray, note("09_16비트_흑백_1000.png", kind="image", size=[1000, 1000]), "PNG")
    pages = [scene(1240, 1754, 100 + i, f"스캔 {i + 1}쪽", photo=False) for i in range(3)]
    pages[0].save(note("10_여러장_스캔.tiff", kind="image", count=3, size=[1240, 1754]), "TIFF", save_all=True,
                  append_images=pages[1:], compression="tiff_deflate")
    save(scene(1600, 900, 11, "WebP"), note("11_웹피_1600x900.webp", kind="image", size=[1600, 900]), "WEBP", quality=90)
    save(scene(800, 600, 12, "BMP", photo=False), note("12_비트맵_800x600.bmp", kind="image", size=[800, 600]), "BMP")
    # 24 MP phone photo with a Display-P3-like embedded profile and 300 dpi
    try:
        from PIL import ImageCms
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        profile = None
    save(scene(5712, 4284, 13, "24MP 사진"), note("13_사진_24MP_5712x4284.jpg", kind="image", size=[5712, 4284]),
         "JPEG", quality=88, dpi=(300, 300), **({"icc_profile": profile} if profile else {}))

    # ---------------- PDF
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.draw_rect(page.rect, fill=(0.97, 0.95, 0.90), color=None)
    font = pymupdf.Font("helv")
    for r in range(6):
        for c in range(4):
            rect = pymupdf.Rect(40 + c * 130, 60 + r * 125, 40 + c * 130 + 110, 60 + r * 125 + 100)
            page.draw_rect(rect, color=(0.1, 0.2, 0.5), fill=((r * 40 + 30) / 255, (c * 60 + 20) / 255, 0.6), width=1.2)
            page.insert_text((rect.x0 + 8, rect.y0 + 28), f"{chr(65 + r)}{c + 1}", fontsize=22, color=(1, 1, 1))
    page.insert_text((40, 40), "Vector poster - text must stay sharp", fontsize=20, color=(0.1, 0.1, 0.1))
    page.draw_circle((297, 420), 180, color=(0.8, 0.1, 0.1), width=6)
    doc.save(note("20_벡터_포스터_A4.pdf", kind="pdf", pages=1, vector=True))
    doc.close()

    doc = pymupdf.open()
    for i, (w, h, rot) in enumerate([(595.28, 841.89, 0), (841.89, 595.28, 0), (595.28, 841.89, 90), (419.53, 595.28, 0), (1190.55, 841.89, 0)]):
        page = doc.new_page(width=w, height=h)
        page.draw_rect(page.rect, fill=((40 + i * 40) / 255, 0.55, (220 - i * 35) / 255), color=None)
        page.insert_text((50, 90), f"Page {i + 1}", fontsize=48, color=(1, 1, 1))
        page.draw_line((0, 0), (w, h), color=(1, 1, 1), width=3)
        if rot:
            page.set_rotation(rot)
    doc.save(note("21_여러쪽_5쪽_크기제각각.pdf", kind="pdf", pages=5))
    doc.close()

    buf = io.BytesIO()
    scene(1654, 2339, 22, "스캔한 문서").save(buf, "JPEG", quality=85)
    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(note("22_스캔_PDF.pdf", kind="pdf", pages=1))
    doc.save(note("23_암호_1234.pdf", kind="pdf", pages=1, password="1234"),
             encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="owner-9")
    doc.close()

    # ---------------- Office (library-made)
    def jpeg_bytes(im, quality=90):
        b = io.BytesIO()
        im.save(b, "JPEG", quality=quality)
        return b.getvalue()

    def png_bytes(im):
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()

    import docx
    from docx.shared import Mm
    document = docx.Document()
    document.add_heading("가정통신문", 0)
    document.add_paragraph("아래 그림을 인쇄해 교실에 붙여 주세요.")
    document.add_picture(io.BytesIO(jpeg_bytes(scene(2400, 1600, 30, "워드 속 그림 1"))), width=Mm(150))
    document.add_paragraph("두 번째 그림")
    document.add_picture(io.BytesIO(png_bytes(scene(1200, 1700, 31, "워드 속 그림 2", photo=False))), width=Mm(90))
    document.save(note("30_워드_그림2장.docx", kind="docx", pictures=2))

    import pptx
    from pptx.util import Emu
    presentation = pptx.Presentation()
    blank = presentation.slide_layouts[6]
    slide = presentation.slides.add_slide(blank)
    slide.shapes.add_picture(io.BytesIO(jpeg_bytes(scene(1920, 1080, 32, "슬라이드 1 그림"))), Emu(0), Emu(0),
                             width=presentation.slide_width)
    slide = presentation.slides.add_slide(blank)
    slide.shapes.add_picture(io.BytesIO(png_bytes(scene(1000, 1000, 33, "슬라이드 2-1", photo=False))), Emu(300000), Emu(300000), width=Emu(3000000))
    slide.shapes.add_picture(io.BytesIO(jpeg_bytes(scene(1500, 1000, 34, "슬라이드 2-2"))), Emu(3600000), Emu(300000), width=Emu(4500000))
    presentation.save(note("31_파워포인트_그림3장.pptx", kind="pptx", pictures=3))

    document = docx.Document()
    document.add_paragraph("이 문서에는 글자만 있습니다.")
    document.save(note("32_글자만_있는_워드.docx", kind="docx", pictures=0, expect_error="NO_IMAGE"))

    # ---------------- archives
    with zipfile.ZipFile(note("40_사진묶음.zip", kind="zip", pictures=3), "w") as zf:
        zf.writestr("여행/사진1.jpg", jpeg_bytes(scene(1600, 1200, 40, "ZIP 1")))
        zf.writestr("여행/사진2.jpg", jpeg_bytes(scene(1200, 1600, 41, "ZIP 2")))
        zf.writestr("여행/사진10.png", png_bytes(scene(800, 800, 42, "ZIP 3", photo=False)))
        zf.writestr("읽어주세요.txt", "설명 파일".encode("utf-8"))

    # ---------------- broken / unusual inputs
    good = jpeg_bytes(scene(1600, 1200, 50, "손상"))
    with open(note("50_잘린_사진.jpg", kind="image", expect_error="CORRUPT"), "wb") as f:
        f.write(good[: len(good) // 4])
    with open(note("51_이름만_사진.png", kind="unknown", expect_error="UNSUPPORTED"), "wb") as f:
        f.write("이건 그림이 아니라 글자 파일입니다.\n".encode("utf-8") * 40)
    open(note("52_빈파일.pdf", kind="unknown", expect_error="EMPTY_FILE"), "wb").close()
    with open(note("53_너무큰_해상도.png", kind="image", expect_error="IMAGE_TOO_LARGE"), "wb") as f:
        f.write(giant_png(20000, 20000))
    # a JPEG that claims to be a PDF by name: content decides
    with open(note("54_확장자가_틀린_사진.pdf", kind="image", size=[1500, 1000]), "wb") as f:
        f.write(jpeg_bytes(scene(1500, 1000, 54, "이름은 PDF")))
    save(scene(40, 30, 55, photo=False), note("55_아주작은_40x30.png", kind="image", size=[40, 30]), "PNG")

    if office:
        try:
            _office_files(out, note, jpeg_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"! Office files skipped: {exc}")

    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
    return manifest


def _office_files(out: str, note, jpeg_bytes) -> None:
    """Author files with the real Word / PowerPoint, including the legacy binary formats."""
    import subprocess
    import tempfile

    import pythoncom
    import win32com.client

    tmp = tempfile.mkdtemp(prefix="corpus_")
    pic1, pic2 = os.path.join(tmp, "a.jpg"), os.path.join(tmp, "b.png")
    scene(2000, 1400, 60, "진짜 워드 그림").save(pic1, "JPEG", quality=90)
    scene(1400, 1400, 61, "진짜 오피스 그림 2", photo=False).save(pic2, "PNG")

    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    try:
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Add()
        document.Content.InsertAfter("실제 Word로 만든 문서\n")
        rng = document.Content
        rng.Collapse(0)
        document.InlineShapes.AddPicture(pic1, False, True, rng)
        rng = document.Content
        rng.Collapse(0)
        document.InlineShapes.AddPicture(pic2, False, True, rng)
        document.SaveAs2(note("60_실제워드.docx", kind="docx", pictures=2), 16)
        document.SaveAs2(note("61_구형워드.doc", kind="doc", pictures=2), 0)
        document.Close(False)
    finally:
        word.Quit()

    running = "POWERPNT.EXE" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"],
                                               capture_output=True, text=True).stdout.upper()
    ppt = win32com.client.DispatchEx("PowerPoint.Application")
    try:
        pres = ppt.Presentations.Add(WithWindow=False)
        slide = pres.Slides.Add(1, 12)  # blank
        slide.Shapes.AddPicture(pic1, False, True, 20, 20)
        slide = pres.Slides.Add(2, 12)
        slide.Shapes.AddPicture(pic2, False, True, 40, 40)
        pres.SaveAs(note("62_실제파워포인트.pptx", kind="pptx", pictures=2), 24)
        pres.SaveAs(note("63_구형파워포인트.ppt", kind="ppt", pictures=2), 1)
        pres.Close()
    finally:
        if not running:
            ppt.Quit()
        pythoncom.CoUninitialize()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = os.path.abspath(args[0]) if args else os.path.join(ROOT, "tests", "corpus")
    manifest = build(out, "--office" in sys.argv)
    print(f"{len(manifest)} files -> {out}")


if __name__ == "__main__":
    main()
