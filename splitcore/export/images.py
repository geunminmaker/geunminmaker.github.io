"""JPG / PNG export: one picture file per tile."""
from __future__ import annotations

from typing import Iterator

from .. import imaging
from ..splitter import SplitPlan, iter_tiles, tile_name
from .base import PNG_LEVEL, OutputFile, Progress, quality_option, report


def export_images(source, plan: SplitPlan, fmt: str, stem: str, options: dict,
                  progress: Progress = None) -> Iterator[OutputFile]:
    """Yield the tiles one at a time so only one is held in memory."""
    jpeg = fmt == "jpg"
    quality = quality_option(options)
    for index, tile in iter_tiles(source, plan):
        if jpeg:
            data = imaging.encode_jpeg(tile, quality)
            name, mime = tile_name(stem, index, plan.count, "jpg"), "image/jpeg"
        else:
            data = imaging.encode_png(tile, PNG_LEVEL)
            name, mime = tile_name(stem, index, plan.count, "png"), "image/png"
        del tile
        yield OutputFile(name, mime, data)
        report(progress, index + 1, plan.count)
