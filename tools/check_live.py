"""Check the published site: every file answers, has the right type, and matches the service worker's hashes.

    python tools/check_live.py [https://geunminmaker.github.io]

Exit code 0 when everything is right.  Uses only the standard library.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else "https://geunminmaker.github.io").rstrip("/")
APP = ORIGIN + "/split-image/"

EXPECTED_TYPES = {
    ".wasm": "application/wasm",
    ".mjs": "javascript",
    ".js": "javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "json",
    ".webmanifest": "manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

problems: list[str] = []


def get(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "split-image-check", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return response.status, response.headers, body
    except urllib.error.HTTPError as error:
        return error.code, error.headers, b""


def check(url: str, kind: str | None = None):
    status, headers, body = get(url)
    content_type = headers.get("Content-Type", "")
    if status != 200:
        problems.append(f"{status} {url}")
    elif kind and kind not in content_type:
        problems.append(f"type {content_type!r} (wanted {kind}) {url}")
    return status, headers, body


def main() -> int:
    status, headers, body = check(APP, "text/html")
    print(f"app page        {status}  {len(body)} B  cache-control: {headers.get('Cache-Control')}")
    status, _headers, body = get(ORIGIN + "/")
    print(f"root page       {status}  redirects to the app: {b'split-image/' in body}")
    if b"split-image/" not in body:
        problems.append("root page does not lead to the app")

    status, headers, body = check(ORIGIN + "/.well-known/assetlinks.json", "json")
    links = json.loads(body) if status == 200 else []
    print(f"assetlinks.json {status}  {headers.get('Content-Type')}  packages: {[item['target']['package_name'] for item in links]}")

    status, _headers, body = check(APP + "manifest.webmanifest", "manifest+json")
    manifest = json.loads(body) if status == 200 else {}
    print(f"manifest        {status}  id={manifest.get('id')} start_url={manifest.get('start_url')} scope={manifest.get('scope')}")
    for key, wanted in (("id", "/split-image/"), ("scope", "/split-image/"), ("start_url", "/split-image/?source=pwa")):
        if manifest.get(key) != wanted:
            problems.append(f"manifest {key} = {manifest.get(key)!r}")

    status, _headers, body = check(APP + "sw.js", "javascript")
    text = body.decode("utf-8")
    build = re.search(r'const BUILD = "([^"]*)";', text).group(1)
    shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", text).group(1))
    engine = json.loads(re.search(r"const ENGINE = (\{.*?\});", text).group(1))
    print(f"service worker  {status}  build {build}  shell {len(shell)} files  engine {len(engine)} files")

    for path in shell:
        ext = "." + path.rsplit(".", 1)[-1]
        check(APP + path, EXPECTED_TYPES.get(ext))
    total = 0
    for path, wanted in engine.items():
        ext = "." + path.rsplit(".", 1)[-1]
        status, headers, body = check(APP + path, EXPECTED_TYPES.get(ext))
        total += len(body)
        actual = hashlib.sha256(body).hexdigest()
        mark = "ok " if actual == wanted else "BAD"
        if actual != wanted:
            problems.append(f"hash of {path} differs from the service worker's")
        print(f"  {mark} {path:78s} {len(body) / 1e6:6.2f} MB  {headers.get('Content-Type')}  enc={headers.get('Content-Encoding')}")
    print(f"engine total {total / 1e6:.1f} MB")

    status, _headers, _body = get(APP + "no-such-file.txt")
    print(f"missing file    {status}")
    if status != 404:
        problems.append("a missing file does not answer 404")

    print("PROBLEMS:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
