"""Shared pieces of the exporters."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from .. import imaging
from ..errors import SplitError

#: progress callback: (done, total) with total == number of tiles
Progress = Optional[Callable[[int, int], None]]

JPEG_QUALITY = 92
#: None = chosen from the tile size (see imaging.encode_png)
PNG_LEVEL = None


@dataclass
class OutputFile:
    name: str
    mime: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class DocImage:
    """A tile encoded for embedding in a document."""

    data: bytes
    ext: str  # "jpg" or "png"
    mime: str
    width: int
    height: int


def encode_for_document(tile: Image.Image, quality: int = JPEG_QUALITY, lossless: bool = False) -> DocImage:
    """JPEG for photos (small, universally supported); PNG for transparency and flat graphics."""
    if tile.mode == "RGBA" or lossless:
        return DocImage(imaging.encode_png(tile, PNG_LEVEL), "png", "image/png", *tile.size)
    return DocImage(imaging.encode_jpeg(tile, quality), "jpg", "image/jpeg", *tile.size)


def looks_like_graphics(source) -> bool:
    """True for screenshots, diagrams, text pages and other flat art that deserves lossless storage.

    Photos stored in a lossless container (a PNG photo) still go through JPEG,
    otherwise every enlarged tile would weigh tens of megabytes.
    """
    if not getattr(source, "lossless", False):
        return False
    try:
        thumb = source.thumbnail(384)
        if thumb.mode == "RGBA":
            thumb = imaging.flatten(thumb)
        buf = io.BytesIO()
        thumb.save(buf, "PNG", compress_level=6)
        return buf.tell() / float(thumb.size[0] * thumb.size[1]) < 0.75
    except SplitError:
        raise
    except Exception:
        return False


def quality_option(options: dict) -> int:
    try:
        return max(50, min(100, int(options.get("quality") or JPEG_QUALITY)))
    except (TypeError, ValueError):
        return JPEG_QUALITY


def report(progress: Progress, done: int, total: int) -> None:
    if progress is not None:
        progress(done, total)
