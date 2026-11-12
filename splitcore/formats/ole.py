"""OLE2 compound files: Hangul HWP 5.x, legacy Word .doc / PowerPoint .ppt / Excel .xls.

* HWP is handed to :mod:`hangul` (pictures in document order).
* DOC / PPT / XLS: JPEG and PNG pictures are located inside the binary streams
  by their signatures and cut out exactly (best effort - enough for the common
  "one big picture in a document" case).
"""
from __future__ import annotations

import io
import struct

import olefile

from ..errors import SplitError
from ..sources import Source
from .common import HINT_PASSWORD_DOC, MAX_PICTURE_BYTES, PictureCollector, natural_key

HWP_SIGNATURE = b"HWP Document File"
_MAX_STREAM = 256 * 1024 * 1024


def _read(ole, path: str, limit: int = _MAX_STREAM) -> bytes | None:
    try:
        if ole.get_size(path) > limit:
            return None
        return ole.openstream(path).read()
    except Exception:
        return None


# ---------------------------------------------------------------- carving

def _png_end(blob: bytes, start: int) -> int | None:
    pos, end = start + 8, len(blob)
    while pos + 12 <= end:
        (length,) = struct.unpack_from(">I", blob, pos)
        kind = blob[pos + 4:pos + 8]
        if length > MAX_PICTURE_BYTES or not kind.isalpha():
            return None
        pos += 12 + length
        if kind == b"IEND":
            return pos if pos <= end else None
    return None


def _jpeg_end(blob: bytes, start: int) -> int | None:
    pos, end = start + 2, len(blob)
    while pos + 4 <= end:
        if blob[pos] != 0xFF:
            return None
        marker = blob[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0xD9:
            return pos + 2
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            pos += 2
            continue
        length = (blob[pos + 2] << 8) | blob[pos + 3]
        if length < 2:
            return None
        pos += 2 + length
        if marker == 0xDA:  # entropy-coded data until the next real marker
            while True:
                i = blob.find(b"\xff", pos)
                if i < 0 or i + 1 >= end:
                    return None
                follower = blob[i + 1]
                if follower == 0x00 or 0xD0 <= follower <= 0xD7:
                    pos = i + 2
                elif follower == 0xFF:
                    pos = i + 1
                else:
                    pos = i
                    break
    return None


def carve_images(blob: bytes) -> list[bytes]:
    """Cut every complete JPEG / PNG out of a binary blob, in file order."""
    found: list[tuple[int, bytes]] = []
    for magic, finder in ((b"\xff\xd8\xff", _jpeg_end), (b"\x89PNG\r\n\x1a\n", _png_end)):
        pos = 0
        while True:
            start = blob.find(magic, pos)
            if start < 0:
                break
            stop = finder(blob, start)
            if stop is None or stop - start < 128:
                pos = start + len(magic)
                continue
            found.append((start, blob[start:stop]))
            pos = stop  # skip nested thumbnails inside this picture
    found.sort(key=lambda item: item[0])
    return [data for _start, data in found]


def _is_encrypted_office(ole, lower: set[str]) -> bool:
    if "encryptedpackage" in lower or "encryptioninfo" in lower:
        return True  # password-protected docx / pptx / xlsx
    if "worddocument" in lower:
        head = _read(ole, "WordDocument", 64 * 1024 * 1024) or b""
        if len(head) >= 12 and struct.unpack_from("<H", head, 10)[0] & 0x0100:
            return True
    if "encryptedsummary" in lower:
        return True
    return False


def _load_office(ole, streams: list[str], origin: str) -> list[Source]:
    collector = PictureCollector(origin, "문서 속 이미지", numbered=True)
    preferred = ("data", "pictures", "worddocument", "powerpoint document", "workbook", "book")
    ordered = sorted(streams, key=lambda s: (0 if s.lower() in preferred else 1, natural_key(s)))
    for stream in ordered:
        if collector.full:
            break
        blob = _read(ole, stream)
        if not blob or len(blob) < 256:
            continue
        for data in carve_images(blob):
            collector.add(data, "이미지")
    return collector.result()


def load_ole(data: bytes, origin: str, password: str | None = None) -> list[Source]:
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as exc:
        raise SplitError("CORRUPT", detail=f"ole: {type(exc).__name__}: {exc}") from None
    try:
        streams = ["/".join(parts) for parts in ole.listdir(streams=True, storages=False)]
        lower = {s.lower() for s in streams}
        is_hwp = "fileheader" in lower and (_read(ole, "FileHeader", 4096) or b"").startswith(HWP_SIGNATURE)
        if not is_hwp:
            if _is_encrypted_office(ole, lower):
                raise SplitError("PROTECTED", HINT_PASSWORD_DOC, detail="office-encrypted")
            return _load_office(ole, streams, origin)
    finally:
        ole.close()
    from .hangul import load_hangul
    return load_hangul(data, origin)
