"""Frontend developer check: drives the real app (real Python engine) through its DOM contract.

    .venv/Scripts/python.exe tests/e2e/dev_frontend_check.py                 # everything
    .venv/Scripts/python.exe tests/e2e/dev_frontend_check.py --only flows    # flows | extras | layout
    .venv/Scripts/python.exe tests/e2e/dev_frontend_check.py --profile iphone
    .venv/Scripts/python.exe tests/e2e/dev_frontend_check.py --base http://localhost:8847

Three parts:

* flows  - the whole journey in Chromium (Pixel 7) and WebKit (iPhone 13): photo, multi-page PDF,
           DOCX, password PDF, broken files, text-only DOCX, several files with one broken, back
           button, cancel, ZIP.  Received files are checked with tests/e2e/verify.py.
* extras - things outside the main journey: in-app browsers, install sheets, engine start policy,
           first run / offline, desktop drop and paste, keyboard use, service worker update bar.
* layout - every screen at 320, 390, 768 and 1280 px (plus dark and a phone held sideways): no
           horizontal overflow, touch targets of 44 px, screenshots in tests/e2e/out/frontend-shots/.

Nothing here calls application functions; it taps, types and reads the DOM like a person would.
WebKit on Windows has no navigator.share, so the iPhone profile captures what the app hands to it.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)

from playwright.sync_api import sync_playwright  # noqa: E402

import serve  # noqa: E402  (tools/serve.py)

try:
    from verify import verify_outputs  # noqa: E402  (independent check of the received files)
except Exception as exc:  # pragma: no cover - the flows still check names and counts
    verify_outputs = None
    print("verify.py not usable:", exc)

WEB = os.path.join(ROOT, "web")
CORPUS = os.path.join(ROOT, "tests", "corpus")
OUT = os.path.join(ROOT, "tests", "e2e", "out")
SHOTS = os.path.join(OUT, "frontend-shots")
SAMPLE = os.path.join(OUT, "frontend-sample", "유채꽃밭_4032x3024.jpg")

PHOTO = "01_사진_가로_4032x3024.jpg"
PORTRAIT = "02_사진_세로_EXIF회전.jpg"
SQUARE = "03_정사각형_2000.png"
PANORAMA = "06_파노라마_6000x1200.jpg"
SMALL = "11_웹피_1600x900.webp"
BMP = "12_비트맵_800x600.bmp"
PDF5 = "21_여러쪽_5쪽_크기제각각.pdf"
LOCKED = "23_암호_1234.pdf"
DOCX2 = "30_워드_그림2장.docx"
PPTX3 = "31_파워포인트_그림3장.pptx"
TEXT_DOCX = "32_글자만_있는_워드.docx"
BROKEN = "50_잘린_사진.jpg"
FAKE = "51_이름만_사진.png"
EMPTY = "52_빈파일.pdf"
HUGE = "53_너무큰_해상도.png"

ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Mobile Safari/537.36")
KAKAO_UA = ANDROID_UA + " KAKAOTALK/25.9.2 (INAPP)"

# Runs before any page script: records what the page would hand to the share sheet, whether that
# happened inside a tap, and promise rejections nobody handled.
INIT_COMMON = """
(() => {
  window.__rejections = [];
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    window.__rejections.push(String((reason && (reason.stack || reason.message)) || reason));
  });
  window.__inTap = false;
  document.addEventListener('click', () => { window.__inTap = true; setTimeout(() => { window.__inTap = false; }, 0); }, true);
})();
"""
INIT_SHARE = """
(() => {
  window.__shared = [];
  const nav = Navigator.prototype;
  Object.defineProperty(nav, 'canShare', { configurable: true, value: (data) => !!(data && data.files && data.files.length) });
  Object.defineProperty(nav, 'share', { configurable: true, value: (data) => {
    window.__shared.push({ files: Array.from((data && data.files) || []), keys: Object.keys(data || {}), inTap: window.__inTap });
    return window.__shareOutcome ? Promise.reject(new DOMException('x', window.__shareOutcome)) : Promise.resolve();
  } });
})();
"""
INIT_ACTIVATION = """
Object.defineProperty(Navigator.prototype, 'userActivation', { configurable: true, value: { isActive: %s, hasBeenActive: true } });
"""
# Lets a test make the next export fail the way a broken engine call would (engine.js turns the
# throw into an INTERNAL error).  Nothing in the app knows about this switch.
INIT_FAIL_EXPORT = """
(() => {
  const post = Worker.prototype.postMessage;
  Worker.prototype.postMessage = function (message, ...rest) {
    if (window.__failExport && message && message.params && message.params.method === 'export') throw new Error('test: export blocked');
    return post.call(this, message, ...rest);
  };
})();
"""
INIT_STANDALONE = """
Object.defineProperty(Navigator.prototype, 'standalone', { configurable: true, value: true });
"""
READ_SHARED = """
async () => {
  const batch = window.__shared[window.__shared.length - 1];
  const files = [];
  for (const file of batch.files) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let text = '';
    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    files.push({ name: file.name, type: file.type, b64: btoa(text) });
  }
  return { keys: batch.keys, inTap: batch.inTap, files };
}
"""

# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------
RESULTS: list[tuple[str, str, bool, str]] = []
VERBOSE = False


def check(group: str, label: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((group, label, ok, detail))
    if not ok or VERBOSE:
        print(f"  {'ok  ' if ok else 'FAIL'} [{group}] {label}{'' if ok else '  <- ' + str(detail)[:400]}", flush=True)
    return ok


def corpus(name: str) -> str:
    return os.path.join(CORPUS, name)


# ---------------------------------------------------------------------------
# Server (serves web/; can pretend a new build of sw.js exists)
# ---------------------------------------------------------------------------
class BumpableHandler(serve.Handler):
    """tools/serve.py, plus: sw.js gets a trailing comment once `bump` is set (= a new build)."""

    bump = 0

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].endswith("/sw.js") and BumpableHandler.bump:
            with open(os.path.join(WEB, "sw.js"), "rb") as handle:
                body = handle.read() + f"\n// test build {BumpableHandler.bump}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def start_server():
    for port in range(8861, 8900):
        try:
            handler = lambda *a, **kw: BumpableHandler(*a, directory=WEB, **kw)  # noqa: E731
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://localhost:{port}"
    raise RuntimeError("no free port between 8861 and 8899")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
class App:
    def __init__(self, page, profile: str, base: str):
        self.page = page
        self.profile = profile  # "android" | "iphone" | "desktop"
        self.base = base
        self.problems: list[str] = []
        self.used_deliver_sheet = 0
        self.delivered_directly = 0
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.problems.append(f"pageerror: {error}"))

    def _console(self, message) -> None:
        if message.type == "error":
            self.problems.append(f"console: {message.text}")

    # -- reading ----------------------------------------------------------
    @property
    def screen(self) -> str:
        return self.page.evaluate("document.body.dataset.screen")

    @property
    def export_state(self) -> str:
        return self.page.evaluate("document.body.dataset.export")

    @property
    def engine_state(self) -> str:
        return self.page.evaluate("document.body.dataset.engine")

    def status(self) -> str:
        # strict locator: there must be exactly one role="status" element
        return self.page.locator('[role="status"]').inner_text()

    def error_code(self):
        return self.page.evaluate("(document.querySelector('[data-error-code]') || { dataset: {} }).dataset.errorCode || null")

    def act(self, name: str):
        """Strict locator for a data-action (fails when two elements carry the same action)."""
        return self.page.locator(f'[data-action="{name}"]')

    def sheet(self):
        return self.page.evaluate("document.querySelector('#sheet.is-open') ? document.querySelector('#sheet').dataset.name : null")

    def rejections(self) -> list[str]:
        return self.page.evaluate("window.__rejections || []")

    # -- steps ------------------------------------------------------------
    def goto(self, query: str = "?nosw") -> None:
        self.page.goto(f"{self.base}/{query}")
        self.page.wait_for_selector("body[data-ready]", timeout=30_000)

    def wait_screen(self, *names: str, timeout: float = 180_000) -> str:
        self.page.wait_for_function("names => names.includes(document.body.dataset.screen)", arg=list(names), timeout=timeout)
        # a person needs a moment to see a new screen; the app ignores taps on the action area for
        # 300 ms after it changed (the second tap of a double tap must not press the new button)
        self.page.wait_for_timeout(330)
        return self.screen

    def settle(self, ms: int = 350) -> None:
        self.page.wait_for_timeout(ms)

    def pick(self, *names: str, selector: str = "#input-files") -> None:
        self.page.set_input_files(selector, [name if os.path.isabs(name) else corpus(name) for name in names])

    def open(self, *names: str) -> str:
        """Pick files and wait for whatever comes after the (optional) waiting screen."""
        self.pick(*names)
        return self.wait_screen("pick", "confirm", "error", timeout=240_000)

    def wait_preview(self) -> None:
        self.page.wait_for_selector("img[data-preview].is-in", timeout=60_000)

    def home(self) -> None:
        """Back to the start through the UI (keeps the engine warm, exercises session resets)."""
        if self.sheet():
            self.page.keyboard.press("Escape")
        screen = self.screen
        if screen == "home":
            return
        if screen == "result":
            self.act("restart").click()
        else:
            for _ in range(6):
                if self.screen == "home":
                    break
                self.act("back").click()
                self.settle(250)
        self.wait_screen("home", timeout=15_000)

    def to_split(self, count: int, layout: str | None = None) -> str:
        self.act("confirm-yes").click()
        self.wait_screen("split")
        self.page.locator(f'[data-count="{count}"]').click()
        self.page.wait_for_selector("[data-layout]")
        if layout:
            card = self.page.locator(f'[data-layout="{layout}"]')
            card.scroll_into_view_if_needed()
            card.click()
        return self.page.evaluate("(document.querySelector('[data-layout][aria-checked=\"true\"]') || { dataset: {} }).dataset.layout || null")

    def to_result(self) -> None:
        self.act("split-yes").click()
        self.wait_screen("result")

    def choose_format(self, fmt: str, paper: str | None = None) -> None:
        card = self.page.locator(f'[data-format="{fmt}"]')
        card.scroll_into_view_if_needed()
        card.click()
        if paper:
            self.page.locator(f'[data-paper="{paper}"]').click()

    def _wait_built(self, timeout: float) -> None:
        self.page.wait_for_function(
            "() => document.body.dataset.screen === 'result' && ['ready', 'failed'].includes(document.body.dataset.export)",
            timeout=timeout)
        if self.export_state == "failed":
            raise AssertionError("export failed: " + self.status())

    def save(self, action: str = "save", timeout: float = 420_000) -> dict[str, bytes]:
        """Tap save (or share), wait for the build, tap the 'ready' sheet when it shows; returns name -> bytes."""
        page = self.page
        if self.profile == "iphone":
            before = page.evaluate("window.__shared.length")
            self.act(action).click()
            self._wait_built(timeout)
            if page.evaluate("window.__shared.length") == before:
                page.wait_for_selector('[data-action="deliver"]', timeout=10_000)
                self.used_deliver_sheet += 1
                self.act("deliver").click()
            else:
                self.delivered_directly += 1
            page.wait_for_function("n => window.__shared.length > n", arg=before, timeout=15_000)
            batch = page.evaluate(READ_SHARED)
            self.last_share = batch
            return {item["name"]: base64.b64decode(item["b64"]) for item in batch["files"]}
        with page.expect_download(timeout=timeout) as info:
            self.act(action).click()
            self._wait_built(timeout)
            self.settle(150)
            if self.sheet() == "deliver":
                self.used_deliver_sheet += 1
                self.act("deliver").click()
            else:
                self.delivered_directly += 1
        download = info.value
        with open(download.path(), "rb") as handle:
            return {download.suggested_filename: handle.read()}


def verify(group: str, label: str, source: str, files: dict[str, bytes], **kwargs) -> None:
    if verify_outputs is None:
        check(group, label + " (verify.py unavailable: count only)", len(files) > 0)
        return
    path = source if os.path.isabs(source) else corpus(source)
    try:
        problems = verify_outputs(path, files, **kwargs)
    except Exception as exc:  # noqa: BLE001
        problems = [f"verifier crashed: {type(exc).__name__}: {exc}"]
    check(group, label, not problems, "; ".join(problems))


def new_app(playwright, browsers, profile: str, base: str, *, viewport=None, scheme="light", ua=None,
            share=None, activation=None, standalone=False, scale=None, landscape=False, service_workers="allow",
            fail_export=False):
    """A fresh context + page.  profile: android (Chromium, Pixel 7), iphone (WebKit, iPhone 13), desktop (Chromium)."""
    if profile == "iphone":
        engine, options = "webkit", dict(playwright.devices["iPhone 13 landscape" if landscape else "iPhone 13"])
    elif profile == "android":
        engine, options = "chromium", dict(playwright.devices["Pixel 7 landscape" if landscape else "Pixel 7"])
    else:
        engine, options = "chromium", {"viewport": {"width": 1280, "height": 800}}
    if viewport:
        options["viewport"] = {"width": viewport[0], "height": viewport[1]}
        options.pop("screen", None)
    if scale:
        options["device_scale_factor"] = scale
    if ua:
        options["user_agent"] = ua
    options.update(accept_downloads=True, locale="ko-KR", color_scheme=scheme, service_workers=service_workers)
    if engine not in browsers:
        browsers[engine] = getattr(playwright, engine).launch()
    context = browsers[engine].new_context(**options)
    context.add_init_script(INIT_COMMON)
    if share if share is not None else profile == "iphone":
        context.add_init_script(INIT_SHARE)
    if activation is not None:
        context.add_init_script(INIT_ACTIVATION % ("true" if activation else "false"))
    if standalone:
        context.add_init_script(INIT_STANDALONE)
    if fail_export:
        context.add_init_script(INIT_FAIL_EXPORT)
    page = context.new_page()
    return App(page, profile, base), context


def finish(app: App, context, group: str) -> None:
    """Console errors and unhandled rejections of one page, then close it."""
    try:
        rejections = app.rejections()
    except Exception:  # noqa: BLE001 - page already gone
        rejections = []
    check(group, "no console errors", not app.problems, " | ".join(app.problems[:4]))
    check(group, "no unhandled promise rejections", not rejections, " | ".join(rejections[:3]))
    context.close()


# ---------------------------------------------------------------------------
# Part 1: flows
# ---------------------------------------------------------------------------
def flow_photo_pdf(a: App) -> None:
    g = f"{a.profile} A photo-9-pdf"
    page = a.page
    accept = page.get_attribute("#input-files", "accept")
    if a.profile == "android":
        a.act("pick").click()
        check(g, "pick opens a sheet with photos / files", a.sheet() == "picker"
              and a.act("pick-photos").is_visible() and a.act("pick-files").is_visible())
        check(g, "file input accept list set at runtime, without image/*", bool(accept) and ".hwpx" in accept and "image/" not in accept, accept)
    else:
        check(g, "file input has no accept attribute on iOS", not accept, accept)
    check(g, "photo input accepts image/* and multiple", page.get_attribute("#input-photos", "accept") == "image/*"
          and page.get_attribute("#input-photos", "multiple") is not None)
    screen = a.open(PHOTO)
    check(g, "one photo goes straight to confirm", screen == "confirm", screen)
    check(g, "picker sheet closed after the pick", a.sheet() is None)
    check(g, "input value reset after reading", page.evaluate("document.querySelector('#input-files').value") == "")
    check(g, "focus moved to the headline", page.evaluate("document.activeElement && document.activeElement.classList.contains('h1')"))
    meta = page.inner_text(".meta")
    check(g, "confirm shows name and pixel size", PHOTO in meta and "4032 × 3024px" in meta, meta)
    a.wait_preview()
    a.act("confirm-yes").click()
    a.wait_screen("split")
    counts = page.evaluate("Array.from(document.querySelectorAll('[data-count]')).map((el) => Number(el.dataset.count))")
    check(g, "eight counts offered, none selected", counts == [2, 4, 6, 8, 9, 12, 16, 18]
          and page.locator('[data-count][aria-checked="true"]').count() == 0, str(counts))
    check(g, "no yes/no before a count is chosen", a.act("split-yes").count() == 0)
    page.locator('[data-count="9"]').click()
    page.wait_for_selector("[data-layout]")
    first = page.locator("[data-layout]").first
    check(g, "recommended layout first, selected, labelled with words",
          first.get_attribute("data-layout") == "3x3" and first.get_attribute("aria-checked") == "true"
          and "추천" in first.inner_text() and "가로 3칸 · 세로 3줄" in first.inner_text(), first.inner_text())
    check(g, "nine numbered cells on the preview", page.locator("#split-overlay .cells b").count() == 9)
    page.wait_for_function("() => document.querySelector('#split-cap').textContent.includes('4032 × 3024px')", timeout=10_000)
    check(g, "piece size line uses the engine's numbers", "조각마다 4032 × 3024px로 키워 드려요" in page.inner_text("#split-cap"))
    a.to_result()
    check(g, "result shows a mosaic of nine tiles", page.locator(".mosaic i").count() == 9)
    check(g, "summary line", "가로 3 × 세로 3 · 각 4032 × 3024px" in page.inner_text(".lead"), page.inner_text(".lead"))
    check(g, "export idle before saving", a.export_state == "idle")
    check(g, "share button hidden (iOS: save is the share sheet; headless Chromium has no share)", a.act("share").count() == 0)
    if a.profile == "iphone":
        check(g, "iOS hint about the share sheet", "공유 창에서" in page.inner_text("#dock"))
    a.choose_format("pdf")
    files = a.save()
    check(g, "one PDF delivered", len(files) == 1 and next(iter(files)).endswith(".pdf"), str(list(files)))
    verify(g, "PDF content verified", PHOTO, files, rows=3, cols=3, fmt="pdf")
    check(g, "export state ready", a.export_state == "ready")
    a.settle(200)
    expected = "내보냈어요" if a.profile == "iphone" else "저장했어요 · " + next(iter(files))
    check(g, "toast text in role=status", expected in a.status(), a.status())
    if a.profile == "iphone":
        share = a.last_share
        check(g, "share() got files only, inside the tap", share["keys"] == ["files"] and share["inTap"], str(share["keys"]) + str(share["inTap"]))
        check(g, "shared file has the engine's MIME type", share["files"][0]["type"] == "application/pdf", share["files"][0]["type"])
    # unchanged choice: the second tap delivers at once from the cache
    started = time.time()
    again = a.save()
    check(g, "second save is instant and identical", again == files and time.time() - started < 4, f"{time.time() - started:.1f}s")
    a.act("restart").click()
    check(g, "restart returns home", a.wait_screen("home", timeout=10_000) == "home")


def flow_pdf_page_png(a: App) -> None:
    g = f"{a.profile} B pdf-page3-6-png"
    page = a.page
    screen = a.open(PDF5)
    check(g, "multi-page PDF opens the thumbnail screen", screen == "pick", f"{screen} {a.error_code()}")
    check(g, "five thumbnails, next disabled", page.locator("[data-source]").count() == 5 and a.act("next").is_disabled())
    check(g, "lead names the file and the page count", PDF5 in page.inner_text(".lead") and "5쪽" in page.inner_text(".lead"))
    page.wait_for_function("() => document.querySelectorAll('.thumb img.is-in').length === 5", timeout=60_000)
    third = page.locator("[data-source]").nth(2)
    third.click()
    check(g, "one selection, reflected by aria-checked", third.get_attribute("aria-checked") == "true"
          and page.locator('[data-source][aria-checked="true"]').count() == 1)
    a.act("next").click()
    a.wait_screen("confirm")
    meta = page.inner_text(".meta")
    check(g, "page shows pixel size and paper size", "3508 × 2480px" in meta and "A4 · 297 × 210 mm" in meta, meta)
    a.wait_preview()
    layout = a.to_split(6)
    check(g, "landscape page: 3x2 suggested", layout == "3x2", str(layout))
    a.to_result()
    a.choose_format("png")
    if a.profile == "iphone":
        check(g, "iOS offers the ZIP alternative for picture sets", a.act("zip-toggle").is_visible()
              and a.act("zip-toggle").get_attribute("aria-checked") == "false")
    else:
        check(g, "paper option switched off for pictures", page.locator('[data-paper="a4"]').is_disabled())
    files = a.save()
    if a.profile == "iphone":
        check(g, "six PNG files shared", len(files) == 6 and all(name.endswith(".png") for name in files), str(sorted(files)))
        check(g, "shared inside a tap, files only", a.last_share["inTap"] and a.last_share["keys"] == ["files"])
        verify(g, "PNG tiles verified", PDF5, files, rows=2, cols=3, fmt="png", pick=2)
    else:
        check(g, "one ZIP downloaded", len(files) == 1 and next(iter(files)).endswith(".zip"), str(list(files)))
        verify(g, "ZIP of PNG tiles verified", PDF5, files, rows=2, cols=3, fmt="png", pick=2, zipped=True)
    a.home()


def flow_docx_word(a: App) -> None:
    g = f"{a.profile} C docx-2nd-4-word"
    page = a.page
    screen = a.open(DOCX2)
    check(g, "DOCX with two pictures opens the thumbnail screen", screen == "pick" and page.locator("[data-source]").count() == 2, screen)
    check(g, "lead says the pictures are inside the file", "들어 있는 이미지 2장" in page.inner_text(".lead"), page.inner_text(".lead"))
    page.locator("[data-source]").nth(1).click()
    a.act("next").click()
    a.wait_screen("confirm")
    check(g, "confirm names file and picture", DOCX2 in page.inner_text(".meta") and "이미지 2" in page.inner_text(".meta"), page.inner_text(".meta"))
    layout = a.to_split(4)
    check(g, "2x2 suggested", layout == "2x2", str(layout))
    a.to_result()
    a.choose_format("docx", "a4")
    check(g, "A4 selected", page.locator('[data-paper="a4"]').get_attribute("aria-checked") == "true")
    files = a.save()
    check(g, "one .docx delivered", len(files) == 1 and next(iter(files)).endswith(".docx"), str(list(files)))
    verify(g, "Word document verified", DOCX2, files, rows=2, cols=2, fmt="docx", paper="a4", pick=1)
    if a.profile == "iphone":
        check(g, "docx shared with its MIME type", a.last_share["files"][0]["type"].endswith("wordprocessingml.document"))
    a.home()


def flow_password(a: App) -> None:
    g = f"{a.profile} D password-pdf"
    page = a.page
    screen = a.open(LOCKED)
    check(g, "locked PDF -> error screen ENCRYPTED", screen == "error" and a.error_code() == "ENCRYPTED", f"{screen} {a.error_code()}")
    check(g, "password field shown, unlock disabled while empty", page.locator("#password").is_visible() and a.act("unlock").is_disabled())
    page.fill("#password", "0000")
    check(g, "unlock enabled after typing", a.act("unlock").is_enabled())
    a.act("unlock").click()
    page.wait_for_function("() => document.querySelector('[data-error-code]') && document.querySelector('[data-error-code]').dataset.errorCode === 'WRONG_PASSWORD'", timeout=30_000)
    check(g, "wrong password: stays on the error screen with a message", a.screen == "error"
          and "암호가 맞지 않아요" in page.inner_text("#password-message"), page.inner_text("#password-message"))
    check(g, "field keeps focus for another try", page.evaluate("document.activeElement && document.activeElement.id") == "password")
    page.fill("#password", "1234")
    page.keyboard.press("Enter")
    check(g, "right password (Enter) opens the document", a.wait_screen("confirm", "pick", timeout=30_000) == "confirm")
    a.wait_preview()
    layout = a.to_split(2)
    a.to_result()
    a.choose_format("pdf", "a4")
    files = a.save()
    cols, rows = (int(v) for v in layout.split("x"))
    verify(g, "PDF from the unlocked document verified", LOCKED, files, rows=rows, cols=cols, fmt="pdf", paper="a4", password="1234")
    a.home()


def flow_broken(a: App) -> None:
    g = f"{a.profile} E broken-files"
    page = a.page
    for name, code in ((BROKEN, "CORRUPT"), (FAKE, "UNSUPPORTED"), (EMPTY, "EMPTY_FILE"), (HUGE, "IMAGE_TOO_LARGE")):
        screen = a.open(name)
        check(g, f"{name} -> {code}", screen == "error" and a.error_code() == code, f"{screen} {a.error_code()}")
        check(g, f"{code}: headline, file name and a recovery action", name in page.inner_text(".lead")
              and a.act("pick").is_visible() and page.locator("#password").count() == 0, page.inner_text(".lead"))
    # 'retry' on the damaged file runs the same file again
    a.open(BROKEN)
    a.act("retry").click()
    page.wait_for_function("() => document.body.dataset.screen === 'error'", timeout=30_000)
    check(g, "retry with the same file ends on the same error", a.error_code() == "CORRUPT")
    # picking a good file from the error screen leaves the error at once
    screen = a.open(BMP)
    check(g, "a good file picked on the error screen continues", screen == "confirm", screen)
    a.home()
    check(g, "back from confirm returns home", a.screen == "home")


def flow_no_image(a: App) -> None:
    g = f"{a.profile} F text-only-docx"
    screen = a.open(TEXT_DOCX)
    check(g, "text-only DOCX -> NO_IMAGE", screen == "error" and a.error_code() == "NO_IMAGE", f"{screen} {a.error_code()}")
    text = a.page.inner_text("#view")
    check(g, "explains what to do, no password field", "찾지 못했어요" in text and a.page.locator("#password").count() == 0, text[:200])
    a.act("back").click()
    check(g, "back arrow returns home", a.wait_screen("home", timeout=10_000) == "home")


def flow_several(a: App) -> None:
    g = f"{a.profile} G several-files"
    page = a.page
    screen = a.open(SMALL, FAKE)
    check(g, "one good + one broken -> confirm for the good one", screen == "confirm", screen)
    status = a.status()
    check(g, "notice in role=status names the failed file", "1개 파일은 열 수 없었어요" in status and FAKE in status, status)
    check(g, "notice visible on the screen too", "1개 파일은 열 수 없었어요" in page.inner_text("#view") and "지원하지 않는 형식" in page.inner_text("#view"))
    a.home()
    screen = a.open(SMALL, BROKEN, BMP)
    check(g, "two good + one broken -> thumbnails", screen == "pick" and page.locator("[data-source]").count() == 2, screen)
    check(g, "notice lists name and reason", BROKEN in page.inner_text(".notice") and "손상된 파일" in page.inner_text(".notice"), page.inner_text(".notice"))
    page.locator("[data-source]").nth(1).click()
    a.act("next").click()
    a.wait_screen("confirm")
    # confirm-no with several candidates goes back to the thumbnails, selection kept
    a.act("confirm-no").click()
    check(g, "confirm-no returns to the thumbnails", a.wait_screen("pick", "home", timeout=10_000) == "pick")
    check(g, "previous choice still selected", page.locator("[data-source]").nth(1).get_attribute("aria-checked") == "true")
    a.act("next").click()
    a.wait_screen("confirm")
    a.to_split(9)
    a.act("split-no").click()
    a.settle(200)
    check(g, "split-no clears the count", a.screen == "split" and page.locator('[data-count][aria-checked="true"]').count() == 0
          and a.act("split-yes").count() == 0)
    page.locator('[data-count="4"]').click()
    page.wait_for_selector('[data-layout="2x2"][aria-checked="true"]')
    a.to_result()
    a.choose_format("jpg")
    files = a.save()
    if a.profile == "iphone":
        verify(g, "JPG tiles verified", BMP, files, rows=2, cols=2, fmt="jpg")
    else:
        verify(g, "JPG ZIP verified", BMP, files, rows=2, cols=2, fmt="jpg", zipped=True)
    a.home()
    screen = a.open(FAKE, BROKEN)
    check(g, "every file broken -> error screen for the first", screen == "error" and a.error_code() == "UNSUPPORTED", f"{screen} {a.error_code()}")
    check(g, "the other failure is listed", BROKEN in a.page.inner_text("#view"))
    a.home()


def flow_back_and_cancel(a: App) -> None:
    g = f"{a.profile} H back-cancel"
    page = a.page
    a.open(PHOTO)
    a.wait_preview()
    a.to_split(9)
    a.to_result()
    page.go_back()
    check(g, "browser back: result -> split (choice kept)", a.wait_screen("split", timeout=10_000) == "split"
          and page.locator('[data-count="9"]').get_attribute("aria-checked") == "true")
    page.go_back()
    check(g, "browser back: split -> confirm", a.wait_screen("confirm", timeout=10_000) == "confirm")
    page.go_back()
    check(g, "browser back: confirm -> home", a.wait_screen("home", timeout=10_000) == "home")
    page.go_forward()
    a.settle(400)
    check(g, "browser forward does not resurrect a screen", a.screen == "home")
    check(g, "still on the app page", page.url.startswith(a.base))

    # a long export (PNG, 9 tiles of 12 MP): back asks, cancel stops, saving again works
    a.open(PHOTO)
    a.wait_preview()
    a.to_split(9)
    a.to_result()
    a.choose_format("png")
    a.act("save").click()
    a.wait_screen("work", timeout=15_000)
    check(g, "work screen while the files are made", a.export_state == "working" and a.act("cancel").is_visible())
    page.wait_for_function("() => Number(document.querySelector('#work-bar').getAttribute('aria-valuenow')) >= 1", timeout=120_000)
    check(g, "progress text and veil follow the tiles", "/ 9" in page.inner_text("#work-figure")
          and page.locator(".veil i.is-done").count() >= 1, page.inner_text("#work-figure"))
    page.go_back()
    page.wait_for_selector('#sheet.is-open[data-name="cancel"]', timeout=10_000)
    check(g, "back during work asks before cancelling", a.screen == "work" and a.sheet() == "cancel")
    page.locator('#sheet [data-action="close-sheet"]').last.click()  # "아니요"
    a.settle(300)
    check(g, "answering no keeps the export running", a.screen == "work" and a.export_state == "working")
    a.act("cancel").click()
    check(g, "cancel returns to the result screen, idle", a.wait_screen("result", timeout=15_000) == "result" and a.export_state == "idle")
    a.settle(300)
    # the engine was replaced: the same picture must still export
    a.choose_format("jpg")
    files = a.save()
    if a.profile == "iphone":
        verify(g, "export after a cancel still verified", PHOTO, files, rows=3, cols=3, fmt="jpg")
    else:
        verify(g, "export after a cancel still verified", PHOTO, files, rows=3, cols=3, fmt="jpg", zipped=True)
    page.go_back()
    check(g, "history still in step after cancel: result -> split", a.wait_screen("split", timeout=10_000) == "split")
    a.home()


def flow_zip_and_formats(a: App) -> None:
    g = f"{a.profile} I zip-formats"
    page = a.page
    a.open(SQUARE)
    a.wait_preview()
    a.to_split(4)
    a.to_result()
    if a.profile == "iphone":
        a.choose_format("jpg")
        a.act("zip-toggle").click()
        check(g, "ZIP toggle switches on", a.act("zip-toggle").get_attribute("aria-checked") == "true")
        files = a.save()
        check(g, "one ZIP shared", len(files) == 1 and next(iter(files)).endswith(".zip")
              and a.last_share["files"][0]["type"] == "application/zip", str(list(files)))
        verify(g, "shared ZIP verified", SQUARE, files, rows=2, cols=2, fmt="jpg", zipped=True)
        a.act("zip-toggle").click()
        files = a.save()
        check(g, "ZIP off again: four JPG files, straight from the cache", len(files) == 4, str(list(files)))
    # the remaining document formats, one after the other from the same split
    for fmt, paper in (("pptx", "original"), ("hwpx", "a4")):
        a.choose_format(fmt, paper)
        try:
            files = a.save()
        except AssertionError as exc:
            check(g, f"{fmt}: export", False, str(exc))
            continue
        check(g, f"{fmt}: one file with the right extension", len(files) == 1 and next(iter(files)).endswith("." + fmt), str(list(files)))
        verify(g, f"{fmt} verified", SQUARE, files, rows=2, cols=2, fmt=fmt, paper=paper)
    a.home()


FLOWS = [flow_photo_pdf, flow_pdf_page_png, flow_docx_word, flow_password, flow_broken, flow_no_image,
         flow_several, flow_back_and_cancel, flow_zip_and_formats]


def run_flows(playwright, browsers, base: str, profiles: list[str]) -> None:
    for profile in profiles:
        print(f"== flows: {profile}", flush=True)
        app, context = new_app(playwright, browsers, profile, base)
        group = f"{profile} flows"
        try:
            app.goto()
            check(group, "home is the first screen", app.screen == "home")
            for flow in FLOWS:
                started = time.time()
                try:
                    flow(app)
                except Exception as exc:  # noqa: BLE001 - one broken flow must not hide the others
                    check(f"{profile} {flow.__name__}", "flow ran to the end", False,
                          f"{type(exc).__name__}: {exc} | screen={app.screen} export={app.export_state} | "
                          + traceback.format_exc().strip().splitlines()[-3])
                    try:
                        app.goto()
                    except Exception:  # noqa: BLE001
                        pass
                print(f"   {flow.__name__}: {time.time() - started:.1f}s", flush=True)
            check(group, "both delivery paths were used or none was needed",
                  app.used_deliver_sheet + app.delivered_directly > 0,
                  f"sheet={app.used_deliver_sheet} direct={app.delivered_directly}")
            print(f"   delivered directly {app.delivered_directly}x, through the ready sheet {app.used_deliver_sheet}x", flush=True)
        finally:
            finish(app, context, group)


# ---------------------------------------------------------------------------
# Part 2: extras
# ---------------------------------------------------------------------------
def extra_inapp(playwright, browsers, base: str) -> None:
    g = "extra in-app browser"
    app, context = new_app(playwright, browsers, "android", base, ua=KAKAO_UA)
    try:
        app.goto()
        page = app.page
        check(g, "notice sheet opens by itself", app.sheet() == "inapp")
        text = page.inner_text("#sheet")
        check(g, "names the host app, offers the hand-off, manual steps, copy", "카카오톡" in text and app.act("open-external").is_visible()
              and app.act("copy-url").is_visible() and "다른 브라우저로 열기" in text, text[:200])
        app.settle(1500)
        check(g, "engine not started before the user decides", app.engine_state == "idle", app.engine_state)
        check(g, "install entry hidden inside an in-app browser", app.act("install").is_hidden())
        url = page.evaluate("import('./js/platform.js').then((m) => m.externalUrl({ key: 'kakaotalk', name: '카카오톡' }))")
        check(g, "KakaoTalk hand-off URL", url.startswith("kakaotalk://web/openExternal?url=") and "ext%3D1" in url, url)
        url = page.evaluate("import('./js/platform.js').then((m) => m.externalUrl({ key: 'line', name: '라인' }))")
        check(g, "LINE hand-off URL", "openExternalBrowser=1" in url and "ext=1" in url, url)
        url = page.evaluate("import('./js/platform.js').then((m) => m.externalUrl({ key: 'naver', name: '네이버 앱' }))")
        check(g, "generic Android hand-off is a Chrome intent", url.startswith("intent://") and "package=com.android.chrome" in url
              and "S.browser_fallback_url=" in url, url)
        page.locator('#sheet .btn--ghost[data-action="close-sheet"]').click()  # "이대로 계속할게요"
        page.wait_for_function("() => document.body.dataset.engine !== 'idle'", timeout=10_000)
        check(g, "continuing starts the engine", True)
        check(g, "home keeps a banner that reopens the notice", app.act("inapp").is_visible())
        app.act("inapp").click()
        check(g, "banner reopens the sheet", app.sheet() == "inapp")
    finally:
        finish(app, context, g)
    # a page reached through our own hand-off link does not nag again
    app, context = new_app(playwright, browsers, "android", base, ua=KAKAO_UA)
    try:
        app.goto("?nosw&ext=1")
        check(g, "no sheet again after a hand-off (ext=1)", app.sheet() is None and app.act("inapp").is_visible())
    finally:
        finish(app, context, g + " (ext=1)")


def extra_install_and_policy(playwright, browsers, base: str) -> None:
    g = "extra install / engine policy"
    # Android browser tab
    app, context = new_app(playwright, browsers, "android", base)
    try:
        app.goto()
        page = app.page
        page.wait_for_function("() => document.body.dataset.engine !== 'idle'", timeout=10_000)
        check(g, "Android tab: engine starts at once", True)
        check(g, "Android tab: install entry visible", app.act("install").is_visible())
        app.act("install").click()
        text = page.inner_text("#sheet")
        apk = page.get_attribute('#sheet a[href$=".apk"]', "href")
        check(g, "Android sheet: menu steps and the APK link", app.sheet() == "install" and "홈 화면에 추가" in text
              and apk == "https://github.com/geunminmaker/geunminmaker.github.io/releases/latest/download/jogakjogak.apk", str(apk))
        # focus is inside the sheet, Tab stays there, Escape closes and returns focus
        check(g, "sheet takes focus", page.evaluate("document.querySelector('#sheet').contains(document.activeElement)"))
        for _ in range(8):
            page.keyboard.press("Tab")
        check(g, "Tab stays inside the sheet", page.evaluate("document.querySelector('#sheet').contains(document.activeElement)"))
        check(g, "rest of the page is inert while the sheet is open", page.evaluate("document.querySelector('#app').inert === true"))
        page.keyboard.press("Escape")
        app.settle(350)
        check(g, "Escape closes the sheet and focus returns to the button", app.sheet() is None
              and page.evaluate("document.activeElement && document.activeElement.dataset.action") == "install")
    finally:
        finish(app, context, g + " android")
    # iOS Safari tab
    app, context = new_app(playwright, browsers, "iphone", base)
    try:
        app.goto()
        page = app.page
        app.settle(1500)
        check(g, "iOS Safari tab: engine waits for a file", app.engine_state == "idle", app.engine_state)
        check(g, "iOS: install entry visible", app.act("install").is_visible())
        app.act("install").click()
        text = page.inner_text("#sheet")
        check(g, "iOS sheet: Add to Home Screen steps", app.sheet() == "install" and "홈 화면에 추가" in text and "공유" in text, text[:160])
        page.locator('#sheet .sheet__foot [data-action="close-sheet"]').click()
        app.open(SMALL)
        check(g, "iOS Safari tab: picking a file starts the engine", app.engine_state == "ready", app.engine_state)
    finally:
        finish(app, context, g + " ios tab")
    # iOS home-screen app
    app, context = new_app(playwright, browsers, "iphone", base, standalone=True)
    try:
        app.goto()
        app.page.wait_for_function("() => document.body.dataset.engine !== 'idle'", timeout=10_000)
        check(g, "iOS home-screen app: engine starts at once", True)
        check(g, "installed: no install entry", app.act("install").is_hidden())
        check(g, "installed app is not mistaken for an in-app browser", app.sheet() is None and app.act("inapp").count() == 0)
    finally:
        finish(app, context, g + " ios standalone")


def extra_first_run_and_offline(playwright, browsers, base: str) -> None:
    g = "extra first run / offline"
    app, context = new_app(playwright, browsers, "android", base)
    try:
        page = app.page
        slow = Gate(page, "**/python_stdlib.zip")
        app.goto()
        app.pick(PHOTO)
        check(g, "picking before the engine is ready shows the waiting screen at once", app.wait_screen("prep", timeout=5_000) == "prep")
        page.wait_for_function("() => document.querySelector('#prep-figure').textContent.includes('MB')", timeout=20_000)
        text = page.inner_text("#view")
        check(g, "first run wording, megabytes and a progress bar", "처음 한 번만" in text and "MB" in page.inner_text("#prep-figure")
              and page.locator('#prep-bar[role="progressbar"][aria-valuenow]').count() == 1, text[:160])
        check(g, "cancel button present", app.act("cancel").is_visible())
        slow.release()
        check(g, "then continues to confirm", app.wait_screen("confirm", "error", timeout=120_000) == "confirm")
        page.go_back()
        check(g, "browser back from here is one step: home", app.wait_screen("home", timeout=10_000) == "home")
        # the engine has been seen on this origin: a later wait says so differently
        page.reload()
        page.wait_for_selector("body[data-ready]")
        app.pick(SMALL)
        first = app.wait_screen("prep", "confirm", timeout=10_000)
        check(g, "later visits do not claim a first-time download",
              first == "confirm" or "처음 한 번만" not in page.inner_text("#view"), page.inner_text("#view")[:80])
        app.wait_screen("confirm", timeout=120_000)
    finally:
        finish(app, context, g)

    app, context = new_app(playwright, browsers, "android", base)
    try:
        page = app.page
        page.route("**/pyodide.asm.wasm", lambda route: route.abort())
        app.goto()
        app.pick(SMALL)
        # the engine cannot notice this by itself: the app's watchdog gives up after 45 s without progress
        check(g, "engine download fails -> error screen", app.wait_screen("error", timeout=120_000) == "error")
        check(g, "offline wording with retry", app.error_code() == "OFFLINE" and "내려받지 못했어요" in page.inner_text("#view")
              and app.act("retry").is_visible(), f"{app.error_code()} {page.inner_text('#view')[:80]}")
        page.unroute("**/pyodide.asm.wasm")
        app.act("retry").click()
        check(g, "retry after the connection is back works", app.wait_screen("confirm", "error", timeout=120_000) == "confirm", app.error_code())
        # the browser and Pyodide report the blocked download themselves; that is the test's doing
        app.problems = [p for p in app.problems if "Failed to load resource" not in p and "Failed to fetch" not in p
                        and "wasm" not in p.lower()]
    finally:
        finish(app, context, g + " offline")

    # cancelling while files are being read lands on home and a later pick works
    app, context = new_app(playwright, browsers, "android", base)
    try:
        page = app.page
        slow = Gate(page, "**/pymupdf*.whl")
        app.goto()
        page.wait_for_function("() => document.body.dataset.engine === 'ready'", timeout=120_000)
        app.pick(PDF5)
        app.wait_screen("prep", timeout=10_000)
        page.wait_for_function("() => document.querySelector('#prep-status').textContent.includes('PDF 도구')", timeout=10_000)
        check(g, "one-time PDF tools download is announced", "PDF 도구 내려받는 중" in page.inner_text("#prep-status"))
        app.act("cancel").click()
        check(g, "cancel during loading returns home", app.wait_screen("home", timeout=10_000) == "home")
        slow.release()
        check(g, "a new pick after cancelling works", app.open(SMALL) == "confirm")
        app.home()
        check(g, "and the cancelled PDF opens when picked again", app.open(PDF5) == "pick" and page.locator("[data-source]").count() == 5)
    finally:
        finish(app, context, g + " cancel")


def extra_desktop(playwright, browsers, base: str) -> None:
    g = "extra desktop"
    app, context = new_app(playwright, browsers, "desktop", base)
    try:
        app.goto()
        page = app.page
        check(g, "drop zone visible, action inside it", page.locator("#drop").is_visible()
              and page.evaluate("document.querySelector('#drop').contains(document.querySelector('#dock'))"))
        check(g, "no install entry without a browser prompt", app.act("install").is_hidden())
        with page.expect_file_chooser(timeout=5_000) as chooser:
            app.act("pick").click()
        check(g, "pick opens the system dialog directly (multiple)", chooser.value.is_multiple() and app.sheet() is None)
        page.wait_for_function("() => document.body.dataset.engine === 'ready'", timeout=120_000)
        # drag a file over the window, then drop it
        with open(corpus(SMALL), "rb") as handle:
            payload = base64.b64encode(handle.read()).decode()
        make = """
        (b64) => {
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
          const data = new DataTransfer();
          data.items.add(new File([bytes], '끌어온_사진.webp', { type: 'image/webp' }));
          window.__drag = data;
          window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: data, bubbles: true, cancelable: true }));
          return document.querySelector('#drop').classList.contains('is-over') + '|' + document.querySelector('[data-drop-title]').textContent;
        }
        """
        over = page.evaluate(make, payload)
        check(g, "drag over the window highlights the drop zone", over.startswith("true|") and "여기에 놓으면" in over, over)
        page.evaluate("window.dispatchEvent(new DragEvent('drop', { dataTransfer: window.__drag, bubbles: true, cancelable: true }))")
        check(g, "dropping starts with that file", app.wait_screen("confirm", "error", timeout=60_000) == "confirm"
              and "끌어온_사진.webp" in page.inner_text(".meta"))
        check(g, "wide layout: actions sit inside the screen", page.evaluate("document.querySelector('#view').contains(document.querySelector('#dock'))"))
        # paste a picture on a later screen replaces the session
        paste = """
        (b64) => {
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
          const data = new DataTransfer();
          data.items.add(new File([bytes], 'image.webp', { type: 'image/webp' }));
          window.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
        }
        """
        app.act("confirm-yes").click()
        app.wait_screen("split")
        page.evaluate(paste, payload)
        page.wait_for_function("() => document.body.dataset.screen === 'confirm' && document.querySelector('.meta').textContent.includes('image.webp')", timeout=60_000)
        check(g, "Ctrl+V with a picture starts over with it", True)
        # keyboard: arrows move inside a radio group, one tab stop per group
        app.settle()  # the action area ignores taps for 300 ms after it changed
        app.act("confirm-yes").click()
        app.wait_screen("split")
        check(g, "focus on the headline after the screen change", page.evaluate("document.activeElement.classList.contains('h1')"))
        stops = page.evaluate("Array.from(document.querySelectorAll('.counts [role=radio]')).filter((el) => el.tabIndex === 0).length")
        check(g, "count group has one tab stop", stops == 1, str(stops))
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement.dataset.count")
        page.keyboard.press("ArrowRight")
        picked = page.evaluate("[document.activeElement.dataset.count, document.activeElement.getAttribute('aria-checked')]")
        check(g, "arrow key moves and selects the next count", focused == "2" and picked == ["4", "true"], f"{focused} {picked}")
        page.keyboard.press("End")
        check(g, "End jumps to the last count", page.evaluate("document.activeElement.dataset.count") == "18")
        visible = page.evaluate("getComputedStyle(document.activeElement).outlineStyle !== 'none'")
        check(g, "keyboard focus ring visible", visible)
        page.keyboard.press("Tab")
        check(g, "Tab moves on to the selected layout card", page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true"
              and page.evaluate("!!document.activeElement.dataset.layout"))
        app.to_result()
        files = app.save()
        check(g, "desktop saves with one download", len(files) == 1 and next(iter(files)).endswith(".pdf"), str(list(files)))
        check(g, "no share button on desktop", app.act("share").count() == 0)
    finally:
        finish(app, context, g)


def extra_big_pdf(playwright, browsers, base: str) -> None:
    """A document with more pages than the app shows: 300 candidates, a notice, thumbnails only near the viewport."""
    g = "extra 320-page pdf"
    import pymupdf
    path = os.path.join(OUT, "frontend-sample", "긴_문서_320쪽.pdf")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = pymupdf.open()
        for number in range(1, 321):
            page = doc.new_page(width=595, height=842)
            page.draw_rect(pymupdf.Rect(40, 40, 555, 802), color=(0.2, 0.4, 0.9), width=3)
            page.insert_text((230, 430), str(number), fontsize=96, color=(0.1, 0.1, 0.1))
        doc.save(path, deflate=True)
        doc.close()
    app, context = new_app(playwright, browsers, "android", base)
    try:
        app.goto()
        page = app.page
        # The PDF reader stops at 300 pages without saying so (engine: truncated stays false for one long PDF),
        # so a second file is picked along with it: that one no longer fits and the engine reports truncated.
        screen = app.open(path, SMALL)
        check(g, "opens the thumbnail screen with 300 candidates", screen == "pick" and page.locator("[data-source]").count() == 300,
              f"{screen} {page.locator('[data-source]').count()}")
        text = page.inner_text("#view")
        check(g, "says that only the first 300 were taken (screen and role=status)", "앞쪽 일부만 가져왔어요" in text
              and "앞쪽 일부만" in app.status(), app.status())
        app.settle(2500)
        loaded = page.locator(".thumb img.is-in").count()
        check(g, "only thumbnails near the viewport were made", 3 <= loaded <= 45, str(loaded))
        page.locator("[data-source]").nth(299).scroll_into_view_if_needed()
        page.wait_for_function("() => { const img = document.querySelectorAll('.thumb img')[299]; return img && img.classList.contains('is-in'); }",
                               timeout=60_000)
        total = page.locator(".thumb img.is-in").count()
        check(g, "the last page's thumbnail appears after scrolling, the middle ones were skipped", total < 120, str(total))
        page.locator("[data-source]").nth(299).click()
        app.act("next").click()
        app.wait_screen("confirm")
        check(g, "the 300th page can be chosen", "300쪽" in page.inner_text(".meta"), page.inner_text(".meta"))
        # keyboard: arrows in a 300-item radio group still move one by one
        page.go_back()
        app.wait_screen("pick")
        check(g, "back keeps the choice and scrolls it into view", page.locator("[data-source]").nth(299).get_attribute("aria-checked") == "true"
              and page.locator("[data-source]").nth(299).is_visible())
    finally:
        finish(app, context, g)


def extra_capped_and_tiny(playwright, browsers, base: str) -> None:
    """Sizes at the edges: a picture so large that the pieces are capped, and one too small for some grids."""
    g = "extra size limits"
    from PIL import Image
    folder = os.path.join(OUT, "frontend-sample")
    os.makedirs(folder, exist_ok=True)
    big = os.path.join(folder, "아주큰_6400x4800.jpg")
    if not os.path.exists(big):
        Image.linear_gradient("L").resize((6400, 4800)).convert("RGB").save(big, quality=70)
    tiny = os.path.join(folder, "아주작은_12x10.png")
    if not os.path.exists(tiny):
        Image.new("RGB", (12, 10), (40, 120, 220)).save(tiny)
    app, context = new_app(playwright, browsers, "android", base)
    try:
        app.goto()
        page = app.page
        check(g, "30 MP picture opens", app.open(big) == "confirm", str(app.error_code()))
        app.to_split(9)
        page.wait_for_function("() => document.querySelector('#split-cap').textContent.includes('줄였어요')", timeout=20_000)
        caption = page.inner_text("#split-cap")
        check(g, "capped piece size is explained, with the engine's reduced numbers", "원본이 아주 커서 조각 크기를 줄였어요" in caption
              and "6400 × 4800px" not in caption, caption)
        app.to_result()
        check(g, "the result line shows the same reduced size", "6400 × 4800px" not in page.inner_text(".lead"), page.inner_text(".lead"))
        app.home()
        check(g, "12 x 10 px picture opens", app.open(tiny) == "confirm", str(app.error_code()))
        app.act("confirm-yes").click()
        app.wait_screen("split")
        page.locator('[data-count="18"]').click()
        page.wait_for_selector("[data-layout]")
        disabled = page.evaluate("() => Array.from(document.querySelectorAll('[data-layout]')).filter((el) => el.disabled).map((el) => el.dataset.layout).sort()")
        chosen = page.evaluate("() => (document.querySelector('[data-layout][aria-checked=\"true\"]') || { dataset: {} }).dataset.layout")
        check(g, "impossible grids are disabled, a possible one is preselected", disabled == ["18x1", "1x18"] and chosen not in disabled,
              f"{disabled} {chosen}")
    finally:
        finish(app, context, g)


def extra_android_share(playwright, browsers, base: str) -> None:
    """Android's extra share button (navigator.share is stubbed: headless Chromium has none)."""
    g = "extra android share"
    app, context = new_app(playwright, browsers, "android", base, share=True)
    try:
        app.goto()
        page = app.page
        app.open(SMALL)
        app.to_split(9)
        app.to_result()
        check(g, "PDF: share is offered next to save", app.act("share").is_visible() and app.act("save").is_visible())
        app.choose_format("docx")
        check(g, "Word: no share button (Chromium's share sheet refuses the type)", app.act("share").count() == 0)
        app.choose_format("hwpx")
        check(g, "한글: no share button", app.act("share").count() == 0)
        app.choose_format("jpg")
        check(g, "nine JPG pictures: share is offered (at most ten files)", app.act("share").is_visible())
        app.act("share").click()
        page.wait_for_selector('#sheet.is-open[data-name="deliver"]', timeout=120_000)
        check(g, "sharing waits for a tap of its own after the build", page.evaluate("window.__shared.length") == 0
              and "공유하기" in page.inner_text("#sheet"), page.inner_text("#sheet")[:80])
        app.act("deliver").click()
        page.wait_for_function("() => window.__shared.length === 1", timeout=10_000)
        shared = page.evaluate(READ_SHARED)
        check(g, "the nine pictures are shared as they are (no ZIP), files only, inside the tap",
              len(shared["files"]) == 9 and all(item["name"].endswith(".jpg") and item["type"] == "image/jpeg" for item in shared["files"])
              and shared["keys"] == ["files"] and shared["inTap"], str([item["name"] for item in shared["files"]][:3]))
        app.settle(300)
        check(g, "toast says the share sheet was opened", "내보냈어요" in app.status(), app.status())
        # the same build also saves: as one ZIP, made on demand
        with page.expect_download(timeout=30_000) as info:
            app.act("save").click()
            app.settle(1500)
            if app.sheet() == "deliver":
                app.act("deliver").click()
        check(g, "save after share downloads the ZIP of the same pictures", info.value.suggested_filename.endswith(".zip"))
        # twelve pictures are more than the share sheet takes
        app.act("resplit").click()
        app.wait_screen("split")
        page.locator('[data-count="12"]').click()
        page.wait_for_selector("[data-layout]")
        app.to_result()
        check(g, "twelve JPG pictures: share is not offered", app.act("share").count() == 0 and app.act("save").is_visible())
        app.choose_format("pdf")
        check(g, "twelve pieces as one PDF: share is offered again", app.act("share").is_visible())
    finally:
        finish(app, context, g)


def extra_export_failure(playwright, browsers, base: str) -> None:
    """A failed export stays on the result screen with a notice and a retry."""
    g = "extra export failure"
    for profile in ("android", "iphone"):
        app, context = new_app(playwright, browsers, profile, base, fail_export=True)
        try:
            app.goto()
            page = app.page
            app.open(SMALL)
            app.to_split(4)
            app.to_result()
            app.choose_format("docx")
            page.evaluate("window.__failExport = true")
            app.act("save").click()
            page.wait_for_function("() => document.body.dataset.screen === 'result' && document.body.dataset.export === 'failed'", timeout=60_000)
            check(g, f"{profile}: failed export returns to the result screen with data-export=failed", True)
            check(g, f"{profile}: the reason is on screen and in role=status", "만들지 못했어요" in page.inner_text("#result-problem")
                  and "만들지 못했어요" in app.status(), app.status())
            check(g, f"{profile}: nothing was delivered, no sheet", app.sheet() is None)
            page.evaluate("window.__failExport = false")
            files = app.save("retry")
            check(g, f"{profile}: retry builds and delivers", len(files) == 1 and next(iter(files)).endswith(".docx") and app.export_state == "ready",
                  str(list(files)))
            check(g, f"{profile}: the notice is gone after a good export", page.inner_text("#result-problem").strip() == "")
        finally:
            finish(app, context, f"{g} {profile}")


def extra_activation_expired(playwright, browsers, base: str) -> None:
    """When the tap is older than the browser's 5 seconds, the download waits for a fresh tap."""
    g = "extra ready sheet"
    app, context = new_app(playwright, browsers, "android", base, activation=False)
    try:
        app.goto()
        page = app.page
        app.open(SMALL)
        app.to_split(4)
        app.to_result()
        downloads = []
        page.on("download", lambda download: downloads.append(download))
        app.act("save").click()
        page.wait_for_selector('#sheet.is-open[data-name="deliver"]', timeout=60_000)
        app.settle(300)
        check(g, "no download without an active tap; the ready sheet is shown", not downloads and app.export_state == "ready"
              and "파일이 준비됐어요" in page.inner_text("#sheet"), page.inner_text("#sheet")[:60])
        with page.expect_download(timeout=15_000) as info:
            app.act("deliver").click()
        check(g, "the sheet's button delivers", info.value.suggested_filename.endswith(".pdf") and app.sheet() is None)
    finally:
        finish(app, context, g)
    # iOS: a share that the browser refuses (NotAllowedError) brings the sheet back; a closed share sheet is not an error
    app, context = new_app(playwright, browsers, "iphone", base)
    try:
        app.goto()
        page = app.page
        app.open(SMALL)
        app.to_split(2)
        app.to_result()
        page.evaluate("window.__shareOutcome = 'NotAllowedError'")
        app.act("save").click()
        page.wait_for_selector('#sheet.is-open[data-name="deliver"]', timeout=60_000)
        app.act("deliver").click()
        app.settle(500)
        check(g, "NotAllowedError from share() shows the ready sheet again", app.sheet() == "deliver"
              and "한 번만 더" in page.inner_text("#sheet"), page.inner_text("#sheet")[:80])
        page.evaluate("window.__shareOutcome = 'AbortError'")
        before = app.status()
        app.act("deliver").click()
        app.settle(500)
        check(g, "AbortError (sheet closed by the user) is silent", app.sheet() is None and app.status() == before, app.status())
        page.evaluate("window.__shareOutcome = ''")
        app.act("save").click()
        app.settle(500)
        check(g, "then a normal share goes through", "내보냈어요" in app.status(), app.status())
    finally:
        finish(app, context, g + " ios")


def extra_service_worker(playwright, browsers, base: str) -> None:
    g = "extra service worker"
    BumpableHandler.bump = 0
    # desktop profile: there (and in an installed app) the page asks the worker to store the whole engine
    app, context = new_app(playwright, browsers, "desktop", base)
    try:
        page = app.page
        app.goto("")  # with the service worker
        page.wait_for_function("() => navigator.serviceWorker.controller !== null", timeout=30_000)
        scope = page.evaluate("navigator.serviceWorker.getRegistration().then((r) => r && r.scope)")
        check(g, "registered with the app folder as scope", scope == base + "/", str(scope))
        check(g, "no update bar on a first visit", page.locator('[data-action="update"]').count() == 0)
        # after one visit with the engine loaded, everything needed is in the caches
        page.wait_for_function("() => document.body.dataset.engine === 'ready'", timeout=120_000)
        stamped = open(os.path.join(WEB, "sw.js"), encoding="utf-8").read()
        missing = [name for name in os.listdir(os.path.join(WEB, "js")) if f'"js/{name}"' not in stamped]
        if "index.html" in stamped and not missing:
            engine_files = page.evaluate(
                "() => fetch('sw.js').then((r) => r.text()).then((t) => Object.keys(JSON.parse(t.split('const ENGINE = ')[1].split(';')[0])).length)")
            stored = 0
            deadline = time.time() + 180
            while time.time() < deadline:
                stored = page.evaluate("() => caches.open('engine-v1').then((cache) => cache.keys()).then((keys) => keys.length)")
                if stored >= engine_files:
                    break
                page.wait_for_timeout(500)
            check(g, "engine files stored for offline use after the first run", stored >= engine_files, f"{stored} of {engine_files}")
            context.set_offline(True)
            page.reload()
            page.wait_for_selector("body[data-ready]", timeout=30_000)
            check(g, "offline reload: the app shell comes from the service worker", app.screen == "home"
                  and page.evaluate("getComputedStyle(document.querySelector('.btn--primary')).backgroundColor") == "rgb(31, 107, 240)")
            check(g, "offline: the engine starts from the cache and a picture opens", app.open(SMALL) == "confirm", str(app.error_code()))
            app.home()
            check(g, "offline: a PDF opens too (its tools were stored)", app.open(PDF5) == "pick", str(app.error_code()))
            context.set_offline(False)
            app.home()
        else:
            check(g, "sw.js lists the app shell (run tools/build_web.py)", False, f"not in the SHELL list: {missing or 'index.html'}")
        BumpableHandler.bump = int(time.time())
        page.evaluate("navigator.serviceWorker.getRegistration().then((r) => r.update())")
        page.wait_for_selector('[data-action="update"]', timeout=30_000)
        check(g, "a new build shows the quiet update bar on home", "새 버전이 있어요" in page.inner_text("#home-notices"))
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, "90-home-update-bar-1280-light.png"))
        with page.expect_navigation(timeout=30_000):
            app.act("update").click()
        page.wait_for_selector("body[data-ready]", timeout=30_000)
        app.settle(500)
        check(g, "update reloads once and the bar is gone", page.locator('[data-action="update"]').count() == 0 and app.screen == "home")
    finally:
        BumpableHandler.bump = 0
        finish(app, context, g)


EXTRAS = [extra_inapp, extra_install_and_policy, extra_first_run_and_offline, extra_desktop, extra_big_pdf,
          extra_capped_and_tiny, extra_android_share, extra_export_failure, extra_activation_expired, extra_service_worker]


def run_extras(playwright, browsers, base: str) -> None:
    for extra in EXTRAS:
        print(f"== {extra.__name__}", flush=True)
        try:
            extra(playwright, browsers, base)
        except Exception as exc:  # noqa: BLE001
            check(extra.__name__, "ran to the end", False, f"{type(exc).__name__}: {exc} | " + traceback.format_exc().strip().splitlines()[-3])


# ---------------------------------------------------------------------------
# Part 3: layout tour (overflow, touch targets, screenshots)
# ---------------------------------------------------------------------------
OVERFLOW_JS = "document.documentElement.scrollWidth - document.documentElement.clientWidth"
SMALL_TARGETS_JS = """
() => {
  // what can be tapped right now: the open sheet, or else the app (segments reach 44px through an invisible extension)
  const root = document.querySelector('#sheet.is-open') || document.querySelector('#app');
  return Array.from(root.querySelectorAll('button, a[href], [role="radio"], [role="checkbox"]'))
    .filter((el) => !el.closest('.seg') && !el.hidden && getComputedStyle(el).display !== 'none')
    .map((el) => ({ el, box: el.getBoundingClientRect() }))
    .filter(({ box }) => box.width > 0 && (box.width < 43.5 || box.height < 43.5))
    .map(({ el, box }) => `${el.dataset.action || el.className}:${Math.round(box.width)}x${Math.round(box.height)}`);
}
"""


class Gate:
    """Holds matching requests back until release() - a slow network without blocking the test."""

    def __init__(self, page, pattern: str):
        self.held = []
        self.open = False
        page.route(pattern, self._handle)

    def _handle(self, route) -> None:
        if self.open:
            route.continue_()
        else:
            self.held.append(route)

    def release(self) -> None:
        self.open = True
        for route in self.held:
            route.continue_()
        self.held.clear()


def make_sample(playwright, browsers, base: str) -> str:
    """A pleasant 4032 x 3024 picture for the screenshots: the app's own home illustration, rendered large."""
    if os.path.exists(SAMPLE):
        return SAMPLE
    from PIL import Image
    os.makedirs(os.path.dirname(SAMPLE), exist_ok=True)
    if "chromium" not in browsers:
        browsers["chromium"] = playwright.chromium.launch()
    context = browsers["chromium"].new_context(viewport={"width": 1008, "height": 756}, device_scale_factor=4)
    page = context.new_page()
    page.goto(f"{base}/css/demo.svg")
    png = os.path.join(os.path.dirname(SAMPLE), "sample.png")
    page.screenshot(path=png)
    context.close()
    Image.open(png).convert("RGB").save(SAMPLE, quality=90)
    os.remove(png)
    return SAMPLE


class Tour:
    def __init__(self, app: App, tag: str, group: str, shots: bool, only: set[str] | None = None):
        self.app = app
        self.tag = tag
        self.group = group
        self.shots = shots
        self.only = only
        self.index = 0

    def at(self, name: str, full: bool = False, targets: bool = True) -> None:
        app = self.app
        app.page.mouse.move(2, 2)  # a touch screen has no pointer resting on the last button
        app.settle(420)
        overflow = app.page.evaluate(OVERFLOW_JS)
        check(self.group, f"{name}: no horizontal overflow", overflow <= 1, f"{overflow}px")
        if targets:
            small = app.page.evaluate(SMALL_TARGETS_JS)
            check(self.group, f"{name}: touch targets at least 44px", not small, ", ".join(small[:6]))
        self.index += 1
        if not self.shots or (self.only is not None and name.split("-")[0] not in self.only):
            return
        os.makedirs(SHOTS, exist_ok=True)
        path = os.path.join(SHOTS, f"{self.index:02d}-{name}-{self.tag}.png")
        app.page.screenshot(path=path)
        if full:
            app.page.screenshot(path=path.replace(".png", "-full.png"), full_page=True)


def tour_main(playwright, browsers, base: str, *, profile: str, viewport, scheme: str, tag: str, shots: bool,
              only: set[str] | None = None, landscape: bool = False) -> None:
    """Walks every screen and state once in one context."""
    group = f"layout {tag}"
    sample = make_sample(playwright, browsers, base)
    ua = ANDROID_UA if profile == "android" else None
    # activation=False: the "files are ready" sheet always shows, whatever the speed of this machine
    app, context = new_app(playwright, browsers, profile, base, viewport=viewport, scheme=scheme, ua=ua, scale=2, activation=False,
                           fail_export=True)
    tour = Tour(app, tag, group, shots, only)
    page = app.page
    try:
        slow_engine = Gate(page, "**/python_stdlib.zip")
        slow_pdf = Gate(page, "**/pymupdf*.whl")
        app.goto()
        tour.at("home")
        if profile == "desktop":
            page.evaluate("""() => {
              const data = new DataTransfer();
              data.items.add(new File(['x'], 'a.png', { type: 'image/png' }));
              window.__drag = data;
              window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: data, bubbles: true, cancelable: true }));
            }""")
            tour.at("home-dragover")
            page.evaluate("() => window.dispatchEvent(new DragEvent('dragleave', { dataTransfer: window.__drag, bubbles: true, cancelable: true }))")
        if profile == "android":
            app.act("pick").click()
            tour.at("home-picker")
            page.keyboard.press("Escape")
            app.act("install").click()
            tour.at("home-install-android")
            page.keyboard.press("Escape")
            app.settle(300)
        # first run: the engine is still downloading
        app.pick(sample)
        app.wait_screen("prep", timeout=5_000)
        page.wait_for_function("() => /[1-9]/.test((document.querySelector('#prep-figure') || {}).textContent || '')", timeout=20_000)
        app.settle(600)
        tour.at("prep-first-run")
        slow_engine.release()
        app.wait_screen("confirm", timeout=120_000)
        app.wait_preview()
        tour.at("confirm-photo")
        app.act("confirm-yes").click()
        app.wait_screen("split")
        tour.at("split-empty")
        for count in (6, 18, 12, 9):
            page.locator(f'[data-count="{count}"]').click()
            page.wait_for_selector("[data-layout]")
            tour.at(f"split-{count}", full=count == 9)
        app.to_result()
        tour.at("result-pdf", full=True)
        app.choose_format("jpg")
        tour.at("result-jpg")
        app.act("save").click()
        app.wait_screen("work", timeout=15_000)
        page.wait_for_function("() => Number(document.querySelector('#work-bar').getAttribute('aria-valuenow')) >= 3", timeout=180_000)
        tour.at("work", targets=False)
        page.go_back()
        page.wait_for_selector('#sheet.is-open[data-name="cancel"]', timeout=10_000)
        tour.at("work-cancel-question")
        page.locator('#sheet [data-action="close-sheet"]').last.click()
        page.wait_for_selector('#sheet.is-open[data-name="deliver"]', timeout=420_000)
        tour.at("result-ready-sheet")
        with page.expect_download(timeout=20_000):
            app.act("deliver").click()
        app.settle(250)
        tour.at("result-saved")
        # an export that fails keeps the choice and says so
        page.evaluate("window.__failExport = true")
        app.choose_format("docx")
        app.act("save").click()
        page.wait_for_function("() => document.body.dataset.screen === 'result' && document.body.dataset.export === 'failed'", timeout=60_000)
        page.evaluate("() => { window.__failExport = false; document.querySelector('#result-problem').scrollIntoView({ block: 'center' }); }")
        tour.at("result-export-failed")
        app.choose_format("jpg")
        # 18 pieces must still fit as a mosaic
        app.act("resplit").click()
        app.wait_screen("split")
        page.locator('[data-count="18"]').click()
        page.wait_for_selector('[data-layout="6x3"]')
        app.to_result()
        tour.at("result-18")
        app.act("restart").click()
        app.wait_screen("home")

        # multi-page PDF (the PDF tools download once)
        app.pick(PDF5)
        app.wait_screen("prep", timeout=10_000)
        page.wait_for_function("() => document.querySelector('#prep-status').textContent.includes('PDF')", timeout=15_000)
        tour.at("prep-pdf-tools")
        slow_pdf.release()
        app.wait_screen("pick", timeout=120_000)
        page.wait_for_function("() => document.querySelectorAll('.thumb img.is-in').length === 5", timeout=60_000)
        tour.at("pick-pages")
        page.locator("[data-source]").nth(2).click()
        tour.at("pick-pages-chosen")
        app.act("next").click()
        app.wait_screen("confirm")
        app.wait_preview()
        tour.at("confirm-page")
        app.home()

        # several pictures at once, one of them broken
        app.open(sample, PORTRAIT, SQUARE, BROKEN, PANORAMA)
        page.wait_for_function("() => document.querySelectorAll('.thumb img.is-in').length === 4", timeout=60_000)
        tour.at("pick-photos-notice")
        page.locator("[data-source]").nth(1).click()
        app.act("next").click()
        app.wait_screen("confirm")
        app.wait_preview()
        tour.at("confirm-portrait")
        app.act("confirm-yes").click()
        app.wait_screen("split")
        page.locator('[data-count="12"]').click()
        page.wait_for_selector("[data-layout]")
        tour.at("split-12-portrait")
        app.home()

        # pictures inside documents, and a mixed pick
        app.open(PPTX3)
        page.wait_for_function("() => document.querySelectorAll('.thumb img.is-in').length === 3", timeout=60_000)
        tour.at("pick-embedded")
        app.home()
        app.open(DOCX2, sample, PDF5, SQUARE)
        app.settle(900)
        tour.at("pick-mixed", full=True)
        app.home()

        # a panorama cut into 16
        app.open(PANORAMA)
        app.wait_preview()
        app.act("confirm-yes").click()
        app.wait_screen("split")
        page.locator('[data-count="16"]').click()
        page.wait_for_selector("[data-layout]")
        tour.at("split-16-panorama")
        app.to_result()
        tour.at("result-16-panorama")
        app.home()

        # errors
        for name, label in ((FAKE, "unsupported"), (BROKEN, "corrupt"), (TEXT_DOCX, "no-image"), (HUGE, "too-large"), (EMPTY, "empty")):
            app.open(name)
            tour.at(f"error-{label}")
        app.open(LOCKED)
        tour.at("error-password")
        page.fill("#password", "hanbit53")
        app.act("unlock").click()
        page.wait_for_function("() => (document.querySelector('[data-error-code]') || { dataset: {} }).dataset.errorCode === 'WRONG_PASSWORD'", timeout=30_000)
        tour.at("error-password-wrong")
        app.home()
    finally:
        finish(app, context, group)


def tour_offline(playwright, browsers, base: str, *, viewport, scheme: str, tag: str, shots: bool) -> None:
    group = f"layout {tag}"
    app, context = new_app(playwright, browsers, "android", base, viewport=viewport, scheme=scheme, ua=ANDROID_UA, scale=2)
    tour = Tour(app, tag, group, shots)
    tour.index = 60
    try:
        app.page.route("**/pyodide.asm.wasm", lambda route: route.abort())
        app.goto()
        app.pick(SMALL)
        app.wait_screen("error", timeout=120_000)
        tour.at("error-offline")
        app.problems = []
    finally:
        finish(app, context, group)
    app, context = new_app(playwright, browsers, "android", base, viewport=viewport, scheme=scheme, ua=KAKAO_UA, scale=2)
    tour = Tour(app, tag, group, shots)
    tour.index = 61
    try:
        app.goto()
        tour.at("inapp-sheet")
        app.page.locator('#sheet .btn--ghost[data-action="close-sheet"]').click()
        tour.at("home-inapp-banner")
    finally:
        finish(app, context, group)


def tour_ios(playwright, browsers, base: str, *, scheme: str, tag: str, shots: bool) -> None:
    """The iOS-only states, in WebKit."""
    group = f"layout {tag}"
    sample = make_sample(playwright, browsers, base)
    app, context = new_app(playwright, browsers, "iphone", base, viewport=(390, 844), scheme=scheme, scale=2)
    tour = Tour(app, tag, group, shots)
    tour.index = 70
    page = app.page
    try:
        app.goto()
        tour.at("ios-home")
        app.act("install").click()
        tour.at("ios-install")
        page.locator('#sheet .sheet__foot [data-action="close-sheet"]').click()
        app.open(sample)
        app.wait_preview()
        tour.at("ios-confirm")
        app.to_split(9)
        tour.at("ios-split-9")
        app.to_result()
        tour.at("ios-result-pdf", full=True)
        app.choose_format("jpg")
        page.locator('[data-action="zip-toggle"]').scroll_into_view_if_needed()
        tour.at("ios-result-jpg-zip-option", full=True)
        app.act("save").click()
        page.wait_for_selector('#sheet.is-open[data-name="deliver"]', timeout=180_000)
        tour.at("ios-ready-sheet")
        app.act("deliver").click()
        app.settle(300)
        tour.at("ios-shared")
    finally:
        finish(app, context, group)


def run_layout(playwright, browsers, base: str, shots: bool, tags: list[str] | None = None) -> None:
    home_split_result = {"home", "split", "result"}
    plans = [
        dict(profile="android", viewport=(390, 844), scheme="light", tag="390-light", only=None),
        dict(profile="android", viewport=(390, 844), scheme="dark", tag="390-dark", only=None),
        dict(profile="android", viewport=(320, 640), scheme="light", tag="320-light", only=home_split_result),
        dict(profile="android", viewport=(320, 640), scheme="dark", tag="320-dark", only=home_split_result),
        dict(profile="desktop", viewport=(768, 1024), scheme="light", tag="768-light", only=home_split_result),
        dict(profile="desktop", viewport=(1280, 800), scheme="light", tag="1280-light", only=home_split_result | {"confirm", "pick", "error", "work", "prep"}),
        dict(profile="desktop", viewport=(1280, 800), scheme="dark", tag="1280-dark", only=home_split_result),
        dict(profile="android", viewport=(844, 390), scheme="light", tag="landscape-light", only=home_split_result | {"confirm"}),
    ]
    for plan in plans:
        if tags and plan["tag"] not in tags:
            continue
        print(f"== layout: {plan['tag']}", flush=True)
        started = time.time()
        try:
            tour_main(playwright, browsers, base, shots=shots, **plan)
        except Exception as exc:  # noqa: BLE001
            check(f"layout {plan['tag']}", "tour ran to the end", False,
                  f"{type(exc).__name__}: {exc} | " + traceback.format_exc().strip().splitlines()[-3])
        print(f"   {time.time() - started:.0f}s", flush=True)
    for scheme in ("light", "dark"):
        if tags and f"390-{scheme}" not in tags:
            continue
        for runner, kwargs in ((tour_offline, dict(viewport=(390, 844))), (tour_ios, {})):
            try:
                runner(playwright, browsers, base, scheme=scheme, tag=f"390-{scheme}", shots=shots, **kwargs)
            except Exception as exc:  # noqa: BLE001
                check(f"layout {runner.__name__} {scheme}", "tour ran to the end", False,
                      f"{type(exc).__name__}: {exc} | " + traceback.format_exc().strip().splitlines()[-3])


# ---------------------------------------------------------------------------
def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=["flows", "extras", "layout"], action="append")
    parser.add_argument("--profile", choices=["android", "iphone"], action="append")
    parser.add_argument("--base", help="use a server that is already running instead of starting one")
    parser.add_argument("--no-shots", action="store_true", help="layout checks without writing screenshots")
    parser.add_argument("--tag", action="append", help="layout: only these tours, e.g. 390-light, 1280-dark, landscape-light")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose
    parts = args.only or ["flows", "extras", "layout"]
    profiles = args.profile or ["android", "iphone"]

    # like tests/e2e/conftest.py: package the engine and stamp the service worker's file list,
    # so the offline checks see the files as they are now
    import subprocess
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_web.py")], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
    server = None
    base = args.base.rstrip("/") if args.base else None
    if not base:
        server, base = start_server()
    print("serving", base, flush=True)
    started = time.time()
    browsers: dict = {}
    try:
        with sync_playwright() as playwright:
            try:
                if "flows" in parts:
                    run_flows(playwright, browsers, base, profiles)
                if "extras" in parts:
                    if args.base:
                        EXTRAS.remove(extra_service_worker)  # needs this script's own server
                    run_extras(playwright, browsers, base)
                if "layout" in parts:
                    run_layout(playwright, browsers, base, shots=not args.no_shots, tags=args.tag)
            finally:
                for browser in browsers.values():
                    browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()

    failed = [item for item in RESULTS if not item[2]]
    print()
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed, {len(RESULTS)} checks in {time.time() - started:.0f}s")
    for group, label, _ok, detail in failed:
        print(f"  FAIL [{group}] {label}: {detail[:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
