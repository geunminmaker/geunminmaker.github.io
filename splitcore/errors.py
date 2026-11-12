"""Typed errors shared by the engine, the CLI and the web worker.

Every failure the user can run into has a stable ``code`` so the UI can show a
specific, friendly message and a sensible recovery action.
"""
from __future__ import annotations

# code -> default Korean message (the web UI may override the wording)
MESSAGES = {
    "UNSUPPORTED": "지원하지 않는 파일 형식이에요.",
    "CORRUPT": "파일이 손상되었거나 읽을 수 없어요.",
    "EMPTY_FILE": "빈 파일이에요.",
    "ENCRYPTED": "암호가 걸린 문서예요.",
    "WRONG_PASSWORD": "암호가 맞지 않아요.",
    "PROTECTED": "보안(배포용/DRM) 문서라서 이미지를 꺼낼 수 없어요.",
    "NO_IMAGE": "문서에서 이미지를 찾지 못했어요.",
    "TOO_LARGE": "파일이 너무 커서 이 기기에서 처리할 수 없어요.",
    "IMAGE_TOO_LARGE": "이미지 해상도가 너무 커서 처리할 수 없어요.",
    "IMAGE_TOO_SMALL": "이미지가 너무 작아서 이만큼 나눌 수 없어요.",
    "BAD_LAYOUT": "이 분할 방식은 사용할 수 없어요.",
    "BAD_FORMAT": "지원하지 않는 저장 형식이에요.",
    "NOT_FOUND": "선택한 이미지를 찾을 수 없어요. 파일을 다시 선택해 주세요.",
    "OUT_OF_MEMORY": "메모리가 부족해요. 더 작은 이미지나 적은 분할 수로 시도해 주세요.",
    "INTERNAL": "처리 중 문제가 생겼어요.",
    # not shown to people: the web app loads the named package (detail) and retries
    "NEED_PACKAGE": "추가 구성 요소가 필요해요.",
}


class SplitError(Exception):
    """An expected, user-explainable failure."""

    def __init__(self, code: str, message: str | None = None, detail: str | None = None):
        if code not in MESSAGES:
            detail = f"{code}: {detail}" if detail else code
            code = "INTERNAL"
        self.code = code
        self.message = message or MESSAGES[code]
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SplitError({self.code!r}, {self.message!r}, detail={self.detail!r})"
