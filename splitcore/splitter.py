"""Turning a source + grid into enlarged tiles, and placing tiles on pages."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator

from PIL import Image

from . import imaging
from .errors import SplitError
from .grids import Layout, is_usable, output_size, scale_factor, tile_boxes

A4_MM = (210.0, 297.0)
#: default margin when a tile is placed on an A4 sheet (inside what printers can reach)
A4_MARGIN_MM = 5.0
MM_PER_INCH = 25.4


@dataclass
class SplitPlan:
    """Everything needed to produce the tiles of one split."""

    layout: Layout
    src_w: int
    src_h: int
    out_w: int
    out_h: int
    #: True when the tile had to be made smaller than "original size" to stay within limits
    capped: bool
    boxes: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.layout.count

    @property
    def fills_original(self) -> bool:
        return (self.out_w, self.out_h) == (self.src_w, self.src_h)

    def to_dict(self) -> dict:
        return {
            "rows": self.layout.rows,
            "cols": self.layout.cols,
            "key": self.layout.key,
            "count": self.count,
            "src_w": self.src_w,
            "src_h": self.src_h,
            "out_w": self.out_w,
            "out_h": self.out_h,
            "capped": self.capped,
            "fills_original": self.fills_original,
            "scale": scale_factor(self.layout),
        }


def make_plan(src_w: int, src_h: int, rows: int, cols: int,
              max_tile_pixels: int = imaging.MAX_TILE_PIXELS) -> SplitPlan:
    """Validate the grid for this picture and work out the enlarged tile size."""
    try:
        layout = Layout(int(rows), int(cols))
    except (TypeError, ValueError):
        raise SplitError("BAD_LAYOUT", detail=f"{rows}x{cols}") from None
    if layout.rows < 1 or layout.cols < 1:
        raise SplitError("BAD_LAYOUT", detail=f"{rows}x{cols}")
    if not is_usable(layout, src_w, src_h):
        raise SplitError("IMAGE_TOO_SMALL", detail=f"{src_w}x{src_h} into {layout.key}")
    out_w, out_h = output_size(layout, src_w, src_h)
    capped = False
    if out_w * out_h > max_tile_pixels:
        shrink = math.sqrt(max_tile_pixels / float(out_w * out_h))
        out_w = max(1, int(out_w * shrink))
        out_h = max(1, int(out_h * shrink))
        capped = True
    return SplitPlan(layout, int(src_w), int(src_h), out_w, out_h, capped,
                     tile_boxes(layout, src_w, src_h))


def iter_tiles(source, plan: SplitPlan) -> Iterator[tuple[int, Image.Image]]:
    """Yield (index, tile image) in reading order, one tile in memory at a time."""
    for index, box in enumerate(plan.boxes):
        yield index, source.render_tile(box, (plan.out_w, plan.out_h))


# ---------------------------------------------------------------------------
# Pages (PDF / Word / PowerPoint / Hangul)
# ---------------------------------------------------------------------------

@dataclass
class PageSpec:
    """Where one tile sits on a page, in millimetres."""

    page_w: float
    page_h: float
    x: float
    y: float
    w: float
    h: float
    paper: str = "original"
    margin: float = 0.0

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _fit(inner_w: float, inner_h: float, box_w: float, box_h: float) -> tuple[float, float]:
    scale = min(box_w / inner_w, box_h / inner_h)
    return inner_w * scale, inner_h * scale


def page_spec(plan: SplitPlan, paper: str = "original", margin_mm: float | None = None,
              physical_mm: tuple[float, float] | None = None,
              min_side_mm: float = 30.0, max_side_mm: float = 550.0) -> PageSpec:
    """Page size and picture placement for document exports.

    ``paper="original"``
        The page has the proportions of the original picture, so a tile from a
        square grid (3x3, 4x4 ...) covers the page completely and a tile from
        any other grid is centred on it, as large as possible.  The page uses
        the source's real size when it has one (PDF pages); otherwise the
        proportions are fitted into an A4 sheet, so printing at 100 % works.

    ``paper="a4"``
        A real A4 sheet, turned to match the tile, with a small printer-safe
        margin.
    """
    paper = (paper or "original").lower()
    if paper == "a4":
        margin = A4_MARGIN_MM if margin_mm is None else max(0.0, float(margin_mm))
        short, long_ = A4_MM
        page_w, page_h = (long_, short) if plan.out_w > plan.out_h else (short, long_)
    elif paper == "original":
        margin = 0.0 if margin_mm is None else max(0.0, float(margin_mm))
        usable = (physical_mm is not None
                  and min(physical_mm) >= min_side_mm and max(physical_mm) <= max_side_mm)
        if usable:
            page_w, page_h = float(physical_mm[0]), float(physical_mm[1])
        else:
            short, long_ = A4_MM
            box = (long_, short) if plan.src_w > plan.src_h else (short, long_)
            page_w, page_h = _fit(plan.src_w, plan.src_h, *box)
            # extremely long strips: keep the short side printable
            if min(page_w, page_h) < min_side_mm:
                grow = min_side_mm / min(page_w, page_h)
                page_w, page_h = page_w * grow, page_h * grow
                if max(page_w, page_h) > max_side_mm:
                    shrink = max_side_mm / max(page_w, page_h)
                    page_w, page_h = page_w * shrink, page_h * shrink
    else:
        raise SplitError("BAD_FORMAT", detail=f"paper={paper!r}")
    margin = min(margin, min(page_w, page_h) / 4.0)
    area_w, area_h = page_w - 2 * margin, page_h - 2 * margin
    w, h = _fit(plan.out_w, plan.out_h, area_w, area_h)
    return PageSpec(page_w, page_h, (page_w - w) / 2.0, (page_h - h) / 2.0, w, h, paper, margin)


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_stem(name: str, fallback: str = "image", max_len: int = 60) -> str:
    """A file-system-safe base name (no extension) derived from the user's file name."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name.strip("."):
        name = name.rsplit(".", 1)[0]
    name = _BAD_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    if not name or name.upper() in _RESERVED:
        name = fallback
    return name


def output_stem(origin: str, plan: SplitPlan, label: str | None = None) -> str:
    """ "사진_9분할_3x3" style base name shared by all files of one export."""
    stem = safe_stem(origin)
    extra = ""
    if label and label != origin:
        cleaned = safe_stem(label, fallback="", max_len=20)
        if cleaned and cleaned not in stem:
            extra = f"_{cleaned}"
    return f"{stem}{extra}_{plan.count}분할_{plan.layout.key}"


def tile_name(stem: str, index: int, count: int, ext: str) -> str:
    digits = max(2, len(str(count)))
    return f"{stem}_{index + 1:0{digits}d}.{ext}"
