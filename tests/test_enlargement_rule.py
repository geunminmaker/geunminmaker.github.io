"""The enlargement rule: a tile keeps its shape and grows as far as it fits inside the original
picture's size, where that box may be turned by 90 degrees when the tile gets larger that way."""
from __future__ import annotations

import io
import re
import zipfile

import pymupdf
import pytest
from PIL import Image, ImageChops, ImageStat

from splitcore.grids import SPLIT_COUNTS, Layout, factor_pairs, layouts_for, output_size, scale_factor
from splitcore.session import Session
from splitcore.sources import RasterSource
from splitcore.splitter import iter_tiles, make_plan, page_spec


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def gradient(w: int, h: int) -> Image.Image:
    """Smooth ramps in three directions, so a misplaced or mirrored tile is detectable."""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), (x + y) * 255 // max(1, w + h - 2))
    return im


@pytest.fixture()
def session():
    s = Session()
    yield s
    s.clear()


# ---------------------------------------------------------------- scale and tile size

@pytest.mark.parametrize("width,height,cols,rows,scale,out", [
    (4032, 3024, 2, 1, 4 / 3, (2688, 4032)),   # two portrait halves: each grows into the turned original
    (4032, 3024, 3, 2, 2.25, (3024, 3402)),
    (3000, 2000, 3, 2, 2.0, (2000, 2000)),     # square tiles: turning the box changes nothing
    (4032, 3024, 1, 2, 1.0, (4032, 1512)),     # a strip as wide as the picture cannot grow
    (3024, 4032, 1, 2, 4 / 3, (4032, 2688)),
    (2000, 2000, 2, 1, 1.0, (1000, 2000)),     # square picture: the turned box is the same box
])
def test_scale_and_output_size(width, height, cols, rows, scale, out):
    layout = Layout(rows, cols)
    assert scale_factor(layout, width, height) == pytest.approx(scale, abs=1e-9)
    assert output_size(layout, width, height) == out
    plan = make_plan(width, height, rows, cols)
    assert (plan.out_w, plan.out_h) == out and not plan.capped
    assert plan.to_dict()["scale"] == round(scale, 4)
    option = next(o for o in layouts_for(rows * cols, width, height) if o["key"] == layout.key)
    assert (option["out_w"], option["out_h"]) == out
    assert option["scale"] == round(scale, 4) and isinstance(option["scale"], float)


def test_a4_landscape_in_two_fills_the_turned_sheet():
    # 3508 x 2480 is A4 at 300 dpi rounded to whole pixels, so its sides are not exactly 1 : sqrt(2);
    # the half that touches the full width of the turned sheet ends 1.5 px short of its height
    out_w, out_h = output_size(Layout(1, 2), 3508, 2480)
    assert out_w == 2480 and abs(out_h - 3508) <= 2
    assert scale_factor(Layout(1, 2), 3508, 2480) == pytest.approx(2 * 2480 / 3508)


@pytest.mark.parametrize("width,height", [(4032, 3024), (3024, 4032), (2000, 2000), (1001, 997), (1170, 2532)])
def test_square_grids_are_unchanged(width, height):
    for side in (2, 3, 4):
        layout = Layout(side, side)
        assert scale_factor(layout, width, height) == side
        assert output_size(layout, width, height) == (width, height)
        option = next(o for o in layouts_for(side * side, width, height) if o["key"] == layout.key)
        assert option["fills_original"] and option["scale"] == side
        assert make_plan(width, height, side, side).fills_original


@pytest.mark.parametrize("width,height", [(4032, 3024), (3024, 4032), (2000, 2000), (6000, 1200), (1170, 2532),
                                           (1001, 997), (3508, 2480)])
def test_tile_touches_the_original_box_in_one_of_its_two_positions(width, height):
    for count in SPLIT_COUNTS:
        for layout in factor_pairs(count):
            scale = scale_factor(layout, width, height)
            out_w, out_h = output_size(layout, width, height)
            tile_w, tile_h = width / layout.cols, height / layout.rows
            assert scale >= 1.0
            assert out_w == pytest.approx(tile_w * scale, abs=0.51) and out_h == pytest.approx(tile_h * scale, abs=0.51)
            # the shape is kept
            assert out_w / out_h == pytest.approx(tile_w / tile_h, rel=0.02)
            fits_upright = out_w <= width + 0.51 and out_h <= height + 0.51
            fits_turned = out_w <= height + 0.51 and out_h <= width + 0.51
            assert fits_upright or fits_turned, (layout, out_w, out_h)
            # ... and it cannot grow any more: it reaches a side of the box it sits in
            touches_upright = fits_upright and (abs(out_w - width) <= 0.51 or abs(out_h - height) <= 0.51)
            touches_turned = fits_turned and (abs(out_w - height) <= 0.51 or abs(out_h - width) <= 0.51)
            assert touches_upright or touches_turned, (layout, out_w, out_h)
            # never smaller than the old rule (growing inside the upright box only)
            assert scale >= min(layout.rows, layout.cols)


def test_fills_original_in_either_position():
    def option(width, height, cols, rows):
        return next(o for o in layouts_for(rows * cols, width, height) if o["key"] == f"{cols}x{rows}")

    turned = option(4000, 2000, 4, 1)  # four 1000 x 2000 strips: each becomes 2000 x 4000
    assert (turned["out_w"], turned["out_h"]) == (2000, 4000) and turned["fills_original"]
    assert make_plan(4000, 2000, 1, 4).fills_original
    near = option(3999, 2000, 4, 1)  # one pixel off still counts
    assert (near["out_w"], near["out_h"]) == (1999, 3999) and near["fills_original"]
    assert not option(4032, 3024, 2, 1)["fills_original"]  # 2688 x 4032 inside 3024 x 4032
    assert not option(4032, 3024, 1, 2)["fills_original"]
    assert not make_plan(4032, 3024, 1, 2).fills_original


def test_pixel_cap_still_shrinks_the_tile_in_proportion():
    free = make_plan(8000, 6000, 1, 2, max_tile_pixels=10 ** 9)
    assert (free.out_w, free.out_h) == (5333, 8000) and not free.capped
    plan = make_plan(8000, 6000, 1, 2, max_tile_pixels=12_000_000)
    assert plan.capped and plan.out_w * plan.out_h <= 12_000_000
    assert plan.out_w * plan.out_h > 11_900_000  # shrunk only as far as needed
    assert plan.out_w / plan.out_h == pytest.approx(4000 / 6000, rel=0.001)
    assert not plan.fills_original and plan.to_dict()["scale"] == 1.3333
    same = make_plan(4032, 3024, 3, 3, max_tile_pixels=12_000_000)
    assert same.capped and same.out_w * same.out_h <= 12_000_000


def test_session_plan_honours_max_pixels(session):
    sid = session.add_file("a.png", _png(Image.new("RGB", (2400, 1800), "white")))["sources"][0]["id"]
    plan = session.describe(sid, 1, 2)["plan"]
    assert (plan["out_w"], plan["out_h"], plan["capped"], plan["scale"]) == (1600, 2400, False, 1.3333)
    small = session.describe(sid, 1, 2, None, {"max_pixels": 1_000_000})["plan"]
    assert small["capped"] and small["out_w"] * small["out_h"] <= 1_000_000
    assert small["out_w"] / small["out_h"] == pytest.approx(2 / 3, rel=0.005)


# ---------------------------------------------------------------- tiles

@pytest.mark.parametrize("width,height,rows,cols", [(240, 180, 1, 2), (240, 180, 2, 3), (180, 240, 2, 1),
                                                     (180, 240, 3, 2), (240, 180, 2, 4)])
def test_enlarged_tiles_reassemble_into_the_source(width, height, rows, cols):
    original = gradient(width, height)
    src = RasterSource(_png(original), label="g.png", origin="g.png")
    plan = make_plan(width, height, rows, cols)
    tiles = list(iter_tiles(src, plan))
    assert [i for i, _ in tiles] == list(range(rows * cols))
    assert all(t.size == (plan.out_w, plan.out_h) for _, t in tiles)
    # really enlarged, and by more than the old rule gave
    assert plan.out_w * cols > width * min(rows, cols) and plan.out_h * rows > height * min(rows, cols)
    mosaic = Image.new("RGB", (width, height))
    for index, tile in tiles:
        x0, y0, x1, y1 = plan.boxes[index]
        mosaic.paste(tile.resize((round(x1 - x0), round(y1 - y0)), Image.BOX), (round(x0), round(y0)))
    diff = [abs(a - b) for a, b in zip(mosaic.tobytes(), original.tobytes())]
    assert sum(diff) / len(diff) < 1.0
    assert max(diff) <= 4


def test_picture_export_uses_the_new_size(session):
    sid = session.add_file("p.png", _png(gradient(600, 400)))["sources"][0]["id"]
    files = list(session.export(sid, 1, 2, "png"))
    assert [Image.open(io.BytesIO(f.data)).size for f in files] == [(400, 533), (400, 533)]
    files = list(session.export(sid, 2, 4, "jpg"))  # 150 x 200 tiles: 2.67 times into the turned 400 x 600
    assert {Image.open(io.BytesIO(f.data)).size for f in files} == {(400, 533)}


# ---------------------------------------------------------------- pages

def test_original_paper_is_turned_when_the_tile_fits_better():
    spec = page_spec(make_plan(4000, 3000, 1, 2), "original")  # two portrait halves of a landscape photo
    assert (round(spec.page_w), round(spec.page_h)) == (210, 280)
    assert round(spec.h) == 280 and round(spec.w, 1) == pytest.approx(186.7, abs=0.1)
    assert round(spec.y) == 0 and spec.x == pytest.approx((210 - spec.w) / 2)

    spec = page_spec(make_plan(4032, 3024, 2, 3), "original")
    assert (round(spec.page_w), round(spec.page_h)) == (210, 280)
    assert round(spec.w) == 210 and round(spec.h) == 236

    spec = page_spec(make_plan(3000, 4000, 2, 1), "original")  # portrait photo in two landscape halves
    assert (round(spec.page_w), round(spec.page_h)) == (280, 210)

    # no gain: the page stays as the picture lies
    for rows, cols in ((3, 3), (2, 1), (2, 2)):
        spec = page_spec(make_plan(4000, 3000, rows, cols), "original")
        assert (round(spec.page_w), round(spec.page_h)) == (280, 210), (rows, cols)
    spec = page_spec(make_plan(3000, 2000, 2, 3), "original")  # square tiles: a tie keeps the orientation
    assert (round(spec.page_w), round(spec.page_h)) == (297, 198)
    spec = page_spec(make_plan(2000, 2000, 1, 2), "original")  # square page: nothing to turn
    assert spec.page_w == spec.page_h

    # a margin does not change the decision
    spec = page_spec(make_plan(4000, 3000, 1, 2), "original", margin_mm=10)
    assert (round(spec.page_w), round(spec.page_h)) == (210, 280) and round(spec.h) == 260


def test_original_paper_keeps_the_physical_size_and_turns_it():
    a4_landscape = (297.0, 210.0)
    spec = page_spec(make_plan(3508, 2480, 1, 2), "original", physical_mm=a4_landscape)
    assert (spec.page_w, spec.page_h) == (210.0, 297.0)
    assert spec.w == pytest.approx(210.0, abs=0.01) and spec.h == pytest.approx(297.0, abs=0.2)
    spec = page_spec(make_plan(3508, 2480, 2, 2), "original", physical_mm=a4_landscape)
    assert (spec.page_w, spec.page_h) == a4_landscape
    spec = page_spec(make_plan(3508, 2480, 2, 1), "original", physical_mm=a4_landscape)
    assert (spec.page_w, spec.page_h) == a4_landscape


def test_a4_paper_still_follows_the_tile():
    assert page_spec(make_plan(4000, 3000, 1, 2), "a4").page_w == 210.0   # portrait tile
    assert page_spec(make_plan(4000, 3000, 2, 1), "a4").page_w == 297.0   # landscape strip
    assert page_spec(make_plan(4000, 3000, 3, 3), "a4").page_w == 297.0


def _a4_landscape_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.draw_rect(pymupdf.Rect(0, 0, 421, 595), fill=(0.85, 0.2, 0.2), color=None)       # left half red
    page.draw_rect(pymupdf.Rect(421, 0, 842, 595), fill=(0.2, 0.3, 0.85), color=None)     # right half blue
    page.draw_rect(pymupdf.Rect(40, 40, 140, 90), fill=(1, 1, 0), color=None)             # marks the top-left corner
    page.draw_rect(pymupdf.Rect(702, 505, 802, 555), fill=(0, 1, 0), color=None)          # marks the bottom-right corner
    page.insert_text((60, 300), "LEFT half", fontsize=40, color=(1, 1, 1))
    page.insert_text((480, 300), "RIGHT half", fontsize=40, color=(1, 1, 1))
    data = doc.tobytes()
    doc.close()
    return data


def test_a4_landscape_pdf_in_two_gives_two_a4_portrait_vector_pages(session):
    data = _a4_landscape_pdf()
    truth_page = pymupdf.open(stream=data, filetype="pdf")[0]
    sid = session.add_file("가로.pdf", data)["sources"][0]["id"]
    info = session.describe(sid, 1, 2, "pdf")
    assert info["vector"] and info["plan"]["scale"] == pytest.approx(1.4133, abs=0.001)
    page = info["page"]
    assert (round(page["page_w"]), round(page["page_h"])) == (210, 297)
    assert page["w"] == pytest.approx(page["page_w"], abs=0.01) and page["h"] == pytest.approx(297, abs=0.5)

    (out,) = list(session.export(sid, 1, 2, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc.page_count == 2
    mm = 72 / 25.4
    for index, pg in enumerate(doc):
        assert pg.rect.width == pytest.approx(595, abs=0.5) and pg.rect.height == pytest.approx(842, abs=0.5)
        assert not pg.get_images()  # still vector
        text = pg.get_text()
        assert ("LEFT" in text) == (index == 0) and ("RIGHT" in text) == (index == 1)
        spans = [s for b in pg.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
        assert spans[0]["size"] == pytest.approx(40 * 842 / 595, rel=0.02)  # enlarged with the half page
        # exactly the half of the page: compare with a direct rendering of that half
        box = pymupdf.Rect(page["x"] * mm, page["y"] * mm, (page["x"] + page["w"]) * mm, (page["y"] + page["h"]) * mm)
        got = pg.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), clip=box, alpha=False)
        half = pymupdf.Rect(421 * index, 0, 421 * (index + 1), 595)
        want = truth_page.get_pixmap(matrix=pymupdf.Matrix(got.width / half.width, got.height / half.height),
                                     clip=half, alpha=False)
        a = Image.frombytes("RGB", (got.width, got.height), got.samples)
        b = Image.frombytes("RGB", (want.width, want.height), want.samples).resize(a.size)
        assert ImageStat.Stat(ImageChops.difference(a, b).convert("L")).mean[0] < 1.5
    # the corner marks are where they belong (not mirrored, not rotated)
    first = doc[0].get_pixmap(dpi=36, alpha=False)
    assert first.pixel(int(first.width * 0.2), int(first.height * 0.11))[:2] == (255, 255)   # yellow
    last = doc[1].get_pixmap(dpi=36, alpha=False)
    assert last.pixel(int(last.width * 0.8), int(last.height * 0.89)) == (0, 255, 0)         # green

    tiles = list(session.export(sid, 1, 2, "png"))
    assert [Image.open(io.BytesIO(t.data)).size for t in tiles] == [(info["plan"]["out_w"], info["plan"]["out_h"])] * 2


def _turned_and_cropped_pdf() -> bytes:
    """Four pages (0 / 90 / 180 / 270 degrees), each with a crop box that hides a magenta border."""
    doc = pymupdf.open()
    colours = [(0.86, 0.16, 0.16), (0.16, 0.7, 0.27), (0.16, 0.31, 0.86), (0.94, 0.78, 0.08)]
    for rotation in (0, 90, 180, 270):
        page = doc.new_page(width=600, height=400)
        page.draw_rect(page.rect, fill=(1, 0, 1), color=None)
        inner = pymupdf.Rect(50, 40, 550, 360)
        for i, colour in enumerate(colours):
            r, c = divmod(i, 2)
            page.draw_rect(pymupdf.Rect(inner.x0 + c * 250, inner.y0 + r * 160,
                                        inner.x0 + (c + 1) * 250, inner.y0 + (r + 1) * 160), fill=colour, color=None)
        page.insert_text((90, 150), "top left", fontsize=36, color=(1, 1, 1))
        page.draw_circle(pymupdf.Point(430, 280), 50, fill=(1, 1, 1), color=None)
        page.set_cropbox(inner)
        if rotation:
            page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("index", [0, 1, 2, 3])
@pytest.mark.parametrize("rows,cols", [(1, 2), (2, 1), (2, 3)])
def test_vector_tiles_of_turned_pdf_pages_show_exactly_their_part(session, index, rows, cols):
    data = _turned_and_cropped_pdf()
    truth_page = pymupdf.open(stream=data, filetype="pdf")[index]
    shown = truth_page.rect  # the page as displayed: rotation and crop box applied
    meta = session.add_file("rot.pdf", data)["sources"][index]
    page = session.describe(meta["id"], rows, cols, "pdf")["page"]
    # the paper is the page's own size, as it lies or turned by 90 degrees
    assert sorted((page["page_w"], page["page_h"])) == pytest.approx(sorted(meta["physical_mm"]), abs=0.01)
    mm = 72 / 25.4
    box = pymupdf.Rect(page["x"] * mm, page["y"] * mm, (page["x"] + page["w"]) * mm, (page["y"] + page["h"]) * mm)
    (out,) = list(session.export(meta["id"], rows, cols, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc.page_count == rows * cols
    for tile, pg in enumerate(doc):
        assert not pg.get_images()
        assert pg.rect.width == pytest.approx(page["page_w"] * mm, abs=0.5)
        assert pg.rect.height == pytest.approx(page["page_h"] * mm, abs=0.5)
        got = pg.get_pixmap(clip=box, alpha=False)
        r, c = divmod(tile, cols)
        cell = pymupdf.Rect(shown.width * c / cols, shown.height * r / rows,
                            shown.width * (c + 1) / cols, shown.height * (r + 1) / rows)
        want = truth_page.get_pixmap(matrix=pymupdf.Matrix(got.width / cell.width, got.height / cell.height),
                                     clip=cell, alpha=False)
        a = Image.frombytes("RGB", (got.width, got.height), got.samples)
        b = Image.frombytes("RGB", (want.width, want.height), want.samples).resize(a.size)
        difference = ImageChops.difference(a, b).convert("L")
        assert ImageStat.Stat(difference).mean[0] < 1.5, (index, tile)
        assert sum(difference.histogram()[64:]) / (a.width * a.height) < 0.01, (index, tile)  # no shifted edges


def test_document_exports_put_the_turned_page_into_the_file(session):
    buf = io.BytesIO()
    Image.effect_noise((1200, 900), 60).convert("RGB").save(buf, "JPEG", quality=85)
    sid = session.add_file("사진.jpg", buf.getvalue())["sources"][0]["id"]
    plan = session.describe(sid, 1, 2)["plan"]
    assert (plan["out_w"], plan["out_h"]) == (800, 1200)

    import docx
    import pptx
    from pptx.util import Emu

    page = session.describe(sid, 1, 2, "docx")["page"]
    assert (round(page["page_w"]), round(page["page_h"])) == (210, 280)
    (out,) = list(session.export(sid, 1, 2, "docx"))
    section = docx.Document(io.BytesIO(out.data)).sections[0]
    assert section.page_width.mm == pytest.approx(210, abs=0.1) and section.page_height.mm == pytest.approx(280, abs=0.1)
    xml = zipfile.ZipFile(io.BytesIO(out.data)).read("word/document.xml").decode("utf-8")
    assert 'w:orient="landscape"' not in xml
    cx, cy = map(int, re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml).groups())
    assert cy == pytest.approx(280 * 36000, abs=40) and cx == pytest.approx(page["w"] * 36000, abs=40)
    first = Image.open(io.BytesIO(zipfile.ZipFile(io.BytesIO(out.data)).read("word/media/image1.jpg")))
    assert first.size == (800, 1200)

    (out,) = list(session.export(sid, 1, 2, "pptx"))
    presentation = pptx.Presentation(io.BytesIO(out.data))
    assert Emu(presentation.slide_width).mm == pytest.approx(210, abs=0.1)
    assert Emu(presentation.slide_height).mm == pytest.approx(280, abs=0.1)
    (shape,) = list(presentation.slides[0].shapes)
    assert Emu(shape.height).mm == pytest.approx(280, abs=0.1) and Emu(shape.width).mm == pytest.approx(page["w"], abs=0.1)

    (out,) = list(session.export(sid, 1, 2, "hwpx"))
    section_xml = zipfile.ZipFile(io.BytesIO(out.data)).read("Contents/section0.xml").decode("utf-8")
    assert section_xml.count("<hp:pic ") == 2
    back = Session()
    pictures = back.add_file("결과.hwpx", out.data)["sources"]
    assert [(p["width"], p["height"]) for p in pictures] == [(800, 1200)] * 2
    back.clear()

    (out,) = list(session.export(sid, 1, 2, "pdf"))
    doc = pymupdf.open(stream=out.data, filetype="pdf")
    assert doc[0].rect.width == pytest.approx(210 * 72 / 25.4, abs=0.05)
    assert doc[0].rect.height == pytest.approx(280 * 72 / 25.4, abs=0.05)
    image = doc[0].get_images(full=True)[0]
    assert (image[2], image[3]) == (800, 1200)
