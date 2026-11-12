"""Hangul documents (HWP 5.x binary and HWPX): embedded pictures in document order.

The low-level parsing lives in :mod:`hwp_reader`; this module turns its results
and errors into the engine's sources and user-facing messages.
"""
from __future__ import annotations

from ..errors import SplitError
from ..sources import Source
from . import hwp_reader
from .common import HINT_NO_PICTURE, HINT_PASSWORD_DOC, PictureCollector


def load_hangul(data: bytes, origin: str) -> list[Source]:
    try:
        images = hwp_reader.extract_images(data, origin)
    except hwp_reader.HwpPasswordError:
        raise SplitError("PROTECTED", HINT_PASSWORD_DOC, detail="hwp-password") from None
    except hwp_reader.HwpDistributionError:
        raise SplitError(
            "PROTECTED",
            "배포용 문서는 내용을 꺼낼 수 없어요. 원본 문서나 이미지 파일을 골라 주세요.",
            detail="hwp-distribution",
        ) from None
    except hwp_reader.HwpDrmError:
        raise SplitError("PROTECTED", "문서 보안(DRM)이 걸린 문서라서 열 수 없어요.", detail="hwp-drm") from None
    except hwp_reader.HwpProtectedError as exc:
        raise SplitError("PROTECTED", detail=f"hwp: {exc}") from None
    except hwp_reader.HwpNoImagesError as exc:
        if exc.linked:
            raise SplitError(
                "NO_IMAGE",
                "그림이 문서에 들어 있지 않고 '연결'만 되어 있어요. 한글에서 그림을 '문서에 포함'으로 "
                "다시 저장하거나 PDF로 저장해 주세요.",
                detail=f"hwp-linked={len(exc.linked)}",
            ) from None
        if exc.skipped:
            raise SplitError(
                "NO_IMAGE",
                "문서 속 그림이 벡터(WMF/EMF)나 OLE 개체라서 꺼낼 수 없어요. PDF로 저장한 뒤 그 PDF를 골라 주세요.",
                detail=f"hwp-skipped={len(exc.skipped)}",
            ) from None
        raise SplitError("NO_IMAGE", HINT_NO_PICTURE, detail="hwp-no-images") from None
    except hwp_reader.HwpUnsupportedError:
        raise SplitError(
            "UNSUPPORTED",
            "아주 오래된 한글 형식이라 열 수 없어요. 한글에서 PDF나 최신 형식(HWPX)으로 다시 저장해 주세요.",
            detail="hwp-old-format",
        ) from None
    except hwp_reader.HwpError as exc:
        raise SplitError("CORRUPT", detail=f"hwp: {exc}") from None
    except SplitError:
        raise
    except MemoryError:
        raise SplitError("OUT_OF_MEMORY") from None
    except Exception as exc:  # a damaged container can fail anywhere inside the parser
        raise SplitError("CORRUPT", detail=f"hwp: {type(exc).__name__}: {exc}") from None

    collector = PictureCollector(origin, "문서 속 이미지", numbered=True)
    for name, blob in images:
        collector.add(blob, name)
    return collector.result()
