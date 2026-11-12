"""ZIP-based containers: DOCX, PPTX, XLSX, HWPX, OpenDocument, EPUB and plain archives.

Only embedded pictures are extracted.  Pages or slides that are *drawn* with
text and shapes cannot be rendered without the authoring program; for those
the user is told to save the document as PDF first.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile

from ..errors import SplitError
from ..sources import Source
from .common import (IMAGE_EXTENSIONS, MAX_PICTURE_BYTES, VECTOR_EXTENSIONS, PictureCollector,
                     natural_key)

_REL = re.compile(rb"<Relationship\b[^>]*>", re.I)
_ATTR = re.compile(rb'(\w[\w:]*)\s*=\s*"([^"]*)"')
_EMBED = re.compile(rb'\br:(?:embed|id|pict)\s*=\s*"([^"]+)"')


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in posixpath.basename(name) else ""


class _Zip:
    """Thin, defensive wrapper around ZipFile."""

    def __init__(self, data: bytes, password: str | None):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SplitError("CORRUPT", detail=f"zip: {exc}") from None
        except Exception as exc:
            raise SplitError("CORRUPT", detail=f"zip: {type(exc).__name__}: {exc}") from None
        self.infos = {i.filename: i for i in self.zf.infolist() if not i.is_dir()}
        self.names = list(self.infos)
        self.lower = {n.lower(): n for n in self.names}
        self.password = password.encode("utf-8") if password else None
        self.encrypted_hit = False

    def has(self, name: str) -> bool:
        return name.lower() in self.lower

    def read(self, name: str, limit: int = MAX_PICTURE_BYTES) -> bytes | None:
        real = self.lower.get(name.lower())
        if real is None:
            return None
        info = self.infos[real]
        if info.file_size > limit:
            return None
        if info.flag_bits & 0x1 and self.password is None:
            self.encrypted_hit = True
            return None
        try:
            return self.zf.read(real, pwd=self.password)
        except RuntimeError:  # wrong / missing password
            self.encrypted_hit = True
            return None
        except Exception:
            return None

    def text(self, name: str) -> bytes:
        return self.read(name, limit=64 * 1024 * 1024) or b""


def _relationships(z: _Zip, part: str) -> dict[bytes, str]:
    """rId -> absolute target path for one OOXML part."""
    folder, base = posixpath.split(part)
    rels = z.text(posixpath.join(folder, "_rels", base + ".rels"))
    out: dict[bytes, str] = {}
    for tag in _REL.findall(rels):
        attrs = dict(_ATTR.findall(tag))
        if attrs.get(b"TargetMode", b"").lower() == b"external":
            continue
        rid, target = attrs.get(b"Id"), attrs.get(b"Target")
        if not rid or not target:
            continue
        path = target.decode("utf-8", "replace")
        path = path[1:] if path.startswith("/") else posixpath.normpath(posixpath.join(folder, path))
        out[rid] = path
    return out


def _part_pictures(z: _Zip, part: str) -> list[str]:
    """Media targets referenced by a part, in order of appearance."""
    rels = _relationships(z, part)
    body = z.text(part)
    ordered, seen = [], set()
    for rid in _EMBED.findall(body):
        target = rels.get(rid)
        if target and target not in seen and z.has(target) and "/media/" in target.lower():
            seen.add(target)
            ordered.append(target)
    return ordered


def _add_media(z: _Zip, collector: PictureCollector, names, label_of) -> None:
    for name in names:
        if collector.full:
            break
        if _ext(name) in VECTOR_EXTENSIONS:
            collector.skipped_vector += 1
            continue
        data = z.read(name)
        if data:
            collector.add(data, label_of(name))


def _load_docx(z: _Zip, origin: str) -> list[Source]:
    collector = PictureCollector(origin, "문서 속 이미지", numbered=True)
    main = "word/document.xml"
    ordered = _part_pictures(z, main) if z.has(main) else []
    rest = sorted((n for n in z.names if n.lower().startswith("word/media/") and n not in ordered),
                  key=natural_key)
    _add_media(z, collector, ordered + rest, posixpath.basename)
    return collector.result()


def _slide_order(z: _Zip) -> list[str]:
    rels = _relationships(z, "ppt/presentation.xml")
    order = []
    for rid in re.findall(rb"<p:sldId\b[^>]*\br:id=\"([^\"]+)\"", z.text("ppt/presentation.xml")):
        target = rels.get(rid)
        if target and z.has(target):
            order.append(target)
    known = set(order)
    extra = sorted((n for n in z.names if re.fullmatch(r"ppt/slides/[^/]+\.xml", n, re.I) and n not in known),
                   key=natural_key)
    return order + extra


def _load_pptx(z: _Zip, origin: str) -> list[Source]:
    collector = PictureCollector(origin, "슬라이드 속 이미지")
    used = set()
    for number, slide in enumerate(_slide_order(z), 1):
        targets = _part_pictures(z, slide)
        for index, target in enumerate(targets, 1):
            if target in used:
                continue
            used.add(target)
            data = z.read(target)
            if _ext(target) in VECTOR_EXTENSIONS:
                collector.skipped_vector += 1
            elif data:
                label = f"슬라이드 {number}" if len(targets) == 1 else f"슬라이드 {number} · 이미지 {index}"
                collector.add(data, label)
    # pictures that live only in masters/layouts (backgrounds) or are unreferenced
    rest = sorted((n for n in z.names if n.lower().startswith("ppt/media/") and n not in used), key=natural_key)
    counter = [0]

    def label(_name: str) -> str:
        counter[0] += 1
        return f"배경·기타 이미지 {counter[0]}"

    _add_media(z, collector, rest, label)
    return collector.result()


def _load_generic(z: _Zip, origin: str, prefixes: tuple[str, ...], group: str, use_names: bool) -> list[Source]:
    collector = PictureCollector(origin, group, numbered=not use_names)
    wanted = []
    for name in z.names:
        low = name.lower()
        if low.startswith("__macosx/") or posixpath.basename(low).startswith("."):
            continue
        if prefixes and not low.startswith(prefixes):
            continue
        ext = _ext(name)
        if ext in IMAGE_EXTENSIONS or ext in VECTOR_EXTENSIONS or (prefixes and ext not in ("xml", "rels")):
            wanted.append(name)
    wanted.sort(key=natural_key)
    _add_media(z, collector, wanted, posixpath.basename)
    if use_names:
        return collector.result(empty_hint="압축 파일 안에서 이미지를 찾지 못했어요.")
    return collector.result()


def load_zip(data: bytes, origin: str, password: str | None = None) -> list[Source]:
    z = _Zip(data, password)
    lower = set(z.lower)
    mimetype = (z.text("mimetype") if "mimetype" in lower else b"").strip()

    try:
        if any(n.startswith("word/") for n in lower):
            sources = _load_docx(z, origin)
        elif any(n.startswith("ppt/") for n in lower):
            sources = _load_pptx(z, origin)
        elif mimetype.startswith(b"application/hwp+zip") or "contents/content.hpf" in lower \
                or any(n.startswith("contents/section") for n in lower):
            from .hangul import load_hangul
            sources = load_hangul(data, origin)
        elif any(n.startswith("xl/") for n in lower):
            sources = _load_generic(z, origin, ("xl/media/",), "문서 속 이미지", use_names=False)
        elif mimetype.startswith(b"application/vnd.oasis.opendocument"):
            sources = _load_generic(z, origin, ("pictures/", "media/"), "문서 속 이미지", use_names=False)
        elif mimetype.startswith(b"application/epub"):
            sources = _load_generic(z, origin, (), "문서 속 이미지", use_names=True)
        else:
            sources = _load_generic(z, origin, (), "압축 파일 속 이미지", use_names=True)
    except SplitError as exc:
        if exc.code == "NO_IMAGE" and z.encrypted_hit:
            raise SplitError("PROTECTED", "암호가 걸린 압축 파일은 열 수 없어요. 압축을 풀어서 이미지를 직접 올려 주세요.",
                             detail="zip-encrypted") from None
        raise
    return sources
