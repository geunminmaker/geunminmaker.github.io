"""Unit tests for grids, tile geometry, raster handling and naming."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from splitcore import imaging
from splitcore.errors import SplitError
from splitcore.grids import (SPLIT_COUNTS, Layout, factor_pairs, layouts_for, output_size,
                             parse_layout, recommended_layout, tile_boxes)
from splitcore.sources import RasterSource, raster_sources
from splitcore.splitter import (iter_tiles, make_plan, output_stem, page_spec, safe_stem,
                                tile_name)


def _png(im: Image.Image, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", **kw)
    return buf.getvalue()


def _jpeg(im: Image.Image, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def gradient(w: int, h: int) -> Image.Image:
    """Smooth ramps in three directions, so a misplaced or mirrored tile is detectable."""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), (x + y) * 255 // max(1, w + h - 2))
    return im


# ---------------------------------------------------------------- grids

@pytest.mark.parametrize("n,landscape,portrait", [
    (2, "2x1", "1x2"), (4, "2x2", "2x2"), (6, "3x2", "2x3"), (8, "4x2", "2x4"),
    (9, "3x3", "3x3"), (12, "4x3", "3x4"), (16, "4x4", "4x4"), (18, "6x3", "3x6"),
])
def test_recommended_layouts(n, landscape, portrait):
    assert recommended_layout(n, 4000, 3000).key == landscape
    assert recommended_layout(n, 3000, 4000).key == portrait
    assert recommended_layout(n, 2000, 2000).key == landscape  # square behaves like landscape


@pytest.mark.parametrize("n", SPLIT_COUNTS)
def test_layout_list_is_complete_and_unique(n):
    options = layouts_for(n, 4000, 3000)
    assert [o["recommended"] for o in options].count(True) == 1
    assert options[0]["recommended"]
    assert sorted((o["rows"], o["cols"]) for o in options) == sorted((l.rows, l.cols) for l in factor_pairs(n))
    assert all(o["rows"] * o["cols"] == n for o in options)


def test_square_grids_keep_the_original_size():
    for n, side in ((4, 2), (9, 3), (16, 4)):
        assert output_size(Layout(side, side), 4032, 3024) == (4032, 3024)
    # 2 rows x 3 columns: enlarged twice -> touches the original height, narrower than the width
    assert output_size(Layout(2, 3), 3000, 2000) == (2000, 2000)
    assert output_size(Layout(3, 2), 2000, 3000) == (2000, 2000)
    # two portrait halves of a landscape picture grow into the original size turned by 90 degrees
    assert output_size(Layout(1, 2), 4000, 3000) == (2667, 4000)
    # a strip across the whole picture cannot grow in either position
    assert output_size(Layout(2, 1), 4000, 3000) == (4000, 1500)


def test_tile_boxes_cover_everything_without_gaps():
    for layout in (Layout(3, 3), Layout(2, 3), Layout(6, 3), Layout(1, 18)):
        boxes = tile_boxes(layout, 1001, 997)  # not divisible on purpose
        assert len(boxes) == layout.count
        area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
        assert area == pytest.approx(1001 * 997)
        assert boxes[0][:2] == (0, 0)
        assert boxes[-1][2:] == (1001, 997)
        # reading order: left to right, then top to bottom
        assert boxes[1][0] >= boxes[0][2] - 1e-9 if layout.cols > 1 else boxes[1][1] > boxes[0][1]


def test_parse_layout_and_bad_input():
    assert parse_layout("3x2") == Layout(2, 3)
    assert parse_layout("3×2") == Layout(2, 3)
    for bad in ("", "3", "0x3", "axb", "100x100", None):
        with pytest.raises(SplitError):
            parse_layout(bad)
    with pytest.raises(SplitError):
        factor_pairs(0)


# ---------------------------------------------------------------- imaging

def test_exif_orientation_is_applied():
    im = gradient(60, 40)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW to view
    src = RasterSource(_jpeg(im, exif=exif, quality=95), label="a.jpg", origin="a.jpg")
    assert src.size == (40, 60)
    assert src.image().size == (40, 60)
    assert src.thumbnail(20).size == (13, 20)


def test_modes_are_normalised():
    cmyk = Image.new("CMYK", (30, 20), (0, 255, 255, 0))
    out = imaging.decode(_jpeg(cmyk))
    assert out.mode == "RGB"
    r, g, b = out.getpixel((5, 5))
    assert r > 200 and g < 60 and b < 60  # red survives the CMYK conversion

    pal = Image.new("P", (10, 10))
    pal.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    assert imaging.decode(_png(pal)).mode == "RGB"
    assert imaging.decode(_png(pal, transparency=0)).mode == "RGBA"

    opaque_rgba = Image.new("RGBA", (10, 10), (1, 2, 3, 255))
    assert imaging.decode(_png(opaque_rgba)).mode == "RGB"
    assert imaging.decode(_png(Image.new("RGBA", (10, 10), (1, 2, 3, 100)))).mode == "RGBA"
    assert imaging.decode(_png(Image.new("1", (10, 10), 1))).mode == "L"

    deep = Image.new("I;16", (10, 10), 65535)
    out = imaging.decode(_png(deep))
    assert out.mode == "L" and out.getpixel((0, 0)) == 255


def test_animated_gif_uses_first_frame_and_tiff_pages_become_sources():
    frames = [Image.new("RGB", (20, 10), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=50)
    gif_sources = raster_sources(buf.getvalue(), origin="a.gif")
    assert len(gif_sources) == 1
    assert gif_sources[0].image().convert("RGB").getpixel((1, 1))[0] > 200

    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    tiff_sources = raster_sources(buf.getvalue(), origin="scan.tif")
    assert len(tiff_sources) == 3
    assert tiff_sources[2].image().getpixel((1, 1)) == (0, 0, 255)
    assert "3번째" in tiff_sources[2].label


@pytest.mark.parametrize("data,code", [
    (b"", "EMPTY_FILE"),
    (b"this is not an image at all", "UNSUPPORTED"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "CORRUPT"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 40, "CORRUPT"),
    (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40, "UNSUPPORTED"),
])
def test_unreadable_images_raise_typed_errors(data, code):
    with pytest.raises(SplitError) as err:
        RasterSource(data, label="x", origin="x")
    assert err.value.code == code


def test_truncated_jpeg_is_reported_as_corrupt_on_decode():
    data = _jpeg(gradient(200, 200), quality=90)
    src = RasterSource(data[: len(data) // 3], label="x.jpg", origin="x.jpg")
    with pytest.raises(SplitError) as err:
        src.image()
    assert err.value.code == "CORRUPT"


def test_huge_image_is_refused(monkeypatch):
    monkeypatch.setattr(imaging, "MAX_SOURCE_PIXELS", 5000)
    with pytest.raises(SplitError) as err:
        RasterSource(_png(Image.new("L", (100, 100))), label="x", origin="x")
    assert err.value.code == "IMAGE_TOO_LARGE"


def test_icc_profile_and_dpi_survive_encoding():
    fake_icc = b"\x00" * 128 + b"fake-profile"
    im = imaging.decode(_jpeg(gradient(40, 30), icc_profile=fake_icc, dpi=(300, 300)))
    assert im.info["icc_profile"] == fake_icc
    tile = imaging.render_tile(im, (0, 0, 20, 15), (40, 30))
    for blob in (imaging.encode_jpeg(tile), imaging.encode_png(tile)):
        back = Image.open(io.BytesIO(blob))
        assert back.info.get("icc_profile") == fake_icc
        assert round(back.info["dpi"][0]) == 300


# ---------------------------------------------------------------- splitting

@pytest.mark.parametrize("rows,cols", [(3, 3), (2, 3), (3, 2), (1, 2), (4, 4), (6, 3)])
def test_tiles_reassemble_into_the_original(rows, cols):
    w, h = 240, 180
    original = gradient(w, h)
    src = RasterSource(_png(original), label="g.png", origin="g.png")
    plan = make_plan(w, h, rows, cols)
    tiles = list(iter_tiles(src, plan))
    assert [i for i, _ in tiles] == list(range(rows * cols))
    assert all(t.size == (plan.out_w, plan.out_h) for _, t in tiles)
    # shrink each tile back and paste: the mosaic must match the original closely
    mosaic = Image.new("RGB", (w, h))
    for index, tile in tiles:
        x0, y0, x1, y1 = plan.boxes[index]
        small = tile.resize((round(x1 - x0), round(y1 - y0)), Image.BOX)
        mosaic.paste(small, (round(x0), round(y0)))
    diff = [abs(a - b) for a, b in zip(mosaic.tobytes(), original.tobytes())]
    assert sum(diff) / len(diff) < 1.0
    assert max(diff) <= 4


def test_plan_limits():
    with pytest.raises(SplitError) as err:
        make_plan(2, 2, 3, 3)
    assert err.value.code == "IMAGE_TOO_SMALL"
    plan = make_plan(8000, 6000, 3, 3, max_tile_pixels=12_000_000)
    assert plan.capped and plan.out_w * plan.out_h <= 12_000_000
    assert abs(plan.out_w / plan.out_h - 8000 / 6000) < 0.01
    assert not make_plan(4032, 3024, 3, 3).capped


def test_transparent_tiles_keep_alpha():
    im = Image.new("RGBA", (90, 90), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (0, 0, 45, 45))
    src = RasterSource(_png(im), label="t.png", origin="t.png")
    plan = make_plan(90, 90, 3, 3)
    tiles = dict(iter_tiles(src, plan))
    assert tiles[0].mode == "RGBA"
    assert tiles[0].getpixel((5, 5)) == (255, 0, 0, 255)
    assert tiles[8].getpixel((80, 80))[3] == 0
    assert imaging.flatten(tiles[8]).getpixel((80, 80)) == (255, 255, 255)


# ---------------------------------------------------------------- pages and names

def test_page_spec_original_and_a4():
    plan = make_plan(4000, 3000, 3, 3)
    spec = page_spec(plan, "original")
    assert (round(spec.page_w), round(spec.page_h)) == (280, 210)  # 4:3 fitted into A4 landscape
    assert (round(spec.w), round(spec.h), round(spec.x), round(spec.y)) == (280, 210, 0, 0)

    plan = make_plan(3000, 2000, 2, 3)  # tiles are square after enlargement
    spec = page_spec(plan, "original")
    assert round(spec.page_w) == 297 and round(spec.page_h) == 198
    assert round(spec.w) == round(spec.h) == 198 and round(spec.y) == 0 and spec.x > 40

    # two portrait halves of a landscape picture: the same sheet, turned by 90 degrees
    spec = page_spec(make_plan(4000, 3000, 1, 2), "original")
    assert (round(spec.page_w), round(spec.page_h)) == (210, 280)
    assert (round(spec.w), round(spec.h), round(spec.y)) == (187, 280, 0)
    # a strip across the picture fits the sheet as it lies
    spec = page_spec(make_plan(4000, 3000, 2, 1), "original")
    assert (round(spec.page_w), round(spec.page_h), round(spec.w), round(spec.h)) == (280, 210, 280, 105)

    a4 = page_spec(make_plan(4000, 3000, 3, 3), "a4")
    assert (a4.page_w, a4.page_h) == (297.0, 210.0) and a4.margin == 5.0
    assert a4.w <= 287.001 and a4.h <= 200.001
    assert page_spec(make_plan(3000, 4000, 3, 3), "a4").page_w == 210.0

    pdf_like = page_spec(make_plan(2480, 3508, 2, 2), "original", physical_mm=(210.0, 297.0))
    assert (pdf_like.page_w, pdf_like.page_h) == (210.0, 297.0)
    pdf_halves = page_spec(make_plan(2480, 3508, 2, 1), "original", physical_mm=(210.0, 297.0))
    assert (pdf_halves.page_w, pdf_halves.page_h) == (297.0, 210.0)  # A4 portrait cut in two: A4 landscape pages
    with pytest.raises(SplitError):
        page_spec(plan, "b5")


def test_names():
    assert safe_stem("C:\\Users\\me\\여름 사진.final.JPG") == "여름 사진.final"
    assert safe_stem('a<b>:c?"*|.png') == "a b c"
    assert safe_stem("...") == "image"
    assert safe_stem("CON.txt") == "image"
    assert len(safe_stem("가" * 200 + ".png")) == 60
    plan = make_plan(300, 300, 2, 3)
    stem = output_stem("포스터.pdf", plan, "3쪽")
    assert stem == "포스터_3쪽_6분할_3x2"
    assert tile_name(stem, 0, 6, "png") == "포스터_3쪽_6분할_3x2_01.png"
    assert tile_name("x", 99, 120, "jpg") == "x_100.jpg"


def test_damaged_picture_is_rejected_when_it_is_picked():
    from splitcore.session import Session
    data = _jpeg(gradient(300, 300), quality=90)
    session = Session()
    with pytest.raises(SplitError) as err:
        session.add_file("cut.jpg", data[: len(data) // 3])
    assert err.value.code == "CORRUPT"
    good = session.add_file("ok.jpg", data)["sources"][0]
    thumb, mime = session.thumbnail(good["id"], 120)
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(thumb)).size) == 120


def test_panoramas_and_long_strips_get_square_tiles():
    assert recommended_layout(4, 6000, 1200).key == "4x1"      # 5:1 panorama -> four side by side
    assert recommended_layout(4, 1200, 6000).key == "1x4"      # long screenshot -> four stacked
    assert recommended_layout(12, 3000, 1000).key == "6x2"
    assert recommended_layout(9, 4000, 1000).key == "9x1"
    assert recommended_layout(4, 2000, 1000).key == "2x2"      # 2:1 is still an ordinary picture
    assert recommended_layout(16, 2532, 1170).key == "4x4"
    options = layouts_for(18, 4000, 3000)
    assert [o["key"] for o in options if o["rare"]] == ["9x2", "2x9", "18x1", "1x18"]
    assert not options[0]["rare"]
    # on a panorama the grids that keep the picture's own shape are not "rare"
    assert [o["key"] for o in layouts_for(4, 6000, 1200) if o["rare"]] == ["1x4"]
