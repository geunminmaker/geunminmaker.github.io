"""Drives the real app through its DOM contract (see docs in tests/e2e/README.md).

The driver only does what a person can do: pick files, tap buttons, wait for
screens.  It never calls application functions directly.
"""
from __future__ import annotations

import base64
import os
import re
import time

SCREENS = ("home", "prep", "pick", "confirm", "split", "work", "result", "error")

#: console noise that is not a defect of the app
IGNORED_CONSOLE = (
    re.compile(r"favicon", re.I),
    re.compile(r"Failed to load resource.*sw\.js", re.I),
)

READ_SHARED = """
async () => {
  const batch = window.__shared[window.__shared.length - 1] || [];
  const out = [];
  for (const file of batch) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let text = "";
    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    out.push({ name: file.name, type: file.type, b64: btoa(text) });
  }
  return out;
}
"""


class AppDriver:
    def __init__(self, page, base_url: str, profile: str, shots_dir: str | None = None):
        self.page = page
        self.base_url = base_url
        self.profile = profile
        self.shots_dir = shots_dir
        self._shot = 0
        self._console: list[str] = []
        self.downloads: list = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda error: self._console.append(f"pageerror: {error}"))
        page.on("download", lambda download: self.downloads.append(download))

    # ------------------------------------------------------------ plumbing
    def _on_console(self, message) -> None:
        if message.type != "error":
            return
        text = message.text
        if any(pattern.search(text) for pattern in IGNORED_CONSOLE):
            return
        self._console.append(text)

    def console_problems(self) -> list[str]:
        return list(self._console)

    def shot(self, name: str) -> None:
        if not self.shots_dir:
            return
        os.makedirs(self.shots_dir, exist_ok=True)
        self._shot += 1
        self.page.screenshot(path=os.path.join(self.shots_dir, f"{self._shot:02d}-{name}.png"))

    @property
    def screen(self) -> str:
        return self.page.evaluate("document.body.dataset.screen || ''")

    def wait_screen(self, *names: str, timeout: float = 180_000) -> str:
        self.page.wait_for_function("names => names.includes(document.body.dataset.screen)", arg=list(names), timeout=timeout)
        self.page.wait_for_timeout(400)  # let the enter transition finish (and the double-tap guard pass)
        return self.screen

    def click(self, selector: str, timeout: float = 15_000) -> None:
        self.page.locator(selector).first.click(timeout=timeout)

    def visible(self, selector: str) -> bool:
        locator = self.page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()

    def text(self) -> str:
        return self.page.inner_text("body")

    # ------------------------------------------------------------ steps
    def open(self, query: str = "?nosw") -> None:
        self.page.goto(f"{self.base_url}/{query}")
        self.wait_screen("home", timeout=30_000)

    def pick_files(self, paths: list[str]) -> str:
        """Hand files to the app the way the system picker would; returns the next screen."""
        self.page.set_input_files("#input-files", paths)
        return self.wait_screen("pick", "confirm", "error")

    def choose_source(self, index: int) -> None:
        """On the thumbnail screen: tap the index-th candidate, then 다음."""
        items = self.page.locator("[data-source]")
        items.nth(index).scroll_into_view_if_needed()
        items.nth(index).click()
        self.shot("pick-chosen")
        self.click('[data-action="next"]')
        self.wait_screen("confirm")

    def source_count(self) -> int:
        return self.page.locator("[data-source]").count()

    def unlock(self, password: str) -> str:
        self.page.fill("#password", password)
        self.click('[data-action="unlock"]')
        self.page.wait_for_timeout(300)
        return self.wait_screen("pick", "confirm", "error")

    def confirm(self, yes: bool = True) -> str:
        self.shot("confirm")
        self.click('[data-action="confirm-yes"]' if yes else '[data-action="confirm-no"]')
        return self.wait_screen("split" if yes else "pick", "home")

    def choose_split(self, count: int, layout: str | None = None) -> str:
        """Tap a piece count (and optionally a layout card); returns the selected layout key."""
        self.click(f'[data-count="{count}"]')
        self.page.wait_for_selector("[data-layout]", timeout=10_000)
        if layout:
            card = self.page.locator(f'[data-layout="{layout}"]').first
            card.scroll_into_view_if_needed()
            card.click()
        self.page.wait_for_timeout(250)
        selected = self.page.evaluate(
            "() => { const el = document.querySelector('[data-layout][aria-checked=\"true\"], [data-layout][aria-pressed=\"true\"]'); return el ? el.dataset.layout : null; }"
        )
        self.shot(f"split-{count}")
        return selected

    def layouts_shown(self) -> list[str]:
        return self.page.evaluate("() => Array.from(document.querySelectorAll('[data-layout]')).map((el) => el.dataset.layout)")

    def split_yes(self) -> None:
        self.click('[data-action="split-yes"]')
        self.wait_screen("result")
        self.shot("result")

    def choose_output(self, fmt: str, paper: str | None = None) -> None:
        card = self.page.locator(f'[data-format="{fmt}"]').first
        card.scroll_into_view_if_needed()
        card.click()
        if paper:
            self.page.locator(f'[data-paper="{paper}"]').first.click()
        self.page.wait_for_timeout(150)

    def save(self, timeout: float = 600_000) -> dict[str, bytes]:
        """Tap 저장하기, wait for the files to be built, and collect what the person receives."""
        self.downloads.clear()
        if self.profile == "iphone":
            self.page.evaluate("window.__shared = []")
        self.click('[data-action="save"]')
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            state = self.page.evaluate("document.body.dataset.export || ''")
            if state == "failed":
                raise AssertionError("export failed: " + self.text()[-300:])
            received = len(self.downloads) if self.profile != "iphone" else self.page.evaluate("window.__shared.length")
            if received:
                break
            if state == "ready" and self.visible('[data-action="deliver"]'):
                self.shot("ready-sheet")
                self.click('[data-action="deliver"]')
            self.page.wait_for_timeout(200)
        else:
            raise AssertionError("nothing was delivered in time")
        self.page.wait_for_timeout(400)
        self.shot("saved")
        if self.profile == "iphone":
            assert not self.page.evaluate("!!window.__shareMixed"), "files must be shared without title/text/url"
            return {item["name"]: base64.b64decode(item["b64"]) for item in self.page.evaluate(READ_SHARED)}
        files = {}
        for download in self.downloads:
            path = download.path()
            with open(path, "rb") as handle:
                files[download.suggested_filename] = handle.read()
        return files

    def error_code(self) -> str | None:
        return self.page.evaluate(
            "() => { const el = document.querySelector('[data-error-code]'); return el ? el.dataset.errorCode : null; }"
        )

    def no_horizontal_overflow(self) -> bool:
        return self.page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
