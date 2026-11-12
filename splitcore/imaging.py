"""Raster helpers: safe decoding, normalisation, previews, tile rendering, encoders.

Only Pillow is required, so this module works unchanged under Pyodide.
"""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

from .errors import SplitError

#: Refuse to decode anything larger than this many pixels (decompression-bomb guard).
MAX_SOURCE_PIXELS = 120_000_000
#: Upper bound for one enlarged tile.  25 MP covers 24 MP phone photos at full size.
MAX_TILE_PIXELS = 25_000_000
#: Frames/pages taken from one multi-frame image file (TIFF).
MAX_FRAMES = 60

# Pillow raises DecompressionBombError above 2x this value and warns above 1x.
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS

RESAMPLE_UP = Image.LANCZOS
RESAMPLE_DOWN = Image.LANCZOS

_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"II+\x00", "tiff"),
    (b"MM\x00+", "tiff"),
    (b"8BPS", "psd"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jp2"),
    (b"\xff\x4f\xff\x51", "jp2"),
)


def sniff_image(data: bytes) -> str | None:
    """Identify a raster/vector image by its first bytes (None when unknown)."""
    head = bytes(data[:64])
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
    probe = bytes(data[:1024]).lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if probe.startswith(b"<svg") or (probe.startswith(b"<?xml") and b"<svg" in probe):
        return "svg"
    return None


def _to_8bit_gray(im: Image.Image) -> Image.Image:
    """16/32-bit integer or float grayscale -> 8-bit, scaled (Pillow would clip)."""
    if im.mode.startswith("I;16"):
        im = im.convert("I")
    lo, hi = im.getextrema()
    if im.mode == "I" and 0 <= lo and hi <= 65535 and hi > 255:
        return im.point(lambda v: v * (1.0 / 257.0)).convert("L")
    if hi <= lo:
        return im.convert("L")
    if lo >= 0 and hi <= 255 and im.mode == "I":
        return im.convert("L")
    scale = 255.0 / (hi - lo)
    offset = -lo * scale
    return im.point(lambda v: v * scale + offset).convert("L")


def _palette_has_alpha(im: Image.Image) -> bool:
    flag = getattr(im, "has_transparency_data", None)
    if flag is not None:
        return bool(flag)
    return "transparency" in im.info


def normalize(im: Image.Image, *, first_frame: bool = True) -> Image.Image:
    """Return an upright, fully loaded copy in mode L, RGB or RGBA.

    * EXIF orientation is applied (phone photos).
    * Animated images contribute their first frame.
    * CMYK / palette / 1-bit / 16-bit data become 8-bit RGB(A) or L.
    * An alpha channel that is fully opaque is dropped.

    ``info['icc_profile']`` and ``info['dpi']`` are carried over when they are
    still valid for the converted data.
    """
    try:
        if first_frame:
            try:
                im.seek(0)
            except (EOFError, ValueError):
                pass
        original_mode = im.mode
        icc = im.info.get("icc_profile")
        dpi = im.info.get("dpi")
        im.load()
        im = ImageOps.exif_transpose(im) or im

        mode = im.mode
        if mode in ("RGB", "L", "RGBA"):
            out = im
        elif mode in ("LA", "La", "PA", "RGBa"):
            out = im.convert("RGBA")
        elif mode == "P":
            out = im.convert("RGBA" if _palette_has_alpha(im) else "RGB")
        elif mode == "1":
            out = im.convert("L")
        elif mode in ("I", "F") or mode.startswith("I;16"):
            out = _to_8bit_gray(im)
        else:  # CMYK, YCbCr, LAB, HSV, RGBX, ...
            out = im.convert("RGB")

        if out.mode == "RGBA":
            lo, _hi = out.getchannel("A").getextrema()
            if lo == 255:
                out = out.convert("RGB")

        if out is im:
            out = im.copy()
        # A colour profile only stays meaningful if the colour model did not change.
        keep_icc = icc and original_mode in ("RGB", "RGBA", "RGBX", "P", "PA") and out.mode in ("RGB", "RGBA")
        keep_icc = keep_icc or (icc and original_mode in ("L", "LA") and out.mode in ("L", "RGBA"))
        out.info = {}
        if keep_icc:
            out.info["icc_profile"] = icc
        if dpi and _valid_dpi(dpi):
            out.info["dpi"] = (float(dpi[0]), float(dpi[1]))
        return out
    except SplitError:
        raise
    except Image.DecompressionBombError as exc:
        raise SplitError("IMAGE_TOO_LARGE", detail=str(exc)) from None
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    except Exception as exc:  # truncated/garbled pixel data
        raise SplitError("CORRUPT", detail=f"{type(exc).__name__}: {exc}") from None


def _valid_dpi(dpi) -> bool:
    try:
        return len(dpi) == 2 and all(1 <= float(v) <= 20000 for v in dpi)
    except (TypeError, ValueError):
        return False


#: Only a successful registration is remembered.  A failed attempt is not: in the browser
#: pillow-heif is downloaded on demand, so the import that fails for a HEIC inside a ZIP must
#: be tried again once the page has loaded the package.
_heif_state = {"ok": False}


def enable_heif() -> bool:
    """Register the optional HEIC decoder (pillow-heif) when it is installed."""
    if not _heif_state["ok"]:
        try:
            import pillow_heif  # type: ignore

            # libheif's tile decoder starts threads, which WebAssembly builds cannot do:
            # without this the first large HEIC kills the whole Python runtime in the browser.
            pillow_heif.options.DECODE_THREADS = 0
            pillow_heif.register_heif_opener()
            _heif_state["ok"] = True
        except Exception:
            pass  # not there (yet), or unusable: asked again for the next HEIC
    return _heif_state["ok"]


def heif_missing() -> bool:
    """True when pillow-heif is not installed (in the browser: not downloaded yet)."""
    try:
        import pillow_heif  # type: ignore  # noqa: F401
    except ImportError:
        return True
    except Exception:  # there, but broken: asking for the package again would not help
        return False
    return False


def open_image(data: bytes) -> Image.Image:
    """Open (lazily) with all the safety checks; pixel data is not decoded yet."""
    if not data:
        raise SplitError("EMPTY_FILE")
    if sniff_image(data) == "heic":
        enable_heif()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            width, height = im.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise SplitError("IMAGE_TOO_LARGE", detail=str(exc)) from None
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    except Exception as exc:
        kind = sniff_image(data)
        if kind is None:
            raise SplitError("UNSUPPORTED", detail=f"{type(exc).__name__}: {exc}") from None
        # A known image type that this build of Pillow cannot decode (HEIC, AVIF, SVG ...)
        # is reported as unsupported so the web app can try the browser's decoder;
        # a known, decodable type that still failed is damaged.
        if kind in ("heic", "avif", "svg", "jp2"):
            raise SplitError("UNSUPPORTED", detail=f"image/{kind}") from None
        raise SplitError("CORRUPT", detail=f"{kind}: {type(exc).__name__}: {exc}") from None
    if width < 1 or height < 1:
        raise SplitError("CORRUPT", detail="zero-sized image")
    if width * height > MAX_SOURCE_PIXELS:
        raise SplitError("IMAGE_TOO_LARGE", detail=f"{width}x{height}")
    return im


def frame_total(im: Image.Image) -> int:
    """All pages of a multi-page TIFF; animations (GIF/WebP/APNG) count as one picture."""
    if (im.format or "").upper() == "TIFF":
        try:
            return max(1, int(getattr(im, "n_frames", 1)))
        except Exception:
            return 1
    return 1


def frame_count(im: Image.Image) -> int:
    """Pages that are offered from a multi-page TIFF (at most MAX_FRAMES)."""
    return min(frame_total(im), MAX_FRAMES)


def decode(data: bytes, frame: int = 0) -> Image.Image:
    """Bytes -> normalised image (see :func:`normalize`)."""
    im = open_image(data)
    if frame:
        try:
            im.seek(frame)
        except Exception as exc:
            raise SplitError("CORRUPT", detail=f"frame {frame}: {exc}") from None
        return normalize(im, first_frame=False)
    return normalize(im)


def decode_reduced(data: bytes, max_px: int, frame: int = 0) -> Image.Image:
    """Decode a small version quickly (JPEG DCT scaling) for thumbnails."""
    im = open_image(data)
    if frame == 0 and (im.format or "").upper() == "JPEG":
        try:
            im.draft("RGB", (max_px, max_px))
        except Exception:
            pass
        return fit_within(normalize(im), max_px)
    return fit_within(decode(data, frame), max_px)


def fit_within(im: Image.Image, max_px: int) -> Image.Image:
    """Downscale so the longest side is at most ``max_px`` (never upscales)."""
    w, h = im.size
    longest = max(w, h)
    if longest <= max_px:
        return im
    scale = max_px / longest
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    out = im.resize(size, RESAMPLE_DOWN, reducing_gap=2.0)
    out.info = dict(im.info)
    return out


def has_alpha(im: Image.Image) -> bool:
    return im.mode == "RGBA"


def flatten(im: Image.Image, background=(255, 255, 255)) -> Image.Image:
    """Composite transparency onto a solid colour (for JPEG / paper)."""
    if im.mode != "RGBA":
        return im
    canvas = Image.new("RGB", im.size, background)
    canvas.paste(im, mask=im.getchannel("A"))
    canvas.info = {k: v for k, v in im.info.items() if k in ("icc_profile", "dpi")}
    return canvas


def render_tile(im: Image.Image, box, out_size) -> Image.Image:
    """Cut ``box`` (float source pixels) out of ``im`` and scale it to ``out_size``.

    The resampling filter may read a few source pixels beyond the box, which is
    exactly what makes neighbouring tiles line up seamlessly when the printed
    sheets are put back together (same result as enlarging the whole picture
    first and cutting afterwards).
    """
    x0, y0, x1, y1 = box
    w, h = im.size
    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
    x1, y1 = min(float(w), float(x1)), min(float(h), float(y1))
    if x1 <= x0 or y1 <= y0:
        raise SplitError("IMAGE_TOO_SMALL", detail=f"empty tile box {box}")
    out_w, out_h = int(out_size[0]), int(out_size[1])
    upscaling = out_w >= (x1 - x0) or out_h >= (y1 - y0)
    try:
        tile = im.resize((out_w, out_h), RESAMPLE_UP if upscaling else RESAMPLE_DOWN, box=(x0, y0, x1, y1))
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    tile.info = {k: v for k, v in im.info.items() if k in ("icc_profile", "dpi")}
    return tile


def encode_jpeg(im: Image.Image, quality: int = 92, dpi=None) -> bytes:
    im = flatten(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    params = {"quality": int(quality)}
    if quality >= 90:
        params["subsampling"] = 0  # 4:4:4 keeps enlarged text and line art crisp
    icc = im.info.get("icc_profile")
    if icc:
        params["icc_profile"] = icc
    dpi = dpi or im.info.get("dpi")
    if dpi and _valid_dpi(dpi):
        params["dpi"] = (round(dpi[0]), round(dpi[1]))
    buf = io.BytesIO()
    try:
        im.save(buf, "JPEG", **params)
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    return buf.getvalue()


def encode_png(im: Image.Image, compress_level: int | None = None, dpi=None) -> bytes:
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    if compress_level is None:
        # big tiles: level 1 is about 40 % faster in the browser for files only a few % larger
        compress_level = 3 if im.size[0] * im.size[1] <= 4_000_000 else 1
    params = {"compress_level": int(compress_level)}
    icc = im.info.get("icc_profile")
    if icc:
        params["icc_profile"] = icc
    dpi = dpi or im.info.get("dpi")
    if dpi and _valid_dpi(dpi):
        params["dpi"] = (dpi[0], dpi[1])
    buf = io.BytesIO()
    try:
        im.save(buf, "PNG", **params)
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    return buf.getvalue()


def encode_preview(im: Image.Image, max_px: int, quality: int = 85) -> tuple[bytes, str]:
    """Small picture for the UI: JPEG, or PNG when transparency must survive."""
    small = fit_within(im, max_px)
    if small.mode == "RGBA":
        return encode_png(small, compress_level=1), "image/png"
    preview = small.copy()
    preview.info.pop("dpi", None)
    return encode_jpeg(preview, quality=quality), "image/jpeg"
