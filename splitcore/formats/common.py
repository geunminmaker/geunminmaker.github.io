"""Helpers shared by the document readers."""
from __future__ import annotations

import hashlib
import re

from .. import imaging
from ..errors import SplitError
from ..sources import RasterSource, Source

#: Pictures smaller than this (either side) are bullets, icons and rules, not "the picture".
MIN_PICTURE_SIDE = 48
#: Stop collecting after this many pictures from one document.
MAX_PICTURES = 300
#: Largest single embedded picture we try to read.
MAX_PICTURE_BYTES = 150 * 1024 * 1024

HINT_NO_PICTURE = (
    "문서에서 사진이나 그림을 찾지 못했어요. 글자와 도형으로 만든 문서라면 "
    "PDF로 저장(내보내기)한 뒤 그 PDF를 올려 주세요."
)
HINT_PASSWORD_DOC = "암호가 걸린 문서는 열 수 없어요. 암호를 해제해서 다시 저장한 뒤 올려 주세요."

IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "bmp", "dib", "tif", "tiff", "webp",
    "heic", "heif", "avif", "ico", "psd", "tga", "pcx", "ppm", "pgm", "pbm", "jp2", "j2k",
}
VECTOR_EXTENSIONS = {"emf", "wmf", "svg", "eps", "wdp", "pict", "pct"}


def is_metafile(data: bytes) -> bool:
    """Windows metafiles (EMF / WMF) cannot be rendered in the browser."""
    head = bytes(data[:44])
    if head[:4] == b"\xd7\xcd\xc6\x9a":  # placeable WMF
        return True
    if head[:4] in (b"\x01\x00\x09\x00", b"\x02\x00\x09\x00"):  # plain WMF
        return True
    return len(head) >= 44 and head[:4] == b"\x01\x00\x00\x00" and head[40:44] == b" EMF"


def natural_key(text: str):
    """Sort key that orders "image10" after "image9"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


class PictureCollector:
    """Turns raw embedded picture blobs into sources, with filtering and labels.

    * identical pictures are kept once;
    * pictures that cannot be decoded here (metafiles, damaged data) are counted
      and skipped instead of failing the whole document;
    * tiny decorations are dropped unless they are all there is.
    """

    def __init__(self, origin: str, group: str | None = None, numbered: bool = False):
        self.origin = origin
        self.group = group
        #: label the surviving pictures "이미지 1", "이미지 2" ... without gaps
        self.numbered = numbered
        self._big: list[Source] = []
        self._small: list[Source] = []
        self._seen: set[str] = set()
        self.skipped_vector = 0
        self.skipped_unreadable = 0
        self.undecodable: list[tuple[str, bytes, str]] = []

    @property
    def full(self) -> bool:
        return len(self._big) + len(self._small) >= MAX_PICTURES

    def add(self, data: bytes, label: str) -> bool:
        """Offer one blob.  Returns True when it became a candidate."""
        if self.full or not data or len(data) > MAX_PICTURE_BYTES:
            return False
        digest = hashlib.sha1(data).hexdigest()
        if digest in self._seen:
            return False
        self._seen.add(digest)
        if is_metafile(data):
            self.skipped_vector += 1
            return False
        kind = imaging.sniff_image(data)
        if kind == "svg":
            self.skipped_vector += 1
            return False
        try:
            sources = [RasterSource(data, label=label, origin=self.origin, group=self.group, kind="embedded")]
        except SplitError as exc:
            if exc.code == "UNSUPPORTED" and kind in ("heic", "avif", "jp2"):
                # the web app may be able to decode these with the browser
                self.undecodable.append((label, data, kind))
            self.skipped_unreadable += 1
            return False
        for source in sources:
            width, height = source.size
            (self._small if min(width, height) < MIN_PICTURE_SIDE else self._big).append(source)
        return True

    def result(self, *, empty_hint: str = HINT_NO_PICTURE) -> list[Source]:
        sources = self._big or self._small
        if sources:
            if self.numbered:
                for index, source in enumerate(sources, 1):
                    source.label = f"이미지 {index}"
            return sources
        detail = f"vector={self.skipped_vector} unreadable={self.skipped_unreadable}"
        if self.skipped_vector and not self.skipped_unreadable:
            raise SplitError(
                "NO_IMAGE",
                "문서 속 그림이 벡터(EMF/WMF) 형식이라 꺼낼 수 없어요. PDF로 저장한 뒤 그 PDF를 올려 주세요.",
                detail=detail,
            )
        raise SplitError("NO_IMAGE", empty_hint, detail=detail)
