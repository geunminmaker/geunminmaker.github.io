"""The whole app, start to finish, with many different inputs.

Every case picks a file in the running app, answers the two yes/no questions,
chooses a split and a format, saves, and then checks the received files with
the independent verifier (tiles reassemble into the original, page sizes,
document structure).  Each case runs in both phone profiles.
"""
from __future__ import annotations

import os

import pytest

from conftest import CORPUS, ROOT
from verify import verify_outputs

HANGUL = os.path.join(ROOT, "tests", "fixtures", "hangul")


def corpus(name: str) -> str:
    return os.path.join(CORPUS, name)


# id, file, candidate index (None = single picture), count, layout to tap (None = keep the suggestion),
# expected layout key (cols x rows), format, paper, password
FLOWS = [
    ("photo-9-pdf", corpus("01_사진_가로_4032x3024.jpg"), None, 9, None, "3x3", "pdf", "original", None),
    ("exif-portrait-6-jpg", corpus("02_사진_세로_EXIF회전.jpg"), None, 6, None, "2x3", "jpg", None, None),
    ("transparent-4-png", corpus("04_투명배경_로고_1200x800.png"), None, 4, None, "2x2", "png", None, None),
    ("screenshot-18-word-a4", corpus("05_휴대폰_캡처_1170x2532.png"), None, 18, None, "3x6", "docx", "a4", None),
    ("panorama-4-pptx", corpus("06_파노라마_6000x1200.jpg"), None, 4, None, "4x1", "pptx", "original", None),
    ("cmyk-12-pdf-a4", corpus("08_CMYK_1800x1200.jpg"), None, 12, None, "4x3", "pdf", "a4", None),
    ("vector-pdf-9-pdf", corpus("20_벡터_포스터_A4.pdf"), None, 9, None, "3x3", "pdf", "original", None),
    ("pdf-page3-6-alt-hwpx", corpus("21_여러쪽_5쪽_크기제각각.pdf"), 2, 6, "2x3", "2x3", "hwpx", "original", None),
    ("locked-pdf-2-pdf", corpus("23_암호_1234.pdf"), None, 2, None, "1x2", "pdf", "a4", "1234"),
    ("docx-second-4-word", corpus("30_워드_그림2장.docx"), 1, 4, None, "2x2", "docx", "original", None),
    ("pptx-third-8-pptx", corpus("31_파워포인트_그림3장.pptx"), 2, 8, None, "4x2", "pptx", "original", None),
    ("tiff-second-2-png", corpus("10_여러장_스캔.tiff"), 1, 2, "2x1", "2x1", "png", None, None),
    ("zip-second-16-jpg", corpus("40_사진묶음.zip"), 1, 16, None, "4x4", "jpg", None, None),
    ("legacy-ppt-9-hwpx-a4", corpus("63_구형파워포인트.ppt"), 0, 9, None, "3x3", "hwpx", "a4", None),
    ("hwp-second-2-pdf", os.path.join(HANGUL, "hwplib_changing-image.hwp"), 1, 2, None, "2x1", "pdf", "original", None),
    ("real-word-9-pdf", corpus("60_실제워드.docx"), 0, 9, None, "3x3", "pdf", "original", None),
]


@pytest.mark.parametrize("case", FLOWS, ids=[f[0] for f in FLOWS])
def test_full_flow(app, profile, case):
    _id, path, pick, count, tap_layout, expected_layout, fmt, paper, password = case
    app.open()
    app.shot("home")
    assert app.no_horizontal_overflow()

    screen = app.pick_files([path])
    if password:
        assert screen == "error" and app.error_code() == "ENCRYPTED", (screen, app.error_code())
        app.shot("password")
        screen = app.unlock("0000")  # wrong on purpose
        assert screen == "error" and app.error_code() in ("WRONG_PASSWORD", "ENCRYPTED")
        screen = app.unlock(password)
    if pick is None:
        assert screen == "confirm", (screen, app.error_code())
    else:
        assert screen == "pick", (screen, app.error_code())
        assert app.source_count() > pick
        app.shot("pick")
        app.choose_source(pick)

    assert app.confirm(yes=True) == "split"
    selected = app.choose_split(count, tap_layout)
    assert selected == expected_layout, f"layout {selected}, expected {expected_layout}; shown {app.layouts_shown()}"
    assert app.no_horizontal_overflow()
    app.split_yes()
    app.choose_output(fmt, paper)
    files = app.save()

    cols, rows = (int(v) for v in expected_layout.split("x"))
    zipped = profile == "android" and fmt in ("jpg", "png")
    problems = verify_outputs(path, files, rows=rows, cols=cols, fmt=fmt, paper=paper or "original",
                              pick=pick or 0, password=password, zipped=zipped)
    assert not problems, problems
    # keep the last result of every case for manual inspection (Office / Hangul checks read them)
    out_dir = os.path.join(ROOT, "tests", "e2e", "out", "files", profile, _id)
    os.makedirs(out_dir, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(data)


def test_second_format_from_the_same_split(app, profile):
    """After saving once, another format can be saved without starting over."""
    path = corpus("03_정사각형_2000.png")
    app.open()
    assert app.pick_files([path]) == "confirm"
    app.confirm(True)
    assert app.choose_split(4) == "2x2"
    app.split_yes()
    app.choose_output("pdf")
    first = app.save()
    assert not verify_outputs(path, first, rows=2, cols=2, fmt="pdf")
    app.choose_output("pptx", "a4")
    second = app.save()
    assert not verify_outputs(path, second, rows=2, cols=2, fmt="pptx", paper="a4")


def test_saying_no_goes_back(app, profile):
    """아니요 on the confirm screen returns to the candidates; 아니요 on the split screen clears the choice."""
    app.open()
    assert app.pick_files([corpus("21_여러쪽_5쪽_크기제각각.pdf")]) == "pick"
    app.choose_source(0)
    assert app.confirm(yes=False) == "pick"
    app.choose_source(4)
    assert app.confirm(yes=True) == "split"
    app.choose_split(12)
    app.click('[data-action="split-no"]')
    app.page.wait_for_timeout(300)
    assert app.screen == "split"
    assert app.page.locator('[data-count][aria-checked="true"], [data-count][aria-pressed="true"]').count() == 0
    assert app.choose_split(4) == "2x2"
    app.split_yes()


def test_several_files_at_once_with_one_broken(app, profile):
    good, bad, other = corpus("11_웹피_1600x900.webp"), corpus("50_잘린_사진.jpg"), corpus("12_비트맵_800x600.bmp")
    app.open()
    assert app.pick_files([good, bad, other]) == "pick"
    assert app.source_count() == 2
    assert "50_잘린_사진" in app.text() or "열 수 없" in app.text()
    app.shot("pick-with-notice")
    app.choose_source(1)
    app.confirm(True)
    assert app.choose_split(9) == "3x3"
    app.split_yes()
    app.choose_output("png")
    files = app.save()
    problems = verify_outputs(other, files, rows=3, cols=3, fmt="png", zipped=profile == "android")
    assert not problems, problems


@pytest.mark.parametrize("name,code", [
    ("50_잘린_사진.jpg", "CORRUPT"),
    ("51_이름만_사진.png", "UNSUPPORTED"),
    ("52_빈파일.pdf", "EMPTY_FILE"),
    ("53_너무큰_해상도.png", "IMAGE_TOO_LARGE"),
    ("32_글자만_있는_워드.docx", "NO_IMAGE"),
])
def test_unusable_files_explain_themselves(app, profile, name, code):
    app.open()
    assert app.pick_files([corpus(name)]) == "error"
    assert app.error_code() == code
    app.shot(f"error-{code}")
    assert app.no_horizontal_overflow()
    # the recovery action leads somewhere useful: pick a good file right away
    assert app.pick_files([corpus("12_비트맵_800x600.bmp")]) == "confirm"


def test_tiny_picture_disables_impossible_layouts(app, profile, tmp_path):
    """A 12 x 10 px picture cannot be cut into 18 columns or 18 rows; the other grids still work."""
    from PIL import Image

    path = str(tmp_path / "아주작은그림.png")
    tiny = Image.new("RGB", (12, 10))
    tiny.putdata([(x * 20, y * 25, (x + y) * 11) for y in range(10) for x in range(12)])
    tiny.save(path)

    app.open()
    assert app.pick_files([path]) == "confirm"
    app.confirm(True)
    app.choose_split(18)
    disabled = app.page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-layout]')).filter((el) => el.disabled || el.getAttribute('aria-disabled') === 'true').map((el) => el.dataset.layout)"
    )
    assert sorted(disabled) == ["18x1", "1x18"], disabled
    assert app.choose_split(18, "6x3") == "6x3"
    app.split_yes()
    app.choose_output("png")
    files = app.save()
    problems = verify_outputs(path, files, rows=3, cols=6, fmt="png", zipped=profile == "android")
    assert not problems, problems
