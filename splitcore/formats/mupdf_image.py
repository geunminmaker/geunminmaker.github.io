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
from ..errors import SplitError, is_out_of_memory
from ..sources import RasterSource, Source

#: formats worth a second try
FALLBACK_KINDS = ("tiff", "jp2")

#: TIFF Orientation tag -> what turns the stored pixels upright (the same table as EXIF)
_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


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
            # The stored pixel size (tags 256 / 257) and the Orientation tag (274) of every frame
            # come from the TIFF header, which Pillow can read even when it cannot decode the pixels.
            # Its own ``size`` is not used: for orientations 5-8 that one is already turned.
            frames = []
            try:
                probe = Image.open(io.BytesIO(data))
                for index in range(min(int(getattr(probe, "n_frames", 1)), imaging.MAX_FRAMES)):
                    probe.seek(index)
                    tags = probe.tag_v2
                    frames.append((int(tags[256]), int(tags[257]), int(tags.get(274) or 1)))
            except Exception:
                frames = []
            document = pymupdf.open(stream=data, filetype="tiff")
            try:
                sources: list[Source] = []
                total = document.page_count
                count = min(total, imaging.MAX_FRAMES)
                for index in range(count):
                    page = document[index]
                    rect = page.rect
                    width, height, orientation = (frames[index] if index < len(frames)
                                                  else (round(rect.width), round(rect.height), 1))
                    if width * height > imaging.MAX_SOURCE_PIXELS:
                        raise SplitError("IMAGE_TOO_LARGE", detail=f"{width}x{height}")
                    # MuPDF draws the pixels as they are stored and ignores the Orientation tag:
                    # render at the stored size, then turn the picture the way every viewer shows it
                    matrix = pymupdf.Matrix(width / rect.width, height / rect.height)
                    image = _to_image(page.get_pixmap(matrix=matrix, alpha=False), pymupdf)
                    if orientation in _UPRIGHT:
                        image = image.transpose(_UPRIGHT[orientation])
                    label = origin if count == 1 else f"{index + 1}번째 장"
                    sources.append(_as_source(image, label, origin))
                if not sources:
                    raise SplitError("CORRUPT", detail="tiff: no frames")
                if total > count:
                    sources[0].origin_total = total  # read by Session.add_file: the list was cut short
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
        if is_out_of_memory(exc):  # MuPDF reports a failed malloc as an ordinary error
            raise SplitError("OUT_OF_MEMORY", detail=f"mupdf: {exc}") from None
        raise SplitError("CORRUPT", detail=f"mupdf: {type(exc).__name__}: {exc}") from None
