"""Second-chance decoder for pictures that this build of Pillow cannot read.

The Pillow that runs in the browser lacks a few codecs (deflate- or
JPEG-compressed TIFF scans, JPEG 2000).  MuPDF, which the app already ships for
PDF, can decode them.  The picture is converted once to PNG, after which it is
handled like any other picture.
"""
from __future__ import annotations

import io

from PIL import Image

from .. import imaging
from ..errors import SplitError
from ..sources import RasterSource, Source

#: formats worth a second try
FALLBACK_KINDS = ("tiff", "jp2")


def _to_image(pix, pymupdf) -> Image.Image:
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # gray / CMYK -> RGB (alpha is kept)
    mode = "RGBA" if pix.alpha else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _as_source(image: Image.Image, label: str, origin: str) -> Source:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=1)
    source = RasterSource(buffer.getvalue(), label=label, origin=origin)
    source.lossless = True
    return source


def load_with_mupdf(data: bytes, origin: str) -> list[Source]:
    """Decode with MuPDF.  Raises NEED_PACKAGE when PyMuPDF is not loaded yet."""
    try:
        import pymupdf
    except ImportError:
        raise SplitError("NEED_PACKAGE", detail="pymupdf") from None

    kind = imaging.sniff_image(data)
    try:
        if kind == "tiff":
            # native pixel size of every frame comes from the TIFF header (Pillow can read headers)
            sizes = []
            try:
                probe = Image.open(io.BytesIO(data))
                for index in range(min(int(getattr(probe, "n_frames", 1)), imaging.MAX_FRAMES)):
                    probe.seek(index)
                    sizes.append(probe.size)
            except Exception:
                sizes = []
            document = pymupdf.open(stream=data, filetype="tiff")
            try:
                sources: list[Source] = []
                count = min(document.page_count, imaging.MAX_FRAMES)
                for index in range(count):
                    page = document[index]
                    rect = page.rect
                    width, height = sizes[index] if index < len(sizes) else (round(rect.width), round(rect.height))
                    if width * height > imaging.MAX_SOURCE_PIXELS:
                        raise SplitError("IMAGE_TOO_LARGE", detail=f"{width}x{height}")
                    matrix = pymupdf.Matrix(width / rect.width, height / rect.height)
                    pix = page.get_pixmap(matrix=matrix, alpha=False)
                    label = origin if count == 1 else f"{index + 1}번째 장"
                    sources.append(_as_source(_to_image(pix, pymupdf), label, origin))
                if not sources:
                    raise SplitError("CORRUPT", detail="tiff: no frames")
                return sources
            finally:
                document.close()
        pix = pymupdf.Pixmap(data)
        if pix.width * pix.height > imaging.MAX_SOURCE_PIXELS:
            raise SplitError("IMAGE_TOO_LARGE", detail=f"{pix.width}x{pix.height}")
        return [_as_source(_to_image(pix, pymupdf), origin, origin)]
    except SplitError:
        raise
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    except Exception as exc:
        raise SplitError("CORRUPT", detail=f"mupdf: {type(exc).__name__}: {exc}") from None
