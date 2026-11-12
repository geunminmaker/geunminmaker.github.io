"""Service worker: offline start, updates, and refusing engine files of another build.

These run in Chromium with the phone profile (a browser tab, not an installed app), against a
private copy of ``web/`` served with the caching headers of the real host (max-age=600).
"""
from __future__ import annotations

import collections
import hashlib
import io
import json
import os
import re
import shutil
import zipfile

import pytest

from app_driver import AppDriver
from conftest import CORPUS, OUT, ROOT
from serve import serve_in_thread
from verify import verify_outputs

PHOTO = os.path.join(CORPUS, "01_사진_가로_4032x3024.jpg")
PDF = os.path.join(CORPUS, "21_여러쪽_5쪽_크기제각각.pdf")
BOOT_NAMES = ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json",
              "splitcore.zip", "pillow-"]
KEYS = "() => caches.open('engine-v1').then((cache) => cache.keys()).then((keys) => keys.map((r) => r.url))"


class Site:
    """A private, editable copy of the built app with its own server."""

    def __init__(self, name: str):
        self.dir = os.path.join(OUT, "sites", name)
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)
        shutil.copytree(os.path.join(ROOT, "web"), self.dir)
        self.hits: collections.Counter = collections.Counter()
        self.server, self.url = serve_in_thread(self.dir, counter=self.hits, cache_control="max-age=600")

    def path(self, rel: str) -> str:
        return os.path.join(self.dir, *rel.split("/"))

    def read(self, rel: str) -> str:
        with io.open(self.path(rel), encoding="utf-8", newline="") as handle:
            return handle.read()

    def write(self, rel: str, text: str) -> None:
        with io.open(self.path(rel), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def engine_hashes(self) -> dict:
        return json.loads(re.search(r"const ENGINE = (\{.*?\});", self.read("sw.js")).group(1))

    def restamp(self, *, build: str | None = None, engine: dict | None = None) -> None:
        text = self.read("sw.js")
        if build:
            text = re.sub(r'const BUILD = "[^"]*";', f'const BUILD = "{build}";', text)
        if engine is not None:
            text = re.sub(r"const ENGINE = \{.*?\};", lambda _m: f"const ENGINE = {json.dumps(engine)};", text)
        self.write("sw.js", text)

    def go_down(self) -> None:
        """Take the server away (a real network failure, for browsers whose offline switch is too blunt)."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def close(self) -> None:
        self.go_down()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture()
def site(request):
    made = Site(request.node.name)
    yield made
    made.close()


@pytest.fixture()
def phone(browsers, playwright_instance, site):
    options = dict(playwright_instance.devices["Pixel 7"])
    options.update(accept_downloads=True, locale="ko-KR")
    context = browsers("chromium").new_context(**options)
    page = context.new_page()
    driver = AppDriver(page, site.url, "android")
    yield driver, context
    context.close()


def wait_engine(driver: AppDriver, state: str = "ready", timeout: float = 180_000) -> None:
    driver.page.wait_for_function("state => document.body.dataset.engine === state", arg=state, timeout=timeout)


def wait_keys(driver: AppDriver, names: list[str], timeout: float = 60_000) -> list[str]:
    driver.page.wait_for_function(
        """names => caches.open('engine-v1').then((cache) => cache.keys())
              .then((keys) => names.every((name) => keys.some((r) => r.url.includes(name))))""",
        arg=names, timeout=timeout)
    return driver.page.evaluate(KEYS)


def split_and_save(driver: AppDriver, path: str, count: int, layout: str, fmt: str) -> list[str]:
    screen = driver.pick_files([path])
    assert screen == "confirm", (screen, driver.error_code())
    driver.confirm(True)
    assert driver.choose_split(count) == layout
    driver.split_yes()
    driver.choose_output(fmt, "original" if fmt not in ("jpg", "png") else None)
    files = driver.save()
    cols, rows = (int(v) for v in layout.split("x"))
    zipped = fmt in ("jpg", "png") and driver.profile != "iphone"   # the iPhone share sheet gets the images themselves
    return verify_outputs(path, files, rows=rows, cols=cols, fmt=fmt, paper="original", zipped=zipped)


def test_phone_tab_works_offline_after_one_visit(site, phone):
    driver, context = phone
    driver.open("")
    wait_engine(driver)
    keys = wait_keys(driver, BOOT_NAMES)
    assert all("?h=" in key for key in keys), keys
    assert not any("pymupdf" in key for key in keys), "a phone tab must not download the PDF tools unasked"
    wasm_hits = site.hits["/vendor/pyodide/pyodide.asm.wasm"]
    assert wasm_hits == 1, f"the engine was downloaded {wasm_hits} times on the first visit"

    context.set_offline(True)
    driver.page.reload()
    driver.wait_screen("home")
    wait_engine(driver)                                   # starts from the cache
    assert split_and_save(driver, PHOTO, 4, "2x2", "pdf") == []

    # PDF tools were never downloaded: opening a PDF offline must end in a clear error, not a hang
    driver.page.reload()
    driver.wait_screen("home")
    screen = driver.pick_files([PDF])
    assert screen == "error", screen
    assert driver.error_code() == "OFFLINE"

    context.set_offline(False)
    driver.click('[data-action="retry"]')
    assert driver.wait_screen("pick", "confirm", "error") == "pick", driver.error_code()
    assert site.hits["/vendor/pyodide/pyodide.asm.wasm"] == wasm_hits, "the engine must not be downloaded again"
    assert not [p for p in driver.console_problems() if "ERR_INTERNET_DISCONNECTED" not in p and "Failed to fetch" not in p
                and "Failed to load resource" not in p]


def test_update_replaces_only_what_changed(site, phone):
    driver, _context = phone
    driver.open("")
    wait_engine(driver)
    before = wait_keys(driver, BOOT_NAMES)
    old = site.engine_hashes()
    wasm_hits = site.hits["/vendor/pyodide/pyodide.asm.wasm"]

    # a new build: the Python engine changed, Pyodide did not
    with zipfile.ZipFile(site.path("py/splitcore.zip"), "a") as archive:
        archive.writestr("splitcore/_marker.py", "MARKER = 2\n")
    with open(site.path("py/splitcore.zip"), "rb") as handle:
        new_hash = hashlib.sha256(handle.read()).hexdigest()
    assert new_hash != old["py/splitcore.zip"]
    site.restamp(build="next-build-0001", engine=dict(old, **{"py/splitcore.zip": new_hash}))

    driver.page.reload()                                  # the browser finds the new worker
    driver.wait_screen("home")
    driver.page.wait_for_selector('[data-action="update"]', timeout=60_000)
    # still the old build: the old engine file is what the page gets
    served = driver.page.evaluate("""() => fetch('py/splitcore.zip').then((r) => r.arrayBuffer())
        .then((b) => crypto.subtle.digest('SHA-256', b))
        .then((d) => Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, '0')).join(''))""")
    assert served == old["py/splitcore.zip"], "the waiting build must not leak into the running one"

    with driver.page.expect_navigation(timeout=60_000):
        driver.click('[data-action="update"]')
    driver.wait_screen("home")
    wait_engine(driver)
    after = wait_keys(driver, BOOT_NAMES)
    assert any(key.endswith("splitcore.zip?h=" + new_hash[:16]) for key in after), after
    assert not any(key.endswith("splitcore.zip?h=" + old["py/splitcore.zip"][:16]) for key in after), after
    unchanged = [key for key in before if "splitcore.zip" not in key]
    assert set(unchanged) <= set(after)
    assert site.hits["/vendor/pyodide/pyodide.asm.wasm"] == wasm_hits, "an update must not download Pyodide again"
    served = driver.page.evaluate("""() => fetch('py/splitcore.zip').then((r) => r.arrayBuffer())
        .then((b) => crypto.subtle.digest('SHA-256', b))
        .then((d) => Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, '0')).join(''))""")
    assert served == new_hash
    assert driver.page.evaluate("() => caches.keys()") and "shell-next-build-0001" in driver.page.evaluate("() => caches.keys()")
    assert not driver.visible('[data-action="update"]')
    assert split_and_save(driver, PHOTO, 6, "3x2", "jpg") == []
    assert driver.console_problems() == []


def test_a_file_of_another_build_is_never_stored(site, phone):
    driver, context = phone
    hashes = site.engine_hashes()
    wheel = next(path for path in hashes if "pillow-" in path)
    site.restamp(engine=dict(hashes, **{wheel: "0" * 64}))   # this build expects a different Pillow

    driver.open("")
    wait_engine(driver)                                   # online it still works (the file comes from the network)
    keys = wait_keys(driver, [name for name in BOOT_NAMES if name != "pillow-"])
    driver.page.wait_for_timeout(3000)
    keys = driver.page.evaluate(KEYS)
    assert not any("pillow-" in key for key in keys), keys

    context.set_offline(True)
    driver.page.reload()
    driver.wait_screen("home")
    wait_engine(driver, "failed", timeout=60_000)         # fails fast instead of waiting for ever
    screen = driver.pick_files([PHOTO])
    assert screen == "error", screen
    assert driver.error_code() == "OFFLINE"


@pytest.fixture()
def iphone(browsers, playwright_instance, site):
    from conftest import SHARE_STUB

    options = dict(playwright_instance.devices["iPhone 13"])
    options.update(locale="ko-KR")
    context = browsers("webkit").new_context(**options)
    context.add_init_script(SHARE_STUB)
    page = context.new_page()
    driver = AppDriver(page, site.url, "iphone")
    yield driver, context
    context.close()


def test_iphone_tab_loads_the_engine_through_the_service_worker(site, iphone):
    """Safari's engine: the service worker answers the module worker, import() and the wasm download."""
    driver, context = iphone
    driver.open("")
    if not driver.page.evaluate("() => 'serviceWorker' in navigator"):
        pytest.skip("this WebKit build has no service workers")
    driver.page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=60_000)
    assert driver.page.evaluate("() => document.body.dataset.engine") == "idle"   # a Safari tab waits for a file
    assert split_and_save(driver, PHOTO, 9, "3x3", "pdf") == []
    wait_keys(driver, BOOT_NAMES)
    assert site.hits["/vendor/pyodide/pyodide.asm.wasm"] == 1

    # Playwright's offline switch for WebKit also blocks what the service worker answers from its
    # cache, so the server itself is taken away instead.
    site.go_down()
    driver.page.reload()
    driver.wait_screen("home")
    assert split_and_save(driver, PHOTO, 4, "2x2", "png") == []
    assert driver.console_problems() == []
