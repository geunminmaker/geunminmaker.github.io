"""Which grids can cut an image into N equal tiles, and which one to suggest.

Convention (see PRD "분할 규칙"):

* Tiles are numbered row by row, left to right, top to bottom.
* The suggested grid is the most balanced factor pair of N (9 -> 3x3,
  16 -> 4x4).  When N is not a square number the two mirrored grids are equally
  balanced, so the image orientation decides: a landscape image gets more
  columns (6 -> 2 rows x 3 columns), a portrait image gets more rows
  (6 -> 3 rows x 2 columns).  A square image is treated like landscape.
* A panorama or long screenshot (long side at least 3x the short side) gets
  the grid whose tiles are closest to square instead (a 5:1 panorama cut in 4
  becomes four side-by-side tiles, not 2x2).
* Every other factor pair stays available as an alternative, ordered by how
  close to square its tiles are; very thin strips come last, marked "rare".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import SplitError

#: split counts offered by the app
SPLIT_COUNTS = (2, 4, 6, 8, 9, 12, 16, 18)

#: hard technical bound for custom requests through the CLI / API
MAX_TILES = 64


@dataclass(frozen=True)
class Layout:
    rows: int
    cols: int

    @property
    def count(self) -> int:
        return self.rows * self.cols

    @property
    def key(self) -> str:
        """Stable identifier, columns first the way people say it ("3x2" = 가로 3 x 세로 2)."""
        return f"{self.cols}x{self.rows}"


def parse_layout(text: str) -> Layout:
    """Parse "3x2" (columns x rows) into a Layout."""
    try:
        cols_s, rows_s = str(text).lower().replace("×", "x").replace("*", "x").split("x")
        layout = Layout(int(rows_s), int(cols_s))
    except (ValueError, TypeError):
        raise SplitError("BAD_LAYOUT", detail=f"layout={text!r}") from None
    if layout.rows < 1 or layout.cols < 1 or layout.count > MAX_TILES:
        raise SplitError("BAD_LAYOUT", detail=f"layout={text!r}")
    return layout


def factor_pairs(n: int) -> list[Layout]:
    """All (rows, cols) with rows * cols == n."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > MAX_TILES:
        raise SplitError("BAD_LAYOUT", detail=f"n={n!r}")
    return [Layout(r, n // r) for r in range(1, n + 1) if n % r == 0]


def _balance(layout: Layout) -> float:
    return max(layout.rows, layout.cols) / min(layout.rows, layout.cols)


def _tile_aspect(layout: Layout, width: float, height: float) -> float:
    return (width / layout.cols) / (height / layout.rows)


def _elongation(aspect: float) -> float:
    """Long side / short side (always >= 1)."""
    return aspect if aspect >= 1.0 else 1.0 / aspect


#: An image whose long side is at least this many times its short side is a
#: panorama / long screenshot: there the grid with the squarest tiles is suggested.
LONG_RATIO = 3.0
#: Tiles more elongated than this are unusual choices (shown last, marked "rare").
RARE_TILE_RATIO = 3.0


def recommended_layout(n: int, width: float, height: float) -> Layout:
    """The grid most people expect for an image of this shape."""
    if width <= 0 or height <= 0:
        raise SplitError("IMAGE_TOO_SMALL", detail=f"{width}x{height}")
    wide = width >= height  # square counts as landscape
    pairs = factor_pairs(n)

    def oriented(layout: Layout) -> int:
        return 0 if ((layout.cols >= layout.rows) if wide else (layout.rows >= layout.cols)) else 1

    if max(width, height) >= LONG_RATIO * min(width, height):
        # Panorama or long strip: cut it so that the tiles come out as square as possible.
        return min(pairs, key=lambda l: (round(abs(math.log(_tile_aspect(l, width, height))), 9),
                                         round(_balance(l), 9), oriented(l)))
    return min(pairs, key=lambda l: (round(_balance(l), 9), oriented(l)))


def scale_factor(layout: Layout) -> int:
    """How many times a tile is enlarged so it fits the original size.

    A tile is ``W/cols x H/rows``.  Growing it with its aspect ratio kept until
    it touches the original ``W x H`` box means multiplying by ``min(rows, cols)``.
    """
    return min(layout.rows, layout.cols)


def output_size(layout: Layout, width: int, height: int) -> tuple[int, int]:
    """Pixel size of every enlarged tile (aspect ratio preserved)."""
    s = scale_factor(layout)
    return (max(1, round(width * s / layout.cols)), max(1, round(height * s / layout.rows)))


def is_usable(layout: Layout, width: int, height: int) -> bool:
    """A grid needs at least one source pixel per tile in each direction."""
    return width >= layout.cols and height >= layout.rows


def layouts_for(n: int, width: int, height: int) -> list[dict]:
    """Every grid for ``n`` tiles, the suggested one first.

    Each entry is JSON-friendly::

        {"rows": 2, "cols": 3, "key": "3x2", "recommended": True, "usable": True,
         "tile_w": 1333.3, "tile_h": 1500.0, "out_w": 2667, "out_h": 3000,
         "fills_original": False, "scale": 2}
    """
    best = recommended_layout(n, width, height)

    def squareness(layout: Layout) -> float:
        return abs(math.log(_tile_aspect(layout, width, height)))

    others = sorted(
        (l for l in factor_pairs(n) if l != best),
        key=lambda l: (round(squareness(l), 9), round(_balance(l), 9), l.rows),
    )
    result = []
    for layout in [best] + others:
        out_w, out_h = output_size(layout, width, height)
        result.append(
            {
                "rows": layout.rows,
                "cols": layout.cols,
                "key": layout.key,
                "count": layout.count,
                "recommended": layout == best,
                "usable": is_usable(layout, width, height),
                "tile_w": width / layout.cols,
                "tile_h": height / layout.rows,
                "out_w": out_w,
                "out_h": out_h,
                "scale": scale_factor(layout),
                # True when the enlarged tile is exactly the original size (rows == cols)
                "fills_original": layout.rows == layout.cols,
                # very long, thin tiles: offered, but not what people usually want
                "rare": layout != best and _elongation(_tile_aspect(layout, width, height))
                > max(RARE_TILE_RATIO, _elongation(width / height) * 1.001),
            }
        )
    return result


def tile_boxes(layout: Layout, width: float, height: float) -> list[tuple[float, float, float, float]]:
    """Source rectangles (x0, y0, x1, y1) in reading order.

    Boundaries are exact fractions of the image, so the tiles always cover the
    whole picture with no gap, overlap or dropped remainder even when the
    pixel size is not divisible by the grid.
    """
    if layout.rows < 1 or layout.cols < 1:
        raise SplitError("BAD_LAYOUT", detail=f"{layout}")
    boxes = []
    for r in range(layout.rows):
        y0 = height * r / layout.rows
        y1 = height * (r + 1) / layout.rows
        for c in range(layout.cols):
            x0 = width * c / layout.cols
            x1 = width * (c + 1) / layout.cols
            boxes.append((x0, y0, x1, y1))
    return boxes
