"""Regenerate every launcher and splash bitmap of the Jogakjogak Android (TWA) wrapper.

Usage (any working directory):

    python android/tools/make_android_assets.py           # rewrite the PNG files
    python android/tools/make_android_assets.py --check   # exit 1 if a file is missing or out of date

Inputs (the only art that is edited by hand):

    android/icon-src/ic_launcher_foreground.png   432 px adaptive-icon foreground layer (white tiles, transparent)
    android/icon-src/ic_launcher_monochrome.png   432 px adaptive-icon monochrome layer (Android 13+ themed icons)
    web/icons/icon-512.png                        rounded blue square with the four white tiles
    web/icons/icon-1024.png                       the same art, full-bleed square

Outputs, all below android/app/src/main/res/ (density factors mdpi 1, hdpi 1.5, xhdpi 2, xxhdpi 3, xxxhdpi 4):

    mipmap-<dpi>/ic_launcher_foreground.png   108 dp  -> 108 / 162 / 216 / 324 / 432 px
    mipmap-<dpi>/ic_launcher_monochrome.png   108 dp  -> 108 / 162 / 216 / 324 / 432 px
    mipmap-<dpi>/ic_launcher.png               48 dp  ->  48 /  72 /  96 / 144 / 192 px  (Android 7.x only)
    mipmap-<dpi>/ic_launcher_round.png         48 dp  ->  48 /  72 /  96 / 144 / 192 px  (Android 7.1 only)
    drawable-<dpi>/splash.png                 128 dp  -> 128 / 192 / 256 / 384 / 512 px

The adaptive-icon background is not a bitmap: it is the colour ic_launcher_background (#1F6BF0) in
res/values/colors.xml, and the layers are wired together in res/mipmap-anydpi-v26/ic_launcher*.xml.
The splash background colours (white, #101214 at night) live in res/values*/colors.xml as well.

Needs only Pillow. The output is deterministic, so running the script twice gives identical files.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ANDROID_DIR = Path(__file__).resolve().parent.parent
ICON_SRC = ANDROID_DIR / "icon-src"
WEB_ICONS = ANDROID_DIR.parent / "web" / "icons"
RES = ANDROID_DIR / "app" / "src" / "main" / "res"

DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}

ADAPTIVE_DP = 108          # full adaptive-icon layer (the launcher shows the middle 72 dp)
LEGACY_DP = 48             # legacy launcher icon canvas
LEGACY_SQUARE_MARGIN_DP = 3  # transparent margin around the rounded square (42 dp art)
LEGACY_ROUND_MARGIN_DP = 2   # transparent margin around the circle (44 dp art)
SPLASH_DP = 128            # splash icon; 128 dp keeps xxxhdpi at the 512 px source size (no upscaling)
SUPERSAMPLE = 4            # the circle mask is drawn this many times larger, then scaled down


def load(path: Path, size: int) -> Image.Image:
    if not path.is_file():
        sys.exit(f"missing input: {path}")
    img = Image.open(path).convert("RGBA")
    if img.size != (size, size):
        sys.exit(f"{path} is {img.size[0]}x{img.size[1]}, expected {size}x{size}")
    return img


def scaled(img: Image.Image, px: int) -> Image.Image:
    """High-quality resize; Pillow premultiplies alpha for RGBA, so edges get no dark fringe."""
    if img.size == (px, px):
        return img.copy()
    return img.resize((px, px), Image.LANCZOS)


def with_margin(img: Image.Image, canvas_px: int, margin_px: int) -> Image.Image:
    canvas = Image.new("RGBA", (canvas_px, canvas_px), (0, 0, 0, 0))
    art = scaled(img, canvas_px - 2 * margin_px)
    canvas.alpha_composite(art, (margin_px, margin_px))
    return canvas


def circle_masked(img: Image.Image) -> Image.Image:
    side = img.size[0]
    big = side * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    out = img.copy()
    out.putalpha(mask.resize((side, side), Image.LANCZOS))
    return out


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build() -> dict[Path, bytes]:
    foreground = load(ICON_SRC / "ic_launcher_foreground.png", 432)
    monochrome = load(ICON_SRC / "ic_launcher_monochrome.png", 432)
    rounded = load(WEB_ICONS / "icon-512.png", 512)
    circle = circle_masked(load(WEB_ICONS / "icon-1024.png", 1024))

    files: dict[Path, bytes] = {}
    for name, factor in DENSITIES.items():
        mipmap = RES / f"mipmap-{name}"
        adaptive_px = round(ADAPTIVE_DP * factor)
        legacy_px = round(LEGACY_DP * factor)
        files[mipmap / "ic_launcher_foreground.png"] = png_bytes(scaled(foreground, adaptive_px))
        files[mipmap / "ic_launcher_monochrome.png"] = png_bytes(scaled(monochrome, adaptive_px))
        files[mipmap / "ic_launcher.png"] = png_bytes(
            with_margin(rounded, legacy_px, round(LEGACY_SQUARE_MARGIN_DP * factor)))
        files[mipmap / "ic_launcher_round.png"] = png_bytes(
            with_margin(circle, legacy_px, round(LEGACY_ROUND_MARGIN_DP * factor)))
        files[RES / f"drawable-{name}" / "splash.png"] = png_bytes(scaled(rounded, round(SPLASH_DP * factor)))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if any generated file is missing or differs")
    args = parser.parse_args()

    stale = 0
    for path, data in build().items():
        rel = path.relative_to(ANDROID_DIR).as_posix()
        current = path.read_bytes() if path.is_file() else None
        size = Image.open(io.BytesIO(data)).size
        if current == data:
            state = "ok"
        elif args.check:
            state = "MISSING" if current is None else "OUT OF DATE"
            stale += 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            state = "written"
        print(f"{state:11s} {size[0]:4d}x{size[1]:<4d} {len(data):6d} B  {rel}")
    if stale:
        print(f"{stale} file(s) need regenerating: run this script without --check")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
