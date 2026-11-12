"""PDF input: every page is a candidate, rendered on demand with PyMuPDF.

A PDF page is vector data, so a tile is never an enlarged bitmap: each tile is
rendered straight from the page at the resolution it needs, and a PDF export
keeps real vector pages (text and lines stay sharp however much they grow).
"""
from __future__ import annotations

import math

import pymupdf
from PIL import Image

from .. import __version__, imaging
from ..errors import SplitError
from ..sources import Source
from ..splitter import MM_PER_INCH

#: Resolution of the reference bitmap of a page ("original size" in pixels).
RENDER_DPI = 300
#: Pages offered from one PDF.
MAX_PAGES = 300

PT_PER_MM = 72.0 / MM_PER_INCH

try:  # keep MuPDF's diagnostics off the console
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except Exception:  # pragma: no cover
    pass


class _Document:
    """One opened PDF shared by all of its page sources."""

    def __init__(self, data: bytes, password: str | None):
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise SplitError("CORRUPT", detail=f"pdf: {type(exc).__name__}: {exc}") from None
        try:
            if doc.needs_pass:
                if not password:
                    raise SplitError("ENCRYPTED", detail="pdf")
                if not doc.authenticate(password):
                    raise SplitError("WRONG_PASSWORD", detail="pdf")
            count = doc.page_count
        except SplitError:
            doc.close()
            raise
        except Exception as exc:
            doc.close()
            raise SplitError("CORRUPT", detail=f"pdf: {type(exc).__name__}: {exc}") from None
        if count < 1:
            doc.close()
            raise SplitError("NO_IMAGE", "PDF에 페이지가 없어요.", detail="pdf: no pages")
        try:  # turn comments / form fields into page content so every output shows them
            doc.bake()
        except Exception:
            pass
        self.doc = doc
        self.users = 0

    def release(self) -> None:
        self.users -= 1
        if self.users <= 0 and self.doc is not None:
            try:
                self.doc.close()
            finally:
                self.doc = None


class PdfPageSource(Source):
    kind = "page"

    def __init__(self, holder: _Document, index: int, origin: str, label: str):
        super().__init__(label=label, origin=origin, group="페이지")
        self._holder = holder
        self._index = index
        holder.users += 1
        page = self._page()
        rect = page.rect  # the page as displayed (rotation and crop box applied)
        if rect.width <= 0 or rect.height <= 0 or rect.is_infinite:
            raise SplitError("CORRUPT", detail=f"pdf page {index + 1}: empty page box")
        self._pt = (float(rect.width), float(rect.height))
        scale = RENDER_DPI / 72.0
        pixels = self._pt[0] * self._pt[1] * scale * scale
        if pixels > imaging.MAX_TILE_PIXELS:
            scale *= math.sqrt(imaging.MAX_TILE_PIXELS / pixels)
        self._scale = scale
        self._size = (max(1, round(self._pt[0] * scale)), max(1, round(self._pt[1] * scale)))
        self.physical_mm = (self._pt[0] / PT_PER_MM, self._pt[1] / PT_PER_MM)
        self.lossless = True
        self.vector = True
        self._image: Image.Image | None = None

    # ------------------------------------------------------------ helpers
    def _page(self):
        if self._holder.doc is None:
            raise SplitError("NOT_FOUND", detail="pdf closed")
        try:
            return self._holder.doc[self._index]
        except Exception as exc:
            raise SplitError("CORRUPT", detail=f"pdf page {self._index + 1}: {exc}") from None

    def _render(self, zoom_x: float, zoom_y: float, clip=None) -> Image.Image:
        try:
            pix = self._page().get_pixmap(matrix=pymupdf.Matrix(zoom_x, zoom_y), clip=clip,
                                          alpha=False, colorspace=pymupdf.csRGB)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except MemoryError:
            raise SplitError("OUT_OF_MEMORY") from None
        except SplitError:
            raise
        except Exception as exc:
            raise SplitError("CORRUPT", detail=f"pdf render: {type(exc).__name__}: {exc}") from None
        return image

    # ------------------------------------------------------------ Source API
    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def has_alpha(self) -> bool:
        return False

    def image(self) -> Image.Image:
        if self._image is None:
            image = self._render(self._scale, self._scale)
            if image.size != self._size:
                image = image.resize(self._size, imaging.RESAMPLE_DOWN)
            self._image = image
        return self._image

    def thumbnail(self, max_px: int) -> Image.Image:
        if self._image is not None:
            return imaging.fit_within(self._image, max_px)
        zoom = min(self._scale, max_px / max(self._pt))
        return imaging.fit_within(self._render(zoom, zoom), max_px)

    def _clip(self, box) -> "pymupdf.Rect":
        x0, y0, x1, y1 = (float(v) / self._scale for v in box)
        return pymupdf.Rect(max(0.0, x0), max(0.0, y0), min(self._pt[0], x1), min(self._pt[1], y1))

    def render_tile(self, box, out_size) -> Image.Image:
        clip = self._clip(box)
        if clip.is_empty:
            raise SplitError("IMAGE_TOO_SMALL", detail=f"empty tile box {box}")
        out_w, out_h = int(out_size[0]), int(out_size[1])
        tile = self._render(out_w / clip.width, out_h / clip.height, clip)
        if tile.size != (out_w, out_h):  # MuPDF rounds the clip outwards by up to a pixel
            tile = tile.resize((out_w, out_h), imaging.RESAMPLE_DOWN)
        return tile

    def release(self) -> None:
        self._image = None

    def close(self) -> None:
        self._image = None
        self._holder.release()

    # ------------------------------------------------------------ vector export
    def export_vector_pdf(self, plan, spec, progress=None, title: str = "") -> bytes:
        """Tiles as real PDF pages: the source page is placed enlarged and clipped."""
        source_doc = self._holder.doc
        if source_doc is None:
            raise SplitError("NOT_FOUND", detail="pdf closed")
        out = pymupdf.open()
        source_page = self._page()
        # show_pdf_page() works in the page's unrotated space and ignores /Rotate: map each
        # displayed tile through the derotation matrix and turn the result back upright.
        rotation = source_page.rotation
        derotate = pymupdf.Matrix(source_page.derotation_matrix)
        if rotation:
            source_page.set_rotation(0)  # in memory only, restored below
        try:
            target = pymupdf.Rect(spec.x * PT_PER_MM, spec.y * PT_PER_MM,
                                  (spec.x + spec.w) * PT_PER_MM, (spec.y + spec.h) * PT_PER_MM)
            for index, box in enumerate(plan.boxes):
                clip = self._clip(box) * derotate
                clip.normalize()
                page = out.new_page(width=spec.page_w * PT_PER_MM, height=spec.page_h * PT_PER_MM)
                page.show_pdf_page(target, source_doc, self._index, clip=clip, rotate=-rotation,
                                   keep_proportion=False)
                if progress is not None:
                    progress(index + 1, plan.count)
            out.set_metadata({"title": title or "split", "producer": f"splitcore {__version__}"})
            return out.tobytes(garbage=3, deflate=True)
        except SplitError:
            raise
        except MemoryError:
            raise SplitError("OUT_OF_MEMORY") from None
        except Exception as exc:
            raise SplitError("INTERNAL", detail=f"pdf export: {type(exc).__name__}: {exc}") from None
        finally:
            if rotation:
                try:
                    source_doc[self._index].set_rotation(rotation)
                except Exception:
                    pass
            out.close()


def load_pdf(data: bytes, origin: str, password: str | None = None) -> list[Source]:
    holder = _Document(data, password)
    total = holder.doc.page_count
    sources: list[Source] = []
    for index in range(min(total, MAX_PAGES)):
        try:
            sources.append(PdfPageSource(holder, index, origin, f"{index + 1}쪽"))
        except SplitError:
            continue
    if not sources:
        holder.users = 0
        holder.release()
        raise SplitError("CORRUPT", detail="pdf: no readable page")
    if total == 1:
        sources[0].label = origin
    if total > MAX_PAGES:
        sources[0].origin_total = total  # read by Session.add_file: the list was cut short
    return sources
