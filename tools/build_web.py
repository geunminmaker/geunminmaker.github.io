"""Prepare ``web/`` for serving: package the Python engine and write the asset manifest.

    python tools/build_web.py

* ``web/py/splitcore.zip``  - the engine source, loaded by the worker
* ``web/assets.json``       - version + size of every engine file (download progress)
* ``web/sw.js``             - the service worker gets the build version and file list stamped in

The build is deterministic: the same sources give the same version string, so
phones only re-download when something really changed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
ENGINE = os.path.join(ROOT, "splitcore")

#: engine modules that are never needed in the browser
SKIP = {"cli.py", "__main__.py"}

#: files fetched while the engine starts (their sizes drive the first-run progress bar)
BOOT_FILES = [
    "vendor/pyodide/pyodide.asm.wasm",
    "vendor/pyodide/python_stdlib.zip",
    "vendor/pyodide/pyodide-lock.json",
    "py/splitcore.zip",
]

#: app shell: cached when the service worker installs, so the app opens offline
SHELL_DIRS = ["css", "js", "worker", "icons"]
SHELL_FILES = ["index.html", "licenses.html", "manifest.webmanifest", "assets.json"]


def build_engine_zip() -> bytes:
    """Zip the package with fixed timestamps (deterministic bytes)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, dirs, files in os.walk(ENGINE):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if not name.endswith(".py") or name in SKIP:
                    continue
                path = os.path.join(folder, name)
                arc = os.path.relpath(path, ROOT).replace(os.sep, "/")
                info = zipfile.ZipInfo(arc, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(path, "rb") as handle:
                    archive.writestr(info, handle.read().replace(b"\r\n", b"\n"))
    return buffer.getvalue()


def sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def walk(rel_dir: str) -> list[str]:
    out = []
    base = os.path.join(WEB, rel_dir)
    for folder, dirs, files in os.walk(base):
        dirs.sort()
        for name in sorted(files):
            out.append(os.path.relpath(os.path.join(folder, name), WEB).replace(os.sep, "/"))
    return out


def main() -> int:
    os.makedirs(os.path.join(WEB, "py"), exist_ok=True)
    engine_zip = build_engine_zip()
    target = os.path.join(WEB, "py", "splitcore.zip")
    if not os.path.exists(target) or open(target, "rb").read() != engine_zip:
        with open(target, "wb") as handle:
            handle.write(engine_zip)

    with open(os.path.join(WEB, "vendor", "pyodide", "pyodide-lock.json"), encoding="utf-8") as handle:
        lock = json.load(handle)
    packages = {
        key: {"file": "vendor/pyodide/" + entry["file_name"],
              "size": os.path.getsize(os.path.join(WEB, "vendor", "pyodide", entry["file_name"]))}
        for key, entry in lock["packages"].items()
    }
    boot = [{"file": f, "size": os.path.getsize(os.path.join(WEB, f))} for f in BOOT_FILES]
    boot.append(packages["pillow"])

    engine_files = sorted(set(walk("vendor") + walk("py")))
    shell = sorted(set(f for d in SHELL_DIRS for f in walk(d)) | {f for f in SHELL_FILES if f != "assets.json"
                                                                    and os.path.exists(os.path.join(WEB, f))})
    # version = hash over every served file except the generated ones
    digest = hashlib.sha256()
    hashes = {rel: sha(os.path.join(WEB, rel)) for rel in sorted(set(shell + engine_files))}
    for rel, value in hashes.items():
        digest.update(rel.encode("utf-8"))
        digest.update(bytes.fromhex(value))
    with open(os.path.join(WEB, "sw.js"), encoding="utf-8") as handle:
        sw_source = handle.read()
    sw_body = re.sub(r"/\* BUILD:START \*/.*?/\* BUILD:END \*/", "", sw_source, flags=re.S)
    digest.update(sw_body.encode("utf-8"))
    version = digest.hexdigest()[:12]

    sys.path.insert(0, ROOT)
    from splitcore import __version__

    assets = {
        "version": version,
        "app_version": __version__,
        "python": lock["info"]["python"],
        "boot": boot,
        "boot_bytes": sum(item["size"] for item in boot),
        "packages": packages,
    }
    with open(os.path.join(WEB, "assets.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(assets, handle, ensure_ascii=False, indent=1)

    stamp = (
        "/* BUILD:START */\n"
        f"const BUILD = {json.dumps(version)};\n"
        f"const SHELL = {json.dumps(sorted(shell + ['assets.json']), ensure_ascii=False)};\n"
        # path -> sha256: the worker stores an engine file only when it is exactly this one
        f"const ENGINE = {json.dumps({rel: hashes[rel] for rel in engine_files}, ensure_ascii=False)};\n"
        "/* BUILD:END */"
    )
    stamped = re.sub(r"/\* BUILD:START \*/.*?/\* BUILD:END \*/", lambda _m: stamp, sw_source, flags=re.S)
    if stamped != sw_source:
        with open(os.path.join(WEB, "sw.js"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(stamped)

    total = sum(os.path.getsize(os.path.join(WEB, f)) for f in set(shell + engine_files))
    print(f"build {version}  app {__version__}  engine zip {len(engine_zip)} B  "
          f"first run {assets['boot_bytes'] / 1e6:.1f} MB  site {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
