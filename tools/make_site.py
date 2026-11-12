"""Assemble the folder that is published to GitHub Pages.

    python tools/make_site.py [_site]

Layout of the published site (https://<user>.github.io/):

    /split-image/...               the app (contents of web/)
    /.well-known/assetlinks.json   lets the Android APK open the app full screen
    /index.html                    redirects to /split-image/
    /.nojekyll                     serve files as they are (no Jekyll processing)
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = "split-image"

REDIRECT = """<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<title>조각조각 - 이미지 분할</title>
<meta name="robots" content="noindex">
<link rel="canonical" href="./split-image/">
<meta http-equiv="refresh" content="0; url=./split-image/">
<script>location.replace("./split-image/" + location.search + location.hash);</script>
<a href="./split-image/">조각조각 열기</a>
</html>
"""

NOT_FOUND = """<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>페이지를 찾을 수 없어요</title>
<body style="font:16px/1.6 -apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;margin:15vh 24px;text-align:center;color:#191F28">
<p style="font-size:20px;font-weight:700;margin:0 0 8px">페이지를 찾을 수 없어요</p>
<p style="margin:0 0 24px;color:#4E5968">주소가 바뀌었거나 잘못 입력되었어요.</p>
<a href="/split-image/" style="color:#1F6BF0;font-weight:600">조각조각 열기</a>
</body>
</html>
"""


def main() -> int:
    out = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "_site")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    shutil.copytree(os.path.join(ROOT, "web"), os.path.join(out, APP_PATH),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "Thumbs.db"))
    # The installed app is identified by its id: pin it (and the start URL) to the public path so
    # that later changes of query strings or file layout never look like a different app.
    manifest_path = os.path.join(out, APP_PATH, "manifest.webmanifest")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["id"] = f"/{APP_PATH}/"
        manifest["start_url"] = f"/{APP_PATH}/?source=pwa"
        manifest["scope"] = f"/{APP_PATH}/"
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(REDIRECT)
    with open(os.path.join(out, "404.html"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(NOT_FOUND)
    open(os.path.join(out, ".nojekyll"), "w").close()
    links = os.path.join(ROOT, "android", "assetlinks.json")
    if os.path.exists(links):
        os.makedirs(os.path.join(out, ".well-known"))
        shutil.copyfile(links, os.path.join(out, ".well-known", "assetlinks.json"))
    size = sum(os.path.getsize(os.path.join(folder, name)) for folder, _dirs, files in os.walk(out) for name in files)
    print(f"site -> {out}  ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
