"""Open generated Word / PowerPoint files in the real Microsoft Office apps.

For every sample it checks that the file opens without a repair prompt, has
exactly one page (slide) per tile, and - by letting Office itself export a PDF
and rasterising that - that each page shows the right tile at the right place.

    .venv/Scripts/python tools/verify_office.py [output_dir]

Windows + Microsoft Office only.  Not part of the automated test suite.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from splitcore.session import Session  # noqa: E402


def cell_colour(i: int):
    return ((i * 53) % 200 + 30, (i * 97) % 200 + 30, (i * 151) % 200 + 30)


def poster(w, h, rows, cols, alpha=False):
    im = Image.new("RGBA" if alpha else "RGB", (w, h), (255, 255, 255, 0) if alpha else "white")
    draw = ImageDraw.Draw(im)
    for r in range(rows):
        for c in range(cols):
            i = r * cols + c
            box = (c * w // cols, r * h // rows, (c + 1) * w // cols - 1, (r + 1) * h // rows - 1)
            if alpha:
                draw.ellipse(box, fill=cell_colour(i) + (255,))
            else:
                draw.rectangle(box, fill=cell_colour(i))
    return im


def as_bytes(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


CASES = [
    # name, (w, h), rows, cols, paper, alpha
    ("land_3x3_original", (1200, 900), 3, 3, "original", False),
    ("land_2x3_original", (1200, 800), 2, 3, "original", False),
    ("port_3x2_a4", (900, 1300), 3, 2, "a4", False),
    ("port_6x3_a4", (1000, 1414), 6, 3, "a4", False),
    ("square_4x4_original", (1000, 1000), 4, 4, "original", False),
    ("alpha_2x2_original", (800, 800), 2, 2, "original", True),
    ("pano_1x2_original", (3000, 600), 1, 2, "original", False),
]


def check_pdf(pdf_path, session, sid, rows, cols, fmt, paper, alpha):
    """Compare Office's own PDF rendering against the expected tiles."""
    info = session.describe(sid, rows, cols, fmt, {"paper": paper})
    page = info["page"]
    doc = pymupdf.open(pdf_path)
    problems = []
    if doc.page_count != rows * cols:
        problems.append(f"pages={doc.page_count} expected={rows * cols}")
    for i, pg in enumerate(doc):
        if i >= rows * cols:
            break
        want_w, want_h = page["page_w"] * 72 / 25.4, page["page_h"] * 72 / 25.4
        if abs(pg.rect.width - want_w) > 1.5 or abs(pg.rect.height - want_h) > 1.5:
            problems.append(f"p{i + 1} size {pg.rect.width:.1f}x{pg.rect.height:.1f} want {want_w:.1f}x{want_h:.1f}")
        pix = pg.get_pixmap(dpi=50, alpha=False)
        sx, sy = pix.width / page["page_w"], pix.height / page["page_h"]
        cx = int((page["x"] + page["w"] / 2) * sx)
        cy = int((page["y"] + page["h"] / 2) * sy)
        got = pix.pixel(cx, cy)
        want = cell_colour(i)
        if max(abs(a - b) for a, b in zip(got, want)) > 16:
            problems.append(f"p{i + 1} centre colour {got} want {want}")
        # the picture must reach the edges of its box and not beyond
        inside = pix.pixel(int((page["x"] + page["w"] * 0.5) * sx), int((page["y"] + 1.0) * sy + 1))
        if not alpha and max(abs(a - b) for a, b in zip(inside, want)) > 16:
            problems.append(f"p{i + 1} top edge colour {inside} want {want}")
        if page["y"] > 3:
            outside = pix.pixel(cx, max(0, int((page["y"] - 2.0) * sy)))
            if min(outside) < 245:
                problems.append(f"p{i + 1} expected white above the picture, got {outside}")
        if page["x"] > 3:
            outside = pix.pixel(max(0, int((page["x"] - 2.0) * sx)), cy)
            if min(outside) < 245:
                problems.append(f"p{i + 1} expected white left of the picture, got {outside}")
    return problems


def main():
    import pythoncom
    import win32com.client

    out_dir = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="split_office_")
    os.makedirs(out_dir, exist_ok=True)
    session = Session()
    jobs = []
    for name, (w, h), rows, cols, paper, alpha in CASES:
        src = as_bytes(poster(w, h, rows, cols, alpha), "PNG") if alpha else as_bytes(poster(w, h, rows, cols), "JPEG", quality=95)
        sid = session.add_file(name + (".png" if alpha else ".jpg"), src)["sources"][0]["id"]
        for fmt in ("docx", "pptx"):
            (f,) = list(session.export(sid, rows, cols, fmt, {"paper": paper}))
            path = os.path.join(out_dir, f"{name}.{fmt}")
            with open(path, "wb") as handle:
                handle.write(f.data)
            jobs.append((path, fmt, sid, rows, cols, paper, alpha))

    pythoncom.CoInitialize()
    failures = 0
    word = ppt = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        for path, fmt, sid, rows, cols, paper, alpha in jobs:
            if fmt != "docx":
                continue
            pdf_path = path + ".pdf"
            try:
                doc = word.Documents.Open(path, ConfirmConversions=False, ReadOnly=True,
                                          AddToRecentFiles=False, OpenAndRepair=False, Visible=False)
                pages = doc.ComputeStatistics(2)
                shapes = doc.Shapes.Count
                mode = doc.CompatibilityMode
                doc.ExportAsFixedFormat(pdf_path, 17)
                doc.Close(False)
                problems = check_pdf(pdf_path, session, sid, rows, cols, fmt, paper, alpha)
                if pages != rows * cols:
                    problems.append(f"Word page count {pages}")
                if shapes != rows * cols:
                    problems.append(f"Word shape count {shapes}")
                status = "OK " if not problems else "BAD"
                print(f"{status} {os.path.basename(path)} pages={pages} shapes={shapes} compat={mode} {problems}")
                failures += bool(problems)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERR {os.path.basename(path)} {exc}")
    finally:
        if word is not None:
            word.Quit()
    # PowerPoint is single-instance: never quit it if the user already had it open.
    import subprocess
    ppt_was_running = "POWERPNT.EXE" in subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"], capture_output=True, text=True).stdout.upper()
    try:
        ppt = win32com.client.DispatchEx("PowerPoint.Application")
        for path, fmt, sid, rows, cols, paper, alpha in jobs:
            if fmt != "pptx":
                continue
            pdf_path = path + ".pdf"
            try:
                pres = ppt.Presentations.Open(path, ReadOnly=True, Untitled=False, WithWindow=False)
                slides = pres.Slides.Count
                size = (pres.PageSetup.SlideWidth, pres.PageSetup.SlideHeight)
                pres.SaveAs(pdf_path, 32)
                pres.Close()
                problems = check_pdf(pdf_path, session, sid, rows, cols, fmt, paper, alpha)
                if slides != rows * cols:
                    problems.append(f"slide count {slides}")
                status = "OK " if not problems else "BAD"
                print(f"{status} {os.path.basename(path)} slides={slides} size_pt={size[0]:.1f}x{size[1]:.1f} {problems}")
                failures += bool(problems)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERR {os.path.basename(path)} {exc}")
    finally:
        if ppt is not None and not ppt_was_running:
            ppt.Quit()
        pythoncom.CoUninitialize()
    print(f"output: {out_dir}")
    print("ALL OK" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
