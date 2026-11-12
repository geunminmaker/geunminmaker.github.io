"""Draw the app icon set (four tiles of a split picture on the brand colour).

    python tools/make_icons.py

Writes web/icons/*.png, web/icons/favicon.svg and the Android launcher layers.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "icons")

BRAND = (31, 107, 240)        # #1F6BF0
WHITE = (255, 255, 255)
SS = 4                        # supersampling factor for smooth edges


def tiles(draw: ImageDraw.ImageDraw, size: int, extent: float, colour, soft, radius_ratio=0.22, gap_ratio=0.085):
    """Four rounded squares centred on the canvas.  ``extent`` = glyph width / canvas width."""
    glyph = size * extent
    gap = glyph * gap_ratio
    cell = (glyph - gap) / 2
    radius = cell * radius_ratio
    origin = (size - glyph) / 2
    for row in range(2):
        for col in range(2):
            x0 = origin + col * (cell + gap)
            y0 = origin + row * (cell + gap)
            fill = soft if (row, col) == (0, 1) else colour
            draw.rounded_rectangle((x0, y0, x0 + cell, y0 + cell), radius=radius, fill=fill)


def render(size: int, *, background, extent: float, corner: float = 0.0, transparent_outside=False) -> Image.Image:
    big = size * SS
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if background is not None:
        if corner:
            draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * corner, fill=background + (255,))
        else:
            draw.rectangle((0, 0, big, big), fill=background + (255,))
        soft = tuple(round(background[i] + (255 - background[i]) * 0.55) for i in range(3)) + (255,)
        tiles(draw, big, extent, WHITE + (255,), soft)
    else:  # monochrome glyph (Android themed icon / notification)
        tiles(draw, big, extent, WHITE + (255,), WHITE + (140,))
    out = image.resize((size, size), Image.LANCZOS)
    if not transparent_outside and background is not None and not corner:
        out = out.convert("RGB")
    return out


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    # "any" icons: rounded square with transparent corners
    for size in (192, 512):
        render(size, background=BRAND, extent=0.52, corner=0.225, transparent_outside=True).save(
            os.path.join(OUT, f"icon-{size}.png"), optimize=True)
    # maskable: full bleed, glyph inside the 80 % safe zone
    for size in (192, 512):
        render(size, background=BRAND, extent=0.44).save(os.path.join(OUT, f"icon-maskable-{size}.png"), optimize=True)
    # iOS home screen (opaque square, iOS rounds it)
    render(180, background=BRAND, extent=0.52).save(os.path.join(OUT, "apple-touch-icon.png"), optimize=True)
    render(32, background=BRAND, extent=0.56, corner=0.22, transparent_outside=True).save(
        os.path.join(OUT, "favicon-32.png"), optimize=True)
    render(1024, background=BRAND, extent=0.52).save(os.path.join(OUT, "icon-1024.png"), optimize=True)

    # vector favicon
    glyph, gap = 52.0, 4.4
    cell = (glyph - gap) / 2
    origin = (100 - glyph) / 2
    rects = []
    for row in range(2):
        for col in range(2):
            opacity = ' fill-opacity=".55"' if (row, col) == (0, 1) else ""
            rects.append(f'<rect x="{origin + col * (cell + gap):.2f}" y="{origin + row * (cell + gap):.2f}" '
                         f'width="{cell:.2f}" height="{cell:.2f}" rx="{cell * 0.22:.2f}" fill="#fff"{opacity}/>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<rect width="100" height="100" rx="22.5" fill="#1F6BF0"/>' + "".join(rects) + "</svg>\n")
    with open(os.path.join(OUT, "favicon.svg"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)

    # Android adaptive icon layers (108 dp canvas, glyph within the 66 dp safe zone) at xxxhdpi = 432 px
    android = os.path.join(ROOT, "android", "icon-src")
    os.makedirs(android, exist_ok=True)
    fg = Image.new("RGBA", (432 * SS, 432 * SS), (0, 0, 0, 0))
    tiles(ImageDraw.Draw(fg), 432 * SS, 0.33, WHITE + (255,), tuple(round(BRAND[i] + (255 - BRAND[i]) * 0.55) for i in range(3)) + (255,))
    fg.resize((432, 432), Image.LANCZOS).save(os.path.join(android, "ic_launcher_foreground.png"), optimize=True)
    mono = Image.new("RGBA", (432 * SS, 432 * SS), (0, 0, 0, 0))
    tiles(ImageDraw.Draw(mono), 432 * SS, 0.33, WHITE + (255,), WHITE + (140,))
    mono.resize((432, 432), Image.LANCZOS).save(os.path.join(android, "ic_launcher_monochrome.png"), optimize=True)
    render(512, background=BRAND, extent=0.52).save(os.path.join(android, "play_store_512.png"), optimize=True)
    print("icons ->", OUT)


if __name__ == "__main__":
    main()
