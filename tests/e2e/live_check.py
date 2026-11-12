"""Drive the published app (real HTTPS host, service worker on) in both phone profiles.

    .venv/Scripts/python tests/e2e/live_check.py [https://geunminmaker.github.io/split-image]

For each profile: three complete flows with different inputs and output formats, every received
file checked by the independent verifier; then (Chromium) a second, offline visit.
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
from conftest import SHARE_STUB  # noqa: E402
from verify import verify_outputs  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else "https://geunminmaker.github.io/split-image").rstrip("/")
CORPUS = os.path.join(ROOT, "tests", "corpus")
SHOTS = os.path.join(ROOT, "tests", "e2e", "out", "live")

# file, candidate index (None = single picture), count, layout, format, paper
FLOWS = [
    ("01_사진_가로_4032x3024.jpg", None, 9, "3x3", "pdf", "original"),
    ("21_여러쪽_5쪽_크기제각각.pdf", 2, 6, "3x2", "hwpx", "original"),
    ("30_워드_그림2장.docx", 1, 4, "2x2", "docx", "a4"),
    ("04_투명배경_로고_1200x800.png", None, 4, "2x2", "png", None),
]
PROFILES = {"android": ("chromium", "Pixel 7"), "iphone": ("webkit", "iPhone 13")}


def run_flow(driver: AppDriver, case) -> list[str]:
    name, pick, count, layout, fmt, paper = case
    path = os.path.join(CORPUS, name)
    screen = driver.pick_files([path])
    if pick is None:
        assert screen == "confirm", (screen, driver.error_code())
    else:
        assert screen == "pick", (screen, driver.error_code())
        driver.choose_source(pick)
    driver.confirm(True)
    selected = driver.choose_split(count)
    assert selected == layout, (selected, layout)
    driver.split_yes()
    driver.choose_output(fmt, paper)
    files = driver.save()
    cols, rows = (int(v) for v in layout.split("x"))
    zipped = fmt in ("jpg", "png") and driver.profile != "iphone"
    return verify_outputs(path, files, rows=rows, cols=cols, fmt=fmt, paper=paper or "original", pick=pick or 0, zipped=zipped)


def main() -> int:
    failures = 0
    with sync_playwright() as p:
        for profile, (engine, device) in PROFILES.items():
            browser = getattr(p, engine).launch()
            options = dict(p.devices[device])
            options.update(accept_downloads=True, locale="ko-KR")
            context = browser.new_context(**options)
            if profile == "iphone":
                context.add_init_script(SHARE_STUB)
            page = context.new_page()
            driver = AppDriver(page, BASE, profile, shots_dir=os.path.join(SHOTS, profile))
            started = time.time()
            driver.open("")
            info = page.evaluate("""() => ({ secure: isSecureContext, url: location.href,
                sw: 'serviceWorker' in navigator, standalone: matchMedia('(display-mode: standalone)').matches })""")
            print(f"[{profile}] {info}")
            driver.shot("home")
            for case in FLOWS:
                t0 = time.time()
                try:
                    problems = run_flow(driver, case)
                except Exception as exc:  # noqa: BLE001
                    problems = [f"{type(exc).__name__}: {str(exc)[:300]}"]
                    driver.shot("failure")
                failures += bool(problems)
                print(f"  {'OK ' if not problems else 'BAD'} {case[0][:30]:30s} -> {case[2]:2d} {case[4]:5s} {time.time() - t0:5.1f}s {problems[:2]}")
                driver.open("")
            controlled = page.evaluate("() => !!(navigator.serviceWorker && navigator.serviceWorker.controller)")
            keys = page.evaluate("() => caches.open('engine-v1').then((c) => c.keys()).then((k) => k.map((r) => r.url.split('/').pop().split('?')[0]))")
            print(f"  service worker controls the page: {controlled}; engine files kept: {len(keys)} {sorted(keys)}")
            if profile == "android":
                context.set_offline(True)
                page.reload()
                driver.wait_screen("home")
                t0 = time.time()
                try:
                    problems = run_flow(driver, FLOWS[0])
                    problems += run_flow_after_home(driver, FLOWS[1])
                except Exception as exc:  # noqa: BLE001
                    problems = [f"{type(exc).__name__}: {str(exc)[:300]}"]
                failures += bool(problems)
                print(f"  {'OK ' if not problems else 'BAD'} offline: photo -> pdf, then PDF page -> hwpx  {time.time() - t0:5.1f}s {problems[:2]}")
                context.set_offline(False)
            console = [line for line in driver.console_problems() if "ERR_INTERNET_DISCONNECTED" not in line]
            if console:
                failures += 1
                print("  console problems:", console[:4])
            print(f"  total {time.time() - started:.0f}s")
            browser.close()
    print("FAILURES:", failures)
    return 1 if failures else 0


def run_flow_after_home(driver: AppDriver, case) -> list[str]:
    driver.page.reload()
    driver.wait_screen("home")
    return run_flow(driver, case)


if __name__ == "__main__":
    sys.exit(main())
