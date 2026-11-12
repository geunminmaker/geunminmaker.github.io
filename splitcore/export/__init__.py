"""Export formats.

``export()`` is a generator: picture formats yield one file per tile, document
formats yield a single file.  Format modules are imported lazily because some
of them pull in heavy libraries that the web app only downloads when needed.
"""
from __future__ import annotations

from typing import Iterator

from ..errors import SplitError
from ..splitter import SplitPlan, output_stem, page_spec
from .base import OutputFile, Progress

#: key -> description used by the UI and the CLI
FORMATS = {
    "pdf": {"label": "PDF", "ext": "pdf", "kind": "document",
            "mime": "application/pdf"},
    "jpg": {"label": "JPG", "ext": "jpg", "kind": "images", "mime": "image/jpeg"},
    "png": {"label": "PNG", "ext": "png", "kind": "images", "mime": "image/png"},
    "docx": {"label": "Word", "ext": "docx", "kind": "document",
             "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "pptx": {"label": "PowerPoint", "ext": "pptx", "kind": "document",
             "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "hwpx": {"label": "한글", "ext": "hwpx", "kind": "document",
             "mime": "application/hwp+zip"},
}

_ALIASES = {"jpeg": "jpg", "word": "docx", "doc": "docx", "ppt": "pptx", "powerpoint": "pptx",
            "hwp": "hwpx", "hangul": "hwpx", "한글": "hwpx"}

def normalize_format(fmt: str) -> str:
    key = str(fmt or "").strip().lower().lstrip(".")
    key = _ALIASES.get(key, key)
    if key not in FORMATS:
        raise SplitError("BAD_FORMAT", detail=f"format={fmt!r}")
    return key


def export(source, plan: SplitPlan, fmt: str, options: dict | None = None,
           progress: Progress = None) -> Iterator[OutputFile]:
    """Produce the output file(s) for one split."""
    fmt = normalize_format(fmt)
    options = dict(options or {})
    stem = output_stem(source.origin, plan, source.label)
    info = FORMATS[fmt]

    if info["kind"] == "images":
        from .images import export_images
        yield from export_images(source, plan, fmt, stem, options, progress)
        return

    spec = page_spec(plan, options.get("paper") or "original", options.get("margin_mm"),
                     physical_mm=source.physical_mm)
    name = f"{stem}.{info['ext']}"
    if fmt == "pdf":
        from .pdf import export_pdf
        data = export_pdf(source, plan, spec, options, progress, title=stem)
    elif fmt == "docx":
        from .docx import export_docx
        data = export_docx(source, plan, spec, options, progress, title=stem)
    elif fmt == "pptx":
        from .pptx import export_pptx
        data = export_pptx(source, plan, spec, options, progress, title=stem)
    else:
        from .hwpx import export_hwpx
        data = export_hwpx(source, plan, spec, options, progress, title=stem)
    yield OutputFile(name, info["mime"], data)
