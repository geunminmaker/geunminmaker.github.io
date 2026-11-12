"""PDF export.

Raster tiles are written with a tiny purpose-built PDF writer (no third-party
library, so nothing extra has to be downloaded in the browser):

* photos are embedded as JPEG streams (``/DCTDecode``) exactly as encoded;
* transparent tiles and flat graphics are embedded losslessly by reusing the
  PNG scan-line data (``/FlateDecode`` with PNG predictors) plus a soft mask;
* an embedded colour profile (for example Display P3 from phone cameras) is
  carried over as an ``/ICCBased`` colour space so colours print as intended.

When the source itself is a PDF page the source renders the tiles as *vector*
pages instead (see ``formats/pdf.py``), so text and line art stay perfectly
sharp at any enlargement.
"""
from __future__ import annotations

import hashlib
import io
import struct
import zlib
from datetime import datetime, timezone

from PIL import Image

from .. import __version__, imaging
from ..splitter import MM_PER_INCH, PageSpec, SplitPlan, iter_tiles
from .base import Progress, looks_like_graphics, quality_option, report

PT_PER_MM = 72.0 / MM_PER_INCH


def _num(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _text(value: str) -> bytes:
    """PDF text string (UTF-16BE hex), safe for Korean titles."""
    return b"<FEFF" + value.encode("utf-16-be").hex().upper().encode("ascii") + b">"


class PdfWriter:
    """Just enough of PDF 1.4 to place one image per page."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
        self._size = len(self._chunks[0])
        self._offsets: dict[int, int] = {}
        self._next = 1
        self._digest = hashlib.md5()

    def reserve(self) -> int:
        number = self._next
        self._next += 1
        return number

    def _emit(self, data: bytes) -> None:
        self._chunks.append(data)
        self._size += len(data)

    def write(self, number: int, dictionary: bytes, stream: bytes | None = None) -> int:
        self._offsets[number] = self._size
        self._digest.update(dictionary)
        self._emit(b"%d 0 obj\n" % number)
        if stream is None:
            self._emit(dictionary + b"\nendobj\n")
        else:
            self._emit(dictionary + b"\nstream\n")
            self._emit(stream)
            self._emit(b"\nendstream\nendobj\n")
            self._digest.update(struct.pack(">I", len(stream)))
        return number

    def add(self, dictionary: bytes, stream: bytes | None = None) -> int:
        return self.write(self.reserve(), dictionary, stream)

    def finish(self, root: int, info: int) -> bytes:
        xref_at = self._size
        count = self._next
        lines = [b"xref\n0 %d\n" % count, b"0000000000 65535 f \n"]
        for number in range(1, count):
            lines.append(b"%010d 00000 n \n" % self._offsets[number])
        file_id = self._digest.hexdigest().upper().encode("ascii")
        lines.append(
            b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R /ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n"
            % (count, root, info, file_id, file_id, xref_at)
        )
        self._emit(b"".join(lines))
        return b"".join(self._chunks)


def _png_idat(im: Image.Image) -> bytes:
    """The zlib stream of PNG-filtered scan lines for an L or RGB image."""
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=6)
    data = buf.getvalue()
    pos, parts = 8, []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT":
            parts.append(data[pos + 8:pos + 8 + length])
        elif kind == b"IEND":
            break
        pos += 12 + length
    return b"".join(parts)


def _icc_object(writer: PdfWriter, cache: dict, profile: bytes | None, components: int):
    """Return a colour-space token: a device space or a shared ICCBased object."""
    device = b"/DeviceGray" if components == 1 else b"/DeviceRGB"
    if not profile or len(profile) < 128:
        return device
    # bytes 16..20 of an ICC header name the colour space of the data
    space = bytes(profile[16:20])
    if (components == 3 and space != b"RGB ") or (components == 1 and space != b"GRAY"):
        return device
    key = (components, hashlib.md5(profile).hexdigest())
    if key not in cache:
        packed = zlib.compress(profile, 6)
        cache[key] = writer.add(
            b"<< /N %d /Alternate %s /Filter /FlateDecode /Length %d >>" % (components, device, len(packed)),
            packed,
        )
    return b"[/ICCBased %d 0 R]" % cache[key]


def _add_image(writer: PdfWriter, tile: Image.Image, lossless: bool, quality: int, icc_cache: dict) -> int:
    width, height = tile.size
    profile = tile.info.get("icc_profile")
    smask = b""
    if tile.mode == "RGBA":
        alpha = tile.getchannel("A")
        mask_stream = _png_idat(alpha)
        mask = writer.add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 1 "
            b"/BitsPerComponent 8 /Columns %d >> /Length %d >>" % (width, height, width, len(mask_stream)),
            mask_stream,
        )
        smask = b" /SMask %d 0 R" % mask
        tile = tile.convert("RGB")
        lossless = True
    elif tile.mode not in ("RGB", "L"):
        tile = tile.convert("RGB")
    components = 1 if tile.mode == "L" else 3
    space = _icc_object(writer, icc_cache, profile, components)
    if lossless:
        stream = _png_idat(tile)
        return writer.add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
            b"/BitsPerComponent 8 /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors %d "
            b"/BitsPerComponent 8 /Columns %d >>%s /Length %d >>"
            % (width, height, space, components, width, smask, len(stream)),
            stream,
        )
    plain = tile.copy()
    plain.info = {}  # the profile lives in the PDF colour space, not inside the JPEG
    stream = imaging.encode_jpeg(plain, quality)
    return writer.add(
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
        b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>" % (width, height, space, len(stream)),
        stream,
    )


def export_pdf(source, plan: SplitPlan, spec: PageSpec, options: dict,
               progress: Progress = None, title: str = "") -> bytes:
    vector = getattr(source, "export_vector_pdf", None)
    if vector is not None and getattr(source, "vector", False) and not options.get("rasterize"):
        return vector(plan, spec, progress, title=title)

    quality = quality_option(options)
    lossless = bool(options.get("lossless")) or looks_like_graphics(source)

    writer = PdfWriter()
    catalog, pages_obj, info = writer.reserve(), writer.reserve(), writer.reserve()
    page_w, page_h = spec.page_w * PT_PER_MM, spec.page_h * PT_PER_MM
    img_w, img_h = spec.w * PT_PER_MM, spec.h * PT_PER_MM
    img_x = spec.x * PT_PER_MM
    img_y = page_h - (spec.y + spec.h) * PT_PER_MM  # PDF origin is the bottom-left corner
    content = ("q %s 0 0 %s %s %s cm /Im0 Do Q" % (_num(img_w), _num(img_h), _num(img_x), _num(img_y))).encode("ascii")
    content_obj = writer.add(b"<< /Length %d >>" % len(content), content)

    icc_cache: dict = {}
    kids = []
    for index, tile in iter_tiles(source, plan):
        image = _add_image(writer, tile, lossless, quality, icc_cache)
        del tile
        kids.append(writer.add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /XObject << /Im0 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_obj, _num(page_w).encode(), _num(page_h).encode(), image, content_obj)
        ))
        report(progress, index + 1, plan.count)

    writer.write(pages_obj, b"<< /Type /Pages /Count %d /Kids [%s] >>"
                 % (len(kids), b" ".join(b"%d 0 R" % k for k in kids)))
    writer.write(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)
    stamp = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ").encode("ascii")
    writer.write(info, b"<< /Title %s /Producer (splitcore %s) /CreationDate (%s) >>"
                 % (_text(title or "split"), __version__.encode("ascii"), stamp))
    return writer.finish(catalog, info)
