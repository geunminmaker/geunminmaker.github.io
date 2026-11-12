"""Fixtures for the browser tests: a local server for ``web/`` and two phone profiles.

    .venv/Scripts/python -m pytest tests/e2e -q            # both engines
    .venv/Scripts/python -m pytest tests/e2e -q -k android # Chromium / Pixel 7 only
    .venv/Scripts/python -m pytest tests/e2e -q -k iphone  # WebKit / iPhone 13 only

"android" = Playwright Chromium with the Pixel 7 profile (same engine as Chrome
and the installed Android app).  "iphone" = Playwright WebKit with the iPhone 13
profile (Safari's engine; not identical to Safari on a real iPhone).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright  # noqa: E402
from serve import serve_in_thread  # noqa: E402

CORPUS = os.path.join(ROOT, "tests", "corpus")
OUT = os.path.join(ROOT, "tests", "e2e", "out")

# navigator.share does not exist in Playwright's WebKit; the app's iOS path is exercised
# by capturing what it hands to the share sheet.
SHARE_STUB = """
(() => {
  window.__shared = [];
  const nav = Navigator.prototype;
  Object.defineProperty(nav, 'canShare', { configurable: true, value: (data) => !!(data && data.files && data.files.length) });
  Object.defineProperty(nav, 'share', { configurable: true, value: async (data) => {
    if (!data || !data.files || !data.files.length) throw new TypeError('no files');
    if (data.title || data.text || data.url) window.__shareMixed = true;
    window.__shared.push(Array.from(data.files));
  } });
})();
"""

PROFILES = {
    "android": {"engine": "chromium", "device": "Pixel 7", "share_stub": False},
    "iphone": {"engine": "webkit", "device": "iPhone 13", "share_stub": True},
}


def pytest_collection_modifyitems(items):
    # run the fast Chromium profile first
    items.sort(key=lambda item: 0 if "android" in item.name else 1)


@pytest.fixture(scope="session", autouse=True)
def built_site():
    """The engine bundle must match the sources."""
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_web.py")], check=True, cwd=ROOT,
                   stdout=subprocess.DEVNULL)
    if not os.path.exists(os.path.join(CORPUS, "manifest.json")):
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_corpus.py")], check=True, cwd=ROOT)


@pytest.fixture(scope="session")
def base_url(built_site):
    server, url = serve_in_thread(os.path.join(ROOT, "web"))
    yield url
    server.shutdown()


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browsers(playwright_instance):
    opened = {}

    def get(engine: str):
        if engine not in opened:
            opened[engine] = getattr(playwright_instance, engine).launch()
        return opened[engine]

    yield get
    for browser in opened.values():
        browser.close()


@pytest.fixture(params=list(PROFILES))
def profile(request):
    return request.param


@pytest.fixture()
def app(request, profile, browsers, playwright_instance, base_url):
    """A fresh page of the app in the given phone profile."""
    from app_driver import AppDriver

    spec = PROFILES[profile]
    options = dict(playwright_instance.devices[spec["device"]])
    options["accept_downloads"] = True
    options["locale"] = "ko-KR"
    context = browsers(spec["engine"]).new_context(**options)
    if spec["share_stub"]:
        context.add_init_script(SHARE_STUB)
    page = context.new_page()
    driver = AppDriver(page, base_url, profile, shots_dir=os.path.join(OUT, "shots", profile, request.node.name))
    yield driver
    problems = driver.console_problems()
    context.close()
    assert not problems, f"console problems: {problems[:5]}"
