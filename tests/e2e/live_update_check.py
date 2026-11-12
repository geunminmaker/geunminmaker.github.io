"""Check a real update on the published site with a browser profile that keeps its data.

    .venv/Scripts/python tests/e2e/live_update_check.py before <profile-dir>   # run before deploying
    .venv/Scripts/python tests/e2e/live_update_check.py after  <profile-dir>   # run after deploying

"before": visits the site, splits a photo and a PDF page (so the engine and the PDF tools are kept).
"after":  opens the same profile again - the old build must still start, the update notice must
          appear, and after taking the update the new build must work, also offline.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from playwright.sync_api import sync_playwright  # noqa: E402

from app_driver import AppDriver  # noqa: E402
from live_check import FLOWS, run_flow  # noqa: E402

BASE = "https://geunminmaker.github.io/split-image"
BUILD = "() => caches.keys().then((names) => names.filter((n) => n.startsWith('shell-')))"
KEYS = "() => caches.open('engine-v1').then((c) => c.keys()).then((k) => k.map((r) => r.url.split('/').pop()))"


def main() -> int:
    phase, profile_dir = sys.argv[1], os.path.abspath(sys.argv[2])
    failures = 0
    with sync_playwright() as p:
        options = dict(p.devices["Pixel 7"])
        options.pop("default_browser_type", None)  # not an option of a persistent context
        options.update(accept_downloads=True, locale="ko-KR")
        context = p.chromium.launch_persistent_context(profile_dir, **options)
        page = context.pages[0] if context.pages else context.new_page()
        driver = AppDriver(page, BASE, "android")
        driver.open("")
        page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=60_000)
        print(f"[{phase}] shell caches: {page.evaluate(BUILD)}")
        if phase == "before":
            for case in (FLOWS[0], FLOWS[1]):
                problems = run_flow(driver, case)
                failures += bool(problems)
                print(f"  {'OK ' if not problems else 'BAD'} {case[0][:28]} -> {case[4]} {problems[:2]}")
                driver.open("")
            print("  engine files kept:", sorted(page.evaluate(KEYS)))
        else:
            # the old build is still in charge on this first load; the new one installs behind it
            started = time.time()
            page.wait_for_selector('[data-action="update"]', timeout=180_000)
            print(f"  update notice after {time.time() - started:.0f}s; caches now: {page.evaluate(BUILD)}")
            context.set_offline(True)        # everything the new build needs must already be on the device
            with page.expect_navigation(timeout=60_000):
                driver.click('[data-action="update"]')
            driver.wait_screen("home")
            print("  after the update:", page.evaluate(BUILD))
            try:
                problems = run_flow(driver, FLOWS[0])
                page.reload()
                driver.wait_screen("home")
                problems += run_flow(driver, FLOWS[1])
            except Exception as exc:  # noqa: BLE001
                problems = [f"{type(exc).__name__}: {str(exc)[:300]}"]
            failures += bool(problems)
            print(f"  {'OK ' if not problems else 'BAD'} offline right after the update: photo -> pdf, PDF page -> hwpx {problems[:2]}")
            context.set_offline(False)
            print("  engine files kept:", sorted(page.evaluate(KEYS)))
        console = [line for line in driver.console_problems() if "ERR_INTERNET_DISCONNECTED" not in line]
        if console:
            failures += 1
            print("  console problems:", console[:4])
        context.close()
    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
