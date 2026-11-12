"""Hangul (.hwpx) export: one page per tile.

HWPX (KS X 6101 / OWPML) is the current standard format of Hancom Office
Hangul; it opens in Hangul 2014 VP and later and in Hancom Docs.  The legacy
binary .hwp format cannot be written reliably without Hangul itself, so it is
not offered.
"""
from __future__ import annotations

from ..errors import SplitError
from ..splitter import PageSpec, SplitPlan, iter_tiles
from .base import Progress, encode_for_document, looks_like_graphics, quality_option, report
from .hwpx_writer import HwpxWriterError, build_hwpx


def export_hwpx(source, plan: SplitPlan, spec: PageSpec, options: dict,
                progress: Progress = None, title: str = "") -> bytes:
    quality = quality_option(options)
    lossless = looks_like_graphics(source)
    pages = []
    for index, tile in iter_tiles(source, plan):
        image = encode_for_document(tile, quality, lossless)
        del tile
        pages.append({"data": image.data, "ext": image.ext, "width_px": image.width, "height_px": image.height,
                      "name": f"조각 {index + 1}"})
        report(progress, index + 1, plan.count)
    try:
        # the writer fits each picture inside the margins and centres it: the same placement as ``spec``
        return build_hwpx(pages, spec.page_w, spec.page_h, margin_mm=spec.margin, title=title or "split",
                          creator="조각조각")
    except HwpxWriterError as exc:
        raise SplitError("INTERNAL", detail=f"hwpx: {exc}") from None
