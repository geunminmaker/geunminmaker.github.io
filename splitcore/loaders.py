"""File -> list of sources.

The file type is decided by its content (magic bytes), never by the extension
alone, because phones and messengers often rename or mislabel files.
"""
from __future__ import annotations

from . import imaging
from .errors import SplitError
from .sources import Source, raster_sources

#: Largest file accepted.  Bigger files are almost never single pictures and
#: would exhaust the memory of a phone browser.
MAX_FILE_BYTES = 300 * 1024 * 1024

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def detect(name: str, data: bytes) -> str:
    """Return one of: pdf, zip, ole, hwp3, image, unknown."""
    head = bytes(data[:2048])
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(ZIP_MAGICS):
        return "zip"
    if head.startswith(OLE_MAGIC):
        return "ole"
    if head.startswith(b"HWP Document File V"):
        return "hwp3"
    if imaging.sniff_image(head) is not None:
        return "image"
    return "unknown"


def extension(name: str) -> str:
    name = str(name or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def load_file(name: str, data: bytes, password: str | None = None) -> list[Source]:
    """Find every picture the user could mean in this file.

    Raises :class:`SplitError` with a specific code when nothing usable is found.
    """
    name = str(name or "file")
    if data is None or len(data) == 0:
        raise SplitError("EMPTY_FILE")
    if len(data) > MAX_FILE_BYTES:
        raise SplitError("TOO_LARGE", detail=f"{len(data)} bytes")
    data = bytes(data)
    kind = detect(name, data)

    if kind == "pdf":
        from .formats.pdf import load_pdf
        return load_pdf(data, name, password)
    if kind == "zip":
        from .formats.zipdoc import load_zip
        return load_zip(data, name, password)
    if kind == "ole":
        from .formats.ole import load_ole
        return load_ole(data, name, password)
    if kind == "hwp3":
        raise SplitError(
            "UNSUPPORTED",
            "아주 오래된 한글(HWP 3.x) 형식이라 열 수 없어요. 한글에서 PDF나 최신 형식으로 다시 저장해 주세요.",
            detail="hwp3",
        )
    if kind == "image":
        return load_picture(data, name)
    # Unknown signature: Pillow knows many more formats than we sniff (TGA, PCX, PPM ...).
    try:
        return load_picture(data, name)
    except SplitError as exc:
        if exc.code in ("IMAGE_TOO_LARGE", "OUT_OF_MEMORY", "NEED_PACKAGE"):
            raise
        raise SplitError("UNSUPPORTED", detail=f"ext={extension(name)!r}") from None


def load_picture(data: bytes, name: str) -> list[Source]:
    """A picture file.  Every frame is decoded once (small) right away, so a damaged
    file is reported when it is picked and not two screens later."""
    try:
        sources = raster_sources(data, origin=name)
        for source in sources:
            source.verify()
        return sources
    except SplitError as exc:
        from .formats.mupdf_image import FALLBACK_KINDS, load_with_mupdf
        if exc.code in ("CORRUPT", "UNSUPPORTED") and imaging.sniff_image(data) in FALLBACK_KINDS:
            try:
                return load_with_mupdf(data, name)
            except SplitError as second:
                if second.code in ("NEED_PACKAGE", "IMAGE_TOO_LARGE", "OUT_OF_MEMORY"):
                    raise
                raise exc from None
        raise
