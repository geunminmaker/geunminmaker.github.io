# -*- coding: utf-8 -*-
"""hwp_reader.py - pull the embedded pictures out of Hangul documents.

    images = extract_images(data, "report.hwp")      # -> [("BIN0001.jpg", b"..."), ...]

Supported containers (detected from the bytes, the file name is only a hint):
  * HWP 5.x binary (.hwp)  - OLE2 compound file, needs the pure-python `olefile` package
  * HWPX / OWPML   (.hwpx) - zip package, standard library only

Runs on CPython 3.8+ and inside Pyodide.  No lxml, no Windows-only code, no file-system access.

Order of the result: pictures in the order they are first used in the body text
(sections in order; headers, footers, tables and text boxes included), then
embedded pictures that are registered in the document's binary table (DocInfo
BIN_DATA records / content.hpf manifest) but not referenced from the body (e.g.
used as a fill).  The same binary used several times is returned once.
BinData entries that the binary table does not list at all ("orphans") cannot
be shown by Hangul and are only returned with include_orphans=True.

Errors are typed, see HwpError and its subclasses.  Every error carries
`.code` (stable identifier) and `.user_message` (Korean, ready for the UI).

Format facts this reader relies on (each one checked on public fixtures)
-----------------------------------------------------------------------
HWP 5.x
* FileHeader stream: 32-byte signature "HWP Document File", version DWORD at
  offset 32, property flags DWORD at offset 36 (bit 0 compressed, 1 password,
  2 distribution, 4 DRM, 8 certificate encryption, 10 certificate DRM).
* Streams are raw deflate (zlib wbits=-15) when bit 0 is set, plain otherwise.
* Records: DWORD header = tag (10 bits) | level (10 bits) | size (12 bits);
  size 0xFFF means "the next DWORD is the size".
* DocInfo, tag 0x12 BIN_DATA: UINT16 attr (bits 0-3: 0 link, 1 embedded,
  2 OLE storage), then for embedded items UINT16 storage id and a length
  prefixed UTF-16 extension -> stream "BinData/BIN%04X.<ext>" (hexadecimal).
* BodyText/SectionN, tag 0x55 SHAPE_COMPONENT_PICTURE: the UINT16 at payload
  offset 71 is a 1-based INDEX into the BIN_DATA record list - not the storage
  id (pyhwp sample-5017-pics.hwp: index 2 -> storage id 11 -> BIN000B.jpg).
* Stale BinData streams without a BIN_DATA record do exist in files saved by Hangul.
HWPX
* Contents/content.hpf maps item ids to hrefs; pictures use
  <hc:img binaryItemIDRef="image1">, a video poster uses imageIDRef.
* A linked (not embedded) item has an absolute path as href and isEmbeded="0".
* The extension inside the package can be upper-case (image1.PNG).
"""

import io
import re
import struct
import zipfile
import zlib

__all__ = [
    "extract_images", "inspect_document", "sniff_image_type",
    "HwpError", "HwpFormatError", "HwpUnsupportedError", "HwpProtectedError",
    "HwpPasswordError", "HwpDrmError", "HwpDistributionError", "HwpNoImagesError",
]

# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class HwpError(Exception):
    """Base class of everything this module raises on purpose."""
    code = "hwp_error"
    user_message = "한글 문서를 읽을 수 없습니다."

    def __init__(self, message=None, **details):
        super().__init__(message or self.__class__.__doc__)
        self.details = details


class HwpFormatError(HwpError):
    """Not a HWP 5.x / HWPX document, or the file is damaged."""
    code = "hwp_bad_format"
    user_message = "한글(HWP/HWPX) 문서가 아니거나 파일이 손상되었습니다."


class HwpUnsupportedError(HwpError):
    """A Hangul format this reader does not handle (HWP 3.x, HWPML)."""
    code = "hwp_unsupported"
    user_message = ("지원하지 않는 옛 한글 형식입니다. "
                    "한글에서 HWPX 또는 PDF로 다시 저장해 주세요.")


class HwpProtectedError(HwpError):
    """The document is protected and its content must not / cannot be read."""
    code = "hwp_protected"
    user_message = "보호된 문서라서 열 수 없습니다."


class HwpPasswordError(HwpProtectedError):
    """The document is encrypted with a password."""
    code = "hwp_password"
    user_message = ("암호가 걸린 문서입니다. "
                    "한글에서 암호를 해제한 뒤 다시 골라 주세요.")


class HwpDrmError(HwpProtectedError):
    """The document is protected by DRM / certificate based encryption."""
    code = "hwp_drm"
    user_message = "DRM(문서 보안)이 적용된 문서라서 열 수 없습니다."


class HwpDistributionError(HwpProtectedError):
    """The document is a distribution ("배포용") document: copying its content is restricted."""
    code = "hwp_distribution"
    user_message = ("배포용 문서는 내용을 가져올 수 없습니다. "
                    "원본 문서나 이미지 파일을 골라 주세요.")


class HwpNoImagesError(HwpError):
    """The document has no embedded picture that can be used.

    .linked  - paths of pictures that are only *linked* (stored outside the document)
    .skipped - [(name, reason)] for embedded objects that are not usable raster pictures
    """
    code = "hwp_no_images"
    user_message = "문서에 포함된 그림이 없습니다."

    def __init__(self, message=None, linked=(), skipped=()):
        self.linked = list(linked)
        self.skipped = list(skipped)
        if message is None:
            message = "no embedded pictures found"
            if self.linked:
                message += "; %d picture(s) are linked to external files and not stored in the document" % len(self.linked)
            if self.skipped:
                message += "; skipped: " + ", ".join("%s (%s)" % s for s in self.skipped[:8])
        super().__init__(message, linked=self.linked, skipped=self.skipped)
        if self.linked:
            self.user_message = ("그림이 문서에 포함되지 않고 연결(링크)만 되어 있습니다. "
                                 "한글에서 '문서에 포함'으로 다시 저장하거나 이미지 파일을 직접 골라 주세요.")
        elif self.skipped:
            self.user_message = ("문서에 사용할 수 있는 그림이 없습니다. "
                                 "(OLE 개체나 WMF/EMF 그림은 지원하지 않습니다.)")


# --------------------------------------------------------------------------
# image sniffing
# --------------------------------------------------------------------------
RASTER_TYPES = ("jpg", "png", "gif", "bmp", "tif", "webp")     # decodable by Pillow everywhere (incl. Pyodide)
VECTOR_TYPES = ("wmf", "emf", "svg")                            # pictures, but not decodable in the browser engine
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff_image_type(data):
    """Return 'jpg' | 'png' | 'gif' | 'bmp' | 'tif' | 'webp' | 'wmf' | 'emf' | 'svg' or None (magic bytes only)."""
    head = bytes(data[:64])
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM" and len(data) >= 26:
        size, reserved = struct.unpack_from("<IxxxxI", head, 2)[0], head[6:10]
        dib = struct.unpack_from("<I", head, 14)[0]
        if dib in (12, 40, 52, 56, 64, 108, 124) and (reserved == b"\x00\x00\x00\x00" or size == len(data)):
            return "bmp"
    if head[:4] == b"\xd7\xcd\xc6\x9a" or head[:6] in (b"\x01\x00\x09\x00\x00\x03", b"\x02\x00\x09\x00\x00\x03"):
        return "wmf"
    if head[:4] == b"\x01\x00\x00\x00" and head[40:44] == b" EMF":
        return "emf"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text[:5] == b"<?xml" or text[:4] == b"<svg":
        if b"<svg" in bytes(data[:2048]):
            return "svg"
    return None


MAX_PART_BYTES = 300 * 1024 * 1024      # refuse to inflate a single stream / zip entry beyond this (zip-bomb guard)


def _inflate(raw):
    """Raw-deflate (no zlib header) as used for HWP 5 streams; None when `raw` is not deflate data."""
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(raw, MAX_PART_BYTES)
        if d.unconsumed_tail:
            return None                 # absurdly large: treat as unusable
        return out + d.flush()
    except zlib.error:
        return None


# --------------------------------------------------------------------------
# HWP 5.x (OLE2 compound file)
# --------------------------------------------------------------------------
_HWP5_SIGNATURE = b"HWP Document File"
_HWP3_SIGNATURE = b"HWP Document File V"          # "HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05"
_DRM_WRAPPER_MARKS = (b"DRMONE", b"NASCA DRM", b"Fasoo", b"SCDSA00", b"MarkAny", b"DOCUMENTSAFER")

# FileHeader property bits (HWP 5.0 file format specification, "FileHeader")
_F_COMPRESSED = 1 << 0
_F_PASSWORD = 1 << 1
_F_DISTRIBUTION = 1 << 2
_F_DRM = 1 << 4
_F_CERT_ENCRYPTED = 1 << 8
_F_CERT_DRM = 1 << 10

_TAG_DOCUMENT_PROPERTIES = 0x10         # HWPTAG_BEGIN, always the first record of DocInfo
_TAG_BIN_DATA = 0x12                    # HWPTAG_BEGIN (0x10) + 2, in DocInfo
_TAG_SHAPE_COMPONENT_PICTURE = 0x55     # HWPTAG_BEGIN (0x10) + 69, in BodyText
_PICTURE_BINITEM_OFFSET = 71            # border(12) + image rect(32) + crop(16) + inner margin(8) + bright/contrast/effect(3)


def _records(data):
    """Iterate (tag, level, payload) over a decompressed HWP 5 record stream."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                return
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        if pos + size > n:
            return
        yield tag, level, data[pos:pos + size]
        pos += size


def _hwp_stream(ole, name, compressed):
    raw = ole.openstream(name).read()
    if compressed:
        out = _inflate(raw)
        if out is not None:
            return out
    return raw


def _wstr(payload, pos):
    n = struct.unpack_from("<H", payload, pos)[0]
    pos += 2
    return payload[pos:pos + 2 * n].decode("utf-16-le", "replace"), pos + 2 * n


def _section_key(name):
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def _inspect_hwp5(data):
    try:
        import olefile
    except ImportError:                                        # pragma: no cover
        raise HwpError("the 'olefile' package is required to read .hwp files")
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as ex:
        raise HwpFormatError("not a readable OLE2 compound file: %s" % ex)
    try:
        streams = {}
        for entry in ole.listdir(streams=True, storages=False):
            streams["/".join(entry).lower()] = "/".join(entry)
        if "fileheader" not in streams:
            raise HwpFormatError("OLE file without a FileHeader stream - not a HWP 5.x document")
        header = ole.openstream(streams["fileheader"]).read()
        if len(header) < 40 or not header.startswith(_HWP5_SIGNATURE):
            raise HwpFormatError("FileHeader signature mismatch - not a HWP 5.x document")
        version, flags = struct.unpack_from("<II", header, 32)
        info = {
            "format": "hwp",
            "version": "%d.%d.%d.%d" % (version >> 24, (version >> 16) & 255, (version >> 8) & 255, version & 255),
            "flags": flags, "compressed": bool(flags & _F_COMPRESSED),
            "images": [], "skipped": [], "linked": [], "order": "document",
        }
        if flags & _F_PASSWORD:
            raise HwpPasswordError("password protected HWP document (FileHeader flag bit 1)")
        if flags & (_F_DRM | _F_CERT_ENCRYPTED | _F_CERT_DRM):
            raise HwpDrmError("DRM / certificate protected HWP document (FileHeader flags %#x)" % flags)
        if flags & _F_DISTRIBUTION:
            raise HwpDistributionError("distribution (read-only, copy-restricted) HWP document (FileHeader flag bit 2)")
        if version >> 24 != 5:
            raise HwpUnsupportedError("unsupported HWP version %s" % info["version"])
        compressed = info["compressed"]

        bin_streams = {low: real for low, real in streams.items() if low.startswith("bindata/")}

        def load(stream_name, mode):
            """mode: 0 follow the file flag, 1 always compressed, 2 never compressed (BIN_DATA attr bits 4-5)."""
            raw = ole.openstream(stream_name).read()
            want = compressed if mode == 0 else (mode == 1)
            first, second = (_inflate(raw), raw) if want else (raw, None)
            if first is not None and sniff_image_type(first) is not None:
                return first
            # The flags and the bytes disagree (seen in files written by third-party tools):
            # trust the magic bytes of whichever interpretation is a picture.
            if second is None:
                second = _inflate(raw)
            if second is not None and sniff_image_type(second) is not None:
                return second
            return first if first is not None else raw

        # ---- DocInfo: the BIN_DATA table (1-based index = "bin item id" used by pictures)
        items = []                       # index -> dict(kind, stream, ext, mode, path)
        table_ok = True
        try:
            docinfo = _hwp_stream(ole, streams["docinfo"], compressed)
            first_tag = None
            for tag, _level, payload in _records(docinfo):
                if first_tag is None:
                    first_tag = tag
                    if tag != _TAG_DOCUMENT_PROPERTIES:      # not a DocInfo record stream we understand
                        raise ValueError("unexpected first DocInfo record %#x" % tag)
                if tag != _TAG_BIN_DATA or len(payload) < 2:
                    continue
                attr = struct.unpack_from("<H", payload, 0)[0]
                kind, mode = attr & 0x0F, (attr >> 4) & 0x03
                item = {"kind": kind, "mode": mode, "stream": None, "ext": "", "path": ""}
                try:
                    if kind == 0:                        # LINK: absolute path, relative path
                        item["path"], pos = _wstr(payload, 2)
                        if not item["path"]:
                            item["path"], pos = _wstr(payload, pos)
                    else:                                # 1 = EMBEDDING, 2 = STORAGE (OLE)
                        bin_id = struct.unpack_from("<H", payload, 2)[0]
                        item["ext"], _ = _wstr(payload, 4)
                        item["stream"] = bin_streams.get(("bindata/bin%04x.%s" % (bin_id, item["ext"])).lower())
                        if item["stream"] is None:       # extension spelled differently in the storage
                            prefix = "bindata/bin%04x." % bin_id
                            for low, real in bin_streams.items():
                                if low.startswith(prefix):
                                    item["stream"] = real
                                    break
                except (struct.error, IndexError):
                    pass
                items.append(item)
        except Exception:
            table_ok = False
            items = []

        # ---- BodyText: order of first use
        order = []
        if table_ok and items:
            try:
                sections = sorted((real for low, real in streams.items() if low.startswith("bodytext/")),
                                  key=_section_key)
                for name in sections:
                    for tag, _level, payload in _records(_hwp_stream(ole, name, compressed)):
                        if tag == _TAG_SHAPE_COMPONENT_PICTURE and len(payload) >= _PICTURE_BINITEM_OFFSET + 2:
                            idx = struct.unpack_from("<H", payload, _PICTURE_BINITEM_OFFSET)[0]
                            if 1 <= idx <= len(items) and idx not in order:
                                order.append(idx)
            except Exception:
                info["order"] = "table"
        for idx in range(1, len(items) + 1):             # registered but not referenced from the body
            if idx not in order:
                order.append(idx)

        used = set()
        if table_ok:
            for idx in order:
                item = items[idx - 1]
                if item["kind"] == 0:
                    info["linked"].append(item["path"])
                    continue
                if not item["stream"]:
                    info["skipped"].append(("BIN#%d.%s" % (idx, item["ext"]), "stream missing"))
                    continue
                used.add(item["stream"].lower())
                name = item["stream"].split("/")[-1]
                if item["kind"] == 2:
                    info["skipped"].append((name, "OLE object"))
                    continue
                info["images"].append((name, load(item["stream"], item["mode"])))
            # streams the BIN_DATA table does not know: Hangul cannot display them (left-overs of other tools)
            orphans = [real for low, real in sorted(bin_streams.items()) if low not in used]
            info["orphans"] = [real.split("/")[-1] for real in orphans]
            info["_orphan_data"] = [(real.split("/")[-1], load(real, 0)) for real in orphans]
        else:
            # DocInfo unreadable: fall back to the BinData streams in name order
            info["order"] = "stream"
            for low in sorted(bin_streams):
                info["images"].append((bin_streams[low].split("/")[-1], load(bin_streams[low], 0)))
            info["orphans"] = []
        return info
    finally:
        ole.close()


# --------------------------------------------------------------------------
# HWPX (zip)
# --------------------------------------------------------------------------
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
_ITEM_RE = re.compile(r"<(?:\w+:)?item\b([^>]*)>")
_ITEMREF_RE = re.compile(r"<(?:\w+:)?itemref\b([^>]*)>")
# <hc:img binaryItemIDRef="image1"> (pictures, image fills) and <hp:video imageIDRef="image2"> (poster frame)
_BINREF_RE = re.compile(r'\b(?:binaryItemIDRef|imageIDRef)\s*=\s*"([^"]+)"')


def _xml_text(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")


def _inspect_hwpx(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as ex:
        raise HwpFormatError("not a readable zip package: %s" % ex)
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        lower = {n.lower(): n for n in names}
        info = {"format": "hwpx", "version": "", "flags": 0, "compressed": True,
                "images": [], "skipped": [], "linked": [], "order": "document", "orphans": []}

        def read(name):
            if zf.getinfo(name).file_size > MAX_PART_BYTES:
                raise HwpFormatError("zip entry %s is unreasonably large" % name)
            try:
                return zf.read(name)
            except RuntimeError as ex:                 # zip-level encryption
                raise HwpPasswordError("encrypted zip entry %s: %s" % (name, ex))
            except Exception as ex:
                raise HwpFormatError("damaged zip entry %s: %s" % (name, ex))

        if any(zi.flag_bits & 0x1 for zi in zf.infolist()):
            raise HwpPasswordError("password protected HWPX (encrypted zip entries)")
        sections = sorted((n for n in names if re.match(r"(?i)contents/section\d+\.xml$", n)), key=lambda n: _section_key(n[:-4]))
        is_package = bool(sections) or "contents/content.hpf" in lower or "mimetype" in lower
        if not is_package:
            raise HwpFormatError("zip file without Contents/section*.xml - not a HWPX document")
        mimetype = read(lower["mimetype"]).strip() if "mimetype" in lower else b""
        if mimetype and b"hwp" not in mimetype.lower() and not sections:
            raise HwpFormatError("zip package of another type (%r) - not a HWPX document" % mimetype[:60])
        if "version.xml" in lower:
            m = re.search(r'major="(\d+)" minor="(\d+)" micro="(\d+)" buildNumber="(\d+)"', _xml_text(read(lower["version.xml"])))
            if m:
                info["version"] = ".".join(m.groups())
        # Password protected packages list the encrypted parts in META-INF/manifest.xml
        # (ODF style <encryption-data>); the parts themselves are then not XML any more.
        if "meta-inf/manifest.xml" in lower:
            manifest = _xml_text(read(lower["meta-inf/manifest.xml"]))
            if "encryption-data" in manifest or "EncryptionData" in manifest:
                raise HwpPasswordError("password protected HWPX (META-INF/manifest.xml lists encrypted parts)")
        for name in sections[:1]:
            head = read(name)[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
            if not head.startswith(b"<"):
                raise HwpProtectedError("HWPX section is not XML - the package is encrypted or DRM protected")

        # manifest: item id -> href
        hrefs, spine = {}, []
        if "contents/content.hpf" in lower:
            hpf = _xml_text(read(lower["contents/content.hpf"]))
            for m in _ITEM_RE.finditer(hpf):
                attrs = dict(_ATTR_RE.findall(m.group(1)))
                if "id" in attrs and "href" in attrs:
                    hrefs[attrs["id"]] = attrs
            for m in _ITEMREF_RE.finditer(hpf):
                attrs = dict(_ATTR_RE.findall(m.group(1)))
                item = hrefs.get(attrs.get("idref", ""))
                if item and re.match(r"(?i)contents/section\d+\.xml$", item["href"]) and item["href"].lower() in lower:
                    spine.append(lower[item["href"].lower()])
        ordered_sections = spine + [s for s in sections if s not in spine]
        other_xml = [n for n in names if re.match(r"(?i)contents/.*\.xml$", n) and n not in ordered_sections]

        bin_entries = [n for n in names if n.lower().startswith("bindata/")]
        by_stem = {}
        for n in bin_entries:
            by_stem.setdefault(n.split("/")[-1].rsplit(".", 1)[0].lower(), n)

        def resolve(ref):
            """binaryItemIDRef -> zip entry name, or ('linked', path), or None."""
            attrs = hrefs.get(ref)
            if attrs:
                href = attrs["href"].replace("\\", "/")
                if href.lower() in lower:
                    return lower[href.lower()]
                if href.lower().lstrip("./") in lower:
                    return lower[href.lower().lstrip("./")]
                if not href.lower().startswith("bindata/"):
                    return ("linked", attrs["href"])
            return by_stem.get(ref.lower())

        refs = []
        for name in ordered_sections + other_xml:
            for ref in _BINREF_RE.findall(_xml_text(read(name))):
                if ref not in refs:
                    refs.append(ref)
        used = []
        for ref in refs:
            target = resolve(ref)
            if isinstance(target, tuple):
                if target[1] not in info["linked"]:
                    info["linked"].append(target[1])
            elif target is None:
                info["skipped"].append((ref, "binary item not found in the package"))
            elif target not in used:
                used.append(target)
        if not refs:
            info["order"] = "table"
        # registered in the manifest but not referenced by a picture (other kinds of use): keep, after the rest
        registered = []
        for attrs in hrefs.values():
            href = attrs["href"].replace("\\", "/").lower()
            if href.startswith("bindata/") and href in lower:
                registered.append(lower[href])
        if not hrefs:                              # no manifest at all: every BinData entry counts
            registered = list(bin_entries)
            if not refs:
                info["order"] = "stream"
        for name in registered:
            if name not in used:
                used.append(name)
        orphans = [n for n in bin_entries if n not in used]
        info["orphans"] = [n.split("/")[-1] for n in orphans]
        for name in used:
            info["images"].append((name.split("/")[-1], read(name)))
        info["_orphan_data"] = [(n.split("/")[-1], read(n)) for n in orphans]
        return info


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def _detect(data, filename):
    head = bytes(data[:64])
    if head[:8] == _OLE_MAGIC:
        return "hwp"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "hwpx"
    if head.startswith(_HWP3_SIGNATURE):
        raise HwpUnsupportedError("HWP 3.x (Hangul 97 and older) binary documents are not supported")
    # Corporate / government DRM agents replace the whole file with their own encrypted container.
    # Best-effort recognition of the common Korean products by their header text (heuristic, no public fixtures).
    probe = bytes(data[:256])
    if any(mark in probe for mark in _DRM_WRAPPER_MARKS):
        raise HwpDrmError("the file is wrapped by a document-security (DRM) product and is not a readable HWP/HWPX")
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text.startswith(b"<?xml") or text.startswith(b"<HWPML"):
        if b"<HWPML" in bytes(data[:4096]):
            raise HwpUnsupportedError("HWPML (.hml / XML flavoured .hwp) documents are not supported")
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    raise HwpFormatError("%s is neither a HWP 5.x (OLE2) nor a HWPX (zip) file"
                         % (("the ." + ext + " file") if ext else "the file"))


def inspect_document(data, filename="", raster_only=True, include_orphans=False):
    """Analyse a Hangul document and return a dict:

        format      'hwp' | 'hwpx'
        version     file-format version string
        images      [(name, bytes)]   usable pictures, in document order, extension = real type
        skipped     [(name, reason)]  OLE objects, vector pictures (WMF/EMF/SVG), unknown data
        linked      [path]            pictures that are linked, not embedded
        orphans     [name]            BinData entries nothing refers to (not returned unless include_orphans)
        order       'document' | 'table' | 'stream'  how `images` was ordered

    Raises the HwpError family for unreadable / protected documents (but not HwpNoImagesError).
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes) or len(data) < 8:
        raise HwpFormatError("empty or truncated file")
    kind = _detect(data, filename)
    try:
        info = _inspect_hwp5(data) if kind == "hwp" else _inspect_hwpx(data)
    except HwpError:
        raise
    except Exception as ex:                    # never leak a random parser exception to the UI
        raise HwpFormatError("damaged %s document: %s: %s" % (kind.upper(), type(ex).__name__, ex))
    candidates = list(info.pop("images"))
    orphan_data = info.pop("_orphan_data", [])
    if include_orphans:
        candidates += orphan_data
    images, seen = [], set()
    for name, blob in candidates:
        kind_ = sniff_image_type(blob)
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if kind_ is None:
            reason = "OLE object" if blob[:8] == _OLE_MAGIC or blob[4:12] == _OLE_MAGIC else "not a known picture format"
            info["skipped"].append((name, reason))
            continue
        if raster_only and kind_ not in RASTER_TYPES:
            info["skipped"].append((name, "vector picture (%s)" % kind_.upper()))
            continue
        out_name = "%s.%s" % (stem, kind_)
        if out_name in seen:
            continue
        seen.add(out_name)
        images.append((out_name, blob))
    info["images"] = images
    return info


def extract_images(data, filename="", raster_only=True, include_orphans=False):
    """Return [(name, image_bytes), ...] for the pictures embedded in a .hwp / .hwpx document.

    data      the whole file as bytes
    filename  original file name (only used in messages; the format is detected from the bytes)
    raster_only      True: JPEG/PNG/GIF/BMP/TIFF/WebP only.  False: also WMF/EMF/SVG bytes.
    include_orphans  True: also return BinData entries that nothing in the document refers to.

    Raises HwpPasswordError / HwpDrmError / HwpDistributionError for protected documents,
    HwpUnsupportedError for HWP 3.x and HWPML, HwpFormatError for anything that is not a
    Hangul document, and HwpNoImagesError (with .linked and .skipped) when nothing usable is embedded.
    """
    info = inspect_document(data, filename, raster_only=raster_only, include_orphans=include_orphans)
    if not info["images"]:
        raise HwpNoImagesError(linked=info["linked"], skipped=info["skipped"])
    return info["images"]
