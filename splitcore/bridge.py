"""Glue between the Web Worker (JavaScript) and the engine.  Pyodide only.

The worker calls ``dispatch(method, args, callbacks)`` and always gets a plain
dict back: ``{"ok": True, ...}`` or ``{"ok": False, "error": {code, message,
detail}}``.  Binary results are returned as ``bytes`` (the worker copies them
out of the WebAssembly heap once and transfers the buffer to the page).
"""
from __future__ import annotations

import traceback

from . import __version__, imaging, loaders
from .errors import SplitError
from .export import FORMATS
from .grids import SPLIT_COUNTS
from .session import Session

try:  # only available inside Pyodide
    from pyodide.ffi import JsProxy  # type: ignore
except ImportError:  # pragma: no cover - lets the module be imported natively for tests
    JsProxy = ()  # type: ignore

_BUFFER_TYPES = {"Uint8Array", "Uint8ClampedArray", "ArrayBuffer", "Int8Array", "DataView"}

_session = Session()


def _is_null(value) -> bool:
    """None, or JavaScript ``null`` (recent Pyodide passes it as a distinct ``JsNull`` object)."""
    return value is None or type(value).__name__ == "JsNull"


def _from_js(value):
    """JS -> Python: typed arrays become bytes, objects dicts, arrays lists, null None."""
    if _is_null(value):
        return None
    if JsProxy and isinstance(value, JsProxy):
        if value.typeof == "function":
            return value
        name = value.constructor.name
        if name in _BUFFER_TYPES:
            return value.to_bytes()
        if name == "Array":
            return [_from_js(item) for item in value]
        if name == "Object":
            return {key: _from_js(item) for key, item in value.object_entries()}
        return value.to_py()
    return value


# ---------------------------------------------------------------- methods

def info() -> dict:
    return {
        "version": __version__,
        "counts": list(SPLIT_COUNTS),
        "formats": [{"key": key, **{k: v for k, v in spec.items()}} for key, spec in FORMATS.items()],
    }


def probe(name: str, head: bytes) -> dict:
    """Which optional Python packages this file needs (decided from its first bytes)."""
    kind = loaders.detect(name, head)
    packages: list[str] = []
    if kind == "pdf":
        packages.append("pymupdf")
    elif kind == "ole":
        packages.append("olefile")
    elif kind == "image" and imaging.sniff_image(head) == "heic":
        packages.append("pillow-heif")
    return {"kind": kind, "packages": packages}


def add_file(name: str, data: bytes, password=None) -> dict:
    return _session.add_file(name, data, password or None)


def clear() -> dict:
    _session.clear()
    return {}


def sources() -> dict:
    return {"sources": _session.sources()}


def thumbnail(id: str, max_px: int = 360) -> dict:
    data, mime = _session.thumbnail(id, int(max_px))
    return {"mime": mime, "bytes": data}


def preview(id: str, max_px: int = 1600) -> dict:
    data, mime = _session.preview(id, int(max_px))
    width, height = _session.get(id).size
    return {"mime": mime, "bytes": data, "width": width, "height": height}


def layouts(id: str) -> dict:
    return {"layouts": _session.all_layouts(id)}


def describe(id: str, rows: int, cols: int, format=None, options=None) -> dict:
    return _session.describe(id, int(rows), int(cols), format or None, options or {})


def export(id: str, rows: int, cols: int, format: str, options=None, emit=None, progress=None) -> dict:
    """Run an export.  Every finished file is handed to ``emit(name, mime, bytes)``."""
    files = []
    for output in _session.export(id, int(rows), int(cols), format, options or {}, progress):
        files.append({"name": output.name, "mime": output.mime, "size": output.size})
        if emit is not None:
            emit(output.name, output.mime, output.data)
        del output
    return {"files": files, "plan": _session.describe(id, int(rows), int(cols), None, options or {})["plan"]}


_METHODS = {
    "info": info, "probe": probe, "add_file": add_file, "clear": clear, "sources": sources,
    "thumbnail": thumbnail, "preview": preview, "layouts": layouts, "describe": describe,
    "export": export,
}


def dispatch(method: str, js_args=None, js_callbacks=None) -> dict:
    try:
        handler = _METHODS.get(method)
        if handler is None:
            raise SplitError("INTERNAL", detail=f"unknown method {method!r}")
        kwargs = _from_js(js_args) or {}
        if method == "export" and not _is_null(js_callbacks):
            for name in ("emit", "progress"):
                callback = getattr(js_callbacks, name, None)
                if callback is not None:
                    kwargs[name] = callback
        result = handler(**kwargs) or {}
        result["ok"] = True
        return result
    except SplitError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except MemoryError:
        return {"ok": False, "error": SplitError("OUT_OF_MEMORY").to_dict()}
    except ImportError as exc:
        return {"ok": False, "error": SplitError("INTERNAL", detail=f"missing package: {exc}").to_dict()}
    except Exception as exc:  # noqa: BLE001 - nothing may escape to JS as a raw traceback
        tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-3:])
        return {"ok": False, "error": SplitError("INTERNAL", detail=tail[-600:]).to_dict()}
