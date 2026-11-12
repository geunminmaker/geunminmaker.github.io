"""Small helpers shared by the Word and PowerPoint writers."""
from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

EMU_PER_MM = 36000

XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def emu(mm: float) -> int:
    return int(round(mm * EMU_PER_MM))


def attr(text: str) -> str:
    """Escape for use inside a double-quoted XML attribute."""
    return escape(str(text), {'"': "&quot;"})


class Package:
    """Writes an Open Packaging Conventions zip (XML deflated, pictures stored)."""

    def __init__(self) -> None:
        self._buffer = io.BytesIO()
        self._zip = zipfile.ZipFile(self._buffer, "w", zipfile.ZIP_DEFLATED)
        self._stamp = time.localtime()[:6]

    def _info(self, name: str, method: int) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, self._stamp)
        info.compress_type = method
        info.external_attr = 0o644 << 16
        return info

    def xml(self, name: str, body: str) -> None:
        self._zip.writestr(self._info(name, zipfile.ZIP_DEFLATED), (XML_HEADER + body).encode("utf-8"))

    def binary(self, name: str, data: bytes) -> None:
        self._zip.writestr(self._info(name, zipfile.ZIP_STORED), data)

    def finish(self) -> bytes:
        self._zip.close()
        return self._buffer.getvalue()


def relationships(items: list[tuple[str, str, str]]) -> str:
    """items: (rId, type URL, target)."""
    rows = "".join(f'<Relationship Id="{rid}" Type="{kind}" Target="{attr(target)}"/>' for rid, kind, target in items)
    return f'<Relationships xmlns="{REL_NS}">{rows}</Relationships>'


def core_properties(title: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title><dc:creator>이미지 분할</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def app_properties(application: str) -> str:
    return (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>{escape(application)}</Application></Properties>"
    )
