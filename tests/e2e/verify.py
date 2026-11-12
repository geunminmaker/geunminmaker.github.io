"""Independent checks of what the app hands back.

``verify_outputs`` receives the original file, the choice that was made in the
app (which picture, grid, format, paper) and the downloaded files, and returns a
list of problems (empty = everything is right).

The central check does not trust the engine: every tile is shrunk back to the
area it came from and pasted into a mosaic, and that mosaic must look like the
original picture.  A missing, repeated, mirrored, rotated, mis-ordered,
stretched or cropped tile shows up as a large difference.
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402
from PIL import Image, ImageChops, ImageStat  # noqa: E402

from splitcore import imaging  # noqa: E402
from splitcore.grids import Layout, tile_boxes  # noqa: E402
from splitcore.session import Session  # noqa: E402

MM = 72 / 25.4


def reference(path: str, pick: int = 0, password: str | None = None):
    """Load the original natively: returns (session, source id, RGB image on white)."""
    session = Session()
    with open(path, "rb") as handle:
        result = session.add_file(os.path.basename(path), handle.read(), password)
    meta = result["sources"][pick]
    image = session.get(meta["id"]).image()
    return session, meta["id"], imaging.flatten(image).convert("RGB")


def mosaic(tiles: list[Image.Image], rows: int, cols: int, cell: tuple[int, int]) -> Image.Image:
    """Bring every tile to the same cell size and lay them out on the grid."""
    cell_w, cell_h = cell
    canvas = Image.new("RGB", (cell_w * cols, cell_h * rows), (255, 0, 255))  # magenta = nothing pasted here
    for index, tile in enumerate(tiles[: rows * cols]):
        tile = imaging.flatten(tile).convert("RGB")
        if tile.size != cell:
            tile = tile.resize(cell, Image.LANCZOS)
        canvas.paste(tile, ((index % cols) * cell_w, (index // cols) * cell_h))
    return canvas


def difference(a: Image.Image, b: Image.Image) -> tuple[float, float]:
    """(mean absolute difference, share of clearly different pixels)."""
    diff = ImageChops.difference(a, b).convert("L")
    mean = ImageStat.Stat(diff).mean[0]
    bad = sum(diff.histogram()[64:]) / float(a.width * a.height)
    return mean, bad


def _check_mosaic(problems, tiles, rows, cols, original, label, mean_limit=4.0, bad_limit=0.012):
    """The tiles, put side by side, must show the original picture."""
    if len(tiles) != rows * cols:
        problems.append(f"{label}: {len(tiles)} tiles, expected {rows * cols}")
        return
    tile_w, tile_h = tiles[0].size
    # compare at a moderate, grid-aligned resolution (never above the tile resolution)
    cell_w = max(1, min(tile_w, max(8, 1200 // cols)))
    cell_h = max(1, round(cell_w * tile_h / tile_w))
    assembled = mosaic(tiles, rows, cols, (cell_w, cell_h))
    target = original.resize(assembled.size, Image.LANCZOS)
    mean, bad = difference(target, assembled)
    if mean > mean_limit or bad > bad_limit:
        problems.append(f"{label}: tiles do not reassemble into the original (mean diff {mean:.2f}, bad pixels {bad:.2%})")


def _tile_aspect_ok(problems, tile_size, plan, label):
    if tuple(tile_size) != (plan["out_w"], plan["out_h"]):
        problems.append(f"{label}: tile is {tile_size[0]}x{tile_size[1]}, expected {plan['out_w']}x{plan['out_h']}")


def _pdf_image(doc, item) -> Image.Image:
    """The pixels of an embedded PDF image (with its soft mask) as a PIL image."""
    xref, smask = item[0], item[1]
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is not None and pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if smask:
        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
    mode = "RGBA" if pix.alpha else ("L" if pix.n == 1 else "RGB")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def verify_outputs(source_path: str, files: dict[str, bytes], *, rows: int, cols: int, fmt: str,
                   paper: str = "original", pick: int = 0, password: str | None = None,
                   zipped: bool = False) -> list[str]:
    """``files`` maps downloaded file name -> bytes.  Returns the list of problems."""
    problems: list[str] = []
    session, sid, original = reference(source_path, pick, password)
    try:
        info = session.describe(sid, rows, cols, fmt, {"paper": paper})
        plan = info["plan"]
        stem = info["stem"]
        count = rows * cols
        vector = info.get("vector", False)
        lossy = fmt in ("jpg",) or (fmt in ("pdf", "docx", "pptx", "hwpx") and not vector)
        mean_limit = 5.0 if lossy else 3.0

        if zipped:  # the app bundled the pictures into one archive
            if len(files) != 1:
                problems.append(f"expected one zip, got {sorted(files)}")
                return problems
            (zip_name, blob), = files.items()
            if not zip_name.endswith(".zip"):
                problems.append(f"zip name {zip_name!r}")
            try:
                archive = zipfile.ZipFile(io.BytesIO(blob))
                if archive.testzip() is not None:
                    problems.append("zip: CRC error")
                files = {n: archive.read(n) for n in archive.namelist()}
            except zipfile.BadZipFile as exc:
                problems.append(f"zip unreadable: {exc}")
                return problems

        if fmt in ("jpg", "png"):
            ext = fmt
            expected = [f"{stem}_{i + 1:0{max(2, len(str(count)))}d}.{ext}" for i in range(count)]
            if sorted(files) != sorted(expected):
                problems.append(f"file names {sorted(files)[:3]}... expected {expected[:3]}...")
            tiles = []
            for name in expected:
                if name not in files:
                    continue
                tile = Image.open(io.BytesIO(files[name]))
                tile.load()
                if tile.format != ("JPEG" if fmt == "jpg" else "PNG"):
                    problems.append(f"{name}: real format is {tile.format}")
                _tile_aspect_ok(problems, tile.size, plan, name)
                tiles.append(tile)
            _check_mosaic(problems, tiles, rows, cols, original, fmt, mean_limit)
            return problems

        # ---- single document
        expected_name = f"{stem}.{fmt}"
        if list(files) != [expected_name]:
            problems.append(f"file name {list(files)} expected {expected_name}")
        blob = next(iter(files.values()))
        page = info["page"]

        if fmt == "pdf":
            try:
                doc = pymupdf.open(stream=blob, filetype="pdf")
            except Exception as exc:  # noqa: BLE001
                return problems + [f"pdf unreadable: {exc}"]
            if doc.page_count != count:
                problems.append(f"pdf has {doc.page_count} pages, expected {count}")
            rendered, embedded = [], []
            for index, pg in enumerate(doc):
                if abs(pg.rect.width - page["page_w"] * MM) > 0.6 or abs(pg.rect.height - page["page_h"] * MM) > 0.6:
                    problems.append(f"pdf page {index + 1} is {pg.rect.width:.1f}x{pg.rect.height:.1f} pt, expected "
                                    f"{page['page_w'] * MM:.1f}x{page['page_h'] * MM:.1f}")
                clip = pymupdf.Rect(page["x"] * MM, page["y"] * MM, (page["x"] + page["w"]) * MM, (page["y"] + page["h"]) * MM)
                if not vector:
                    images = pg.get_images(full=True)
                    if len(images) != 1:
                        problems.append(f"pdf page {index + 1}: {len(images)} images")
                    else:
                        _tile_aspect_ok(problems, (images[0][2], images[0][3]), plan, f"pdf page {index + 1}")
                        rect = pg.get_image_rects(images[0][0])[0]
                        if max(abs(rect.x0 - clip.x0), abs(rect.y0 - clip.y0), abs(rect.x1 - clip.x1), abs(rect.y1 - clip.y1)) > 0.6:
                            problems.append(f"pdf page {index + 1}: picture at {rect}, expected {clip}")
                        embedded.append(_pdf_image(doc, images[0]))
                zoom = min(4.0, 1400 / max(clip.width, clip.height))
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
                rendered.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
                # nothing may be drawn outside the picture box
                if page["x"] > 2 or page["y"] > 2:
                    full = pg.get_pixmap(dpi=36, alpha=False)
                    if min(full.pixel(1, 1)) < 250:
                        problems.append(f"pdf page {index + 1}: page corner is not white")
            if vector:
                _check_mosaic(problems, rendered, rows, cols, original, "pdf", mean_limit)
            else:
                # the stored pixels must be the tiles ...
                _check_mosaic(problems, embedded, rows, cols, original, "pdf pixels", mean_limit)
                # ... and the page must show them upright (a viewer interpolates tiny pictures
                # differently, so the rendering is only compared when the tile has real resolution)
                if plan["out_w"] >= 300 and plan["out_h"] >= 300:
                    _check_mosaic(problems, rendered, rows, cols, original, "pdf rendering", mean_limit + 1.5)
            try:
                from pypdf import PdfReader
                if len(PdfReader(io.BytesIO(blob), strict=True).pages) != count:
                    problems.append("pypdf page count mismatch")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"pypdf strict read failed: {exc}")
            return problems

        # ---- zip based documents
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
            if archive.testzip() is not None:
                problems.append(f"{fmt}: CRC error")
        except zipfile.BadZipFile as exc:
            return problems + [f"{fmt} unreadable: {exc}"]
        names = archive.namelist()
        folder = {"docx": "word/media/", "pptx": "ppt/media/", "hwpx": "BinData/"}[fmt]
        media = sorted((n for n in names if n.startswith(folder)),
                       key=lambda n: int(re.search(r"(\d+)\.\w+$", n).group(1)))
        tiles = []
        for name in media:
            tile = Image.open(io.BytesIO(archive.read(name)))
            tile.load()
            _tile_aspect_ok(problems, tile.size, plan, f"{fmt} {name}")
            tiles.append(tile)
        _check_mosaic(problems, tiles, rows, cols, original, fmt, mean_limit)

        if fmt == "docx":
            import docx
            document = docx.Document(io.BytesIO(blob))
            section = document.sections[0]
            if abs(section.page_width.mm - page["page_w"]) > 0.2 or abs(section.page_height.mm - page["page_h"]) > 0.2:
                problems.append(f"docx page {section.page_width.mm:.1f}x{section.page_height.mm:.1f} mm, expected "
                                f"{page['page_w']:.1f}x{page['page_h']:.1f}")
            xml = archive.read("word/document.xml").decode("utf-8")
            if xml.count("<wp:inline ") != count or "<wp:anchor" in xml or xml.count("<w:pageBreakBefore/>") != count - 1:
                problems.append("docx: wrong number of pictures in the line / page breaks")
            # the margins put the picture at its place; the text area must hold it without scaling
            if abs(section.left_margin.mm - page["x"]) > 0.2 or abs(section.top_margin.mm - page["y"]) > 0.2:
                problems.append(f"docx: picture at {section.left_margin.mm:.1f},{section.top_margin.mm:.1f} mm, "
                                f"expected {page['x']:.1f},{page['y']:.1f}")
            room_w = section.page_width.mm - section.left_margin.mm - section.right_margin.mm
            room_h = section.page_height.mm - section.top_margin.mm - section.bottom_margin.mm
            if room_w < page["w"] - 0.05 or room_h < page["h"] - 0.05:
                problems.append(f"docx: text area {room_w:.1f}x{room_h:.1f} mm is smaller than the picture")
            sizes = set(re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml))
            want = (round(page["w"] * 36000), round(page["h"] * 36000))
            if len(sizes) != 1 or max(abs(int(a) - b) for a, b in zip(next(iter(sizes)), want)) > 80:
                problems.append(f"docx: picture size {sizes}, expected {want} EMU")
            order = re.findall(r'r:embed="rId(\d+)"', xml)
            if order != [str(i + 10) for i in range(count)]:
                problems.append(f"docx: picture order {order}")
        elif fmt == "pptx":
            import pptx
            from pptx.util import Emu
            presentation = pptx.Presentation(io.BytesIO(blob))
            if abs(Emu(presentation.slide_width).mm - page["page_w"]) > 0.2 or abs(Emu(presentation.slide_height).mm - page["page_h"]) > 0.2:
                problems.append("pptx: slide size differs from the page spec")
            if len(presentation.slides) != count:
                problems.append(f"pptx has {len(presentation.slides)} slides, expected {count}")
            for index, slide in enumerate(presentation.slides):
                shapes = list(slide.shapes)
                if len(shapes) != 1 or shapes[0].shape_type != 13:
                    problems.append(f"pptx slide {index + 1}: expected exactly one picture")
                    continue
                shape = shapes[0]
                got = (Emu(shape.left).mm, Emu(shape.top).mm, Emu(shape.width).mm, Emu(shape.height).mm)
                want = (page["x"], page["y"], page["w"], page["h"])
                if max(abs(g - w) for g, w in zip(got, want)) > 0.2:
                    problems.append(f"pptx slide {index + 1}: picture at {got}, expected {want}")
                part = str(slide.part.related_part(shape._element.blip_rId).partname)
                if not re.search(rf"/image{index + 1}\.\w+$", part):
                    problems.append(f"pptx slide {index + 1}: shows {part}")
        elif fmt == "hwpx":
            if archive.namelist()[0] != "mimetype" or archive.read("mimetype") != b"application/hwp+zip":
                problems.append("hwpx: mimetype entry must come first and be application/hwp+zip")
            if archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
                problems.append("hwpx: mimetype must be stored uncompressed")
            import xml.dom.minidom
            for name in names:
                if name.endswith((".xml", ".hpf", ".rdf")):
                    try:
                        xml.dom.minidom.parseString(archive.read(name))
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"hwpx: {name} is not well-formed XML: {exc}")
            section = archive.read("Contents/section0.xml").decode("utf-8")
            if section.count("<hp:pic ") != count:
                problems.append(f"hwpx: {section.count('<hp:pic ')} pictures in the body, expected {count}")
        return problems
    finally:
        session.clear()
