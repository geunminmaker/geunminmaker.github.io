"""Sources: the pictures a user can choose to split.

A *source* hides where the pixels come from (a photo, one frame of a TIFF, a
picture embedded in a document, a PDF page that is rendered on demand) behind
one small interface, so the splitter and the exporters never care.
"""
from __future__ import annotations

from PIL import Image

from . import imaging
from .errors import SplitError


class Source:
    """Base class.  Sub-classes implement :meth:`size` and :meth:`image`."""

    #: "image" (a picture file), "embedded" (picture inside a document), "page" (rendered page)
    kind = "image"

    def __init__(self, *, label: str, origin: str, group: str | None = None):
        self.id = ""  # assigned by the Session
        #: short caption for the picker, e.g. "3쪽", "이미지 2" or the file name
        self.label = label
        #: name of the file the picture came from
        self.origin = origin
        #: optional section caption used to group thumbnails ("문서 속 이미지", "페이지")
        self.group = group
        #: physical size in millimetres when the source has one (PDF pages)
        self.physical_mm: tuple[float, float] | None = None
        #: True when the pixels come from a lossless container (PNG, BMP, vector page ...)
        self.lossless = False
        #: True when tiles can be re-rendered from vector data at any resolution
        self.vector = False

    # -- geometry ---------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        """Reference pixel size of the original picture (width, height)."""
        raise NotImplementedError

    # -- pixels -----------------------------------------------------------
    def image(self) -> Image.Image:
        """The full picture, normalised to L / RGB / RGBA (may be cached)."""
        raise NotImplementedError

    def thumbnail(self, max_px: int) -> Image.Image:
        return imaging.fit_within(self.image(), max_px)

    def render_tile(self, box, out_size) -> Image.Image:
        """Enlarged tile for ``box`` (source pixel coordinates, floats allowed)."""
        return imaging.render_tile(self.image(), box, out_size)

    @property
    def has_alpha(self) -> bool:
        return imaging.has_alpha(self.image())

    def release(self) -> None:
        """Drop cached pixels (the encoded bytes stay, so it can be reloaded)."""

    # -- description ------------------------------------------------------
    def meta(self) -> dict:
        width, height = self.size
        return {
            "id": self.id,
            "label": self.label,
            "origin": self.origin,
            "group": self.group,
            "kind": self.kind,
            "width": width,
            "height": height,
            "vector": self.vector,
            "physical_mm": list(self.physical_mm) if self.physical_mm else None,
        }


_EXIF_ORIENTATION = 0x0112


def _size_already_turned(probe: Image.Image) -> bool:
    """True when ``probe.size`` is the stored height x width (TIFF tags 257 and 256), i.e. the
    reader has applied a quarter-turn orientation to the size by itself."""
    tags = getattr(probe, "tag_v2", None)
    try:
        return probe.size == (int(tags[257]), int(tags[256]))
    except Exception:  # not a TIFF, or a header without plain sizes
        return False


class RasterSource(Source):
    """A picture stored as encoded bytes (JPEG, PNG, WebP, ...)."""

    #: small decoded copy kept for the picker (a few hundred kB at most)
    THUMB_CACHE_PX = 512

    def __init__(self, data: bytes, *, label: str, origin: str, group: str | None = None,
                 frame: int = 0, kind: str = "image"):
        super().__init__(label=label, origin=origin, group=group)
        self.kind = kind
        self._data = bytes(data)
        self._frame = int(frame)
        self._image: Image.Image | None = None
        self._thumb: Image.Image | None = None
        probe = imaging.open_image(self._data)  # raises SplitError when unreadable
        if self._frame:
            try:
                probe.seek(self._frame)
            except Exception as exc:
                raise SplitError("CORRUPT", detail=f"frame {frame}: {exc}") from None
        width, height = probe.size
        try:
            orientation = probe.getexif().get(_EXIF_ORIENTATION, 1)
        except Exception:
            orientation = 1
        # orientations 5-8 lie on their side: shown upright, width and height change places.
        # Pillow's TIFF reader reports that turned size itself, so it must not be exchanged again.
        if orientation in (5, 6, 7, 8) and not _size_already_turned(probe):
            width, height = height, width
        self._size = (int(width), int(height))
        self.format = (probe.format or "").upper()
        self.lossless = self.format not in ("JPEG", "MPO", "WEBP", "JPEG2000")
        self.frames = imaging.frame_count(probe)
        #: pages the file really has (more than ``frames`` when the list is cut at MAX_FRAMES)
        self.frames_total = imaging.frame_total(probe)
        probe.close()

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def data(self) -> bytes:
        return self._data

    def image(self) -> Image.Image:
        if self._image is None:
            self._image = imaging.decode(self._data, self._frame)
            self._size = self._image.size
        return self._image

    def verify(self) -> None:
        """Decode a small version right away, so a damaged file is reported when it is picked."""
        if self._thumb is None and self._image is None:
            self._thumb = imaging.decode_reduced(self._data, self.THUMB_CACHE_PX, self._frame)

    def thumbnail(self, max_px: int) -> Image.Image:
        if self._image is not None:
            return imaging.fit_within(self._image, max_px)
        cached = self._thumb
        if cached is not None and (max(cached.size) >= max_px or max(cached.size) >= max(self._size)):
            return imaging.fit_within(cached, max_px)
        thumb = imaging.decode_reduced(self._data, max_px, self._frame)
        if max_px <= self.THUMB_CACHE_PX:
            self._thumb = thumb
        return thumb

    @property
    def has_alpha(self) -> bool:
        if self._image is not None:
            return imaging.has_alpha(self._image)
        if self.format in ("JPEG", "MPO", "BMP"):
            return False
        return imaging.has_alpha(self.image())

    def release(self) -> None:
        self._image = None


def raster_sources(data: bytes, *, origin: str, label: str | None = None,
                   group: str | None = None, kind: str = "image") -> list[Source]:
    """One source per picture in an image file (several for a multi-page TIFF)."""
    first = RasterSource(data, label=label or origin, origin=origin, group=group, kind=kind)
    if first.frames <= 1:
        return [first]
    sources: list[Source] = []
    base = label or origin
    for index in range(first.frames):
        try:
            src = first if index == 0 else RasterSource(
                data, label=base, origin=origin, group=group, frame=index, kind=kind)
        except SplitError:
            continue
        src.label = f"{index + 1}번째 장"
        sources.append(src)
    sources = sources or [first]
    if first.frames_total > first.frames:
        sources[0].origin_total = first.frames_total  # read by Session.add_file: the list was cut short
    return sources
