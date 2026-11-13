"""Phones stay upright: when the system turns the screen, the page is turned back.

A phone on its side is emulated (landscape viewport, the screen angle the system would report);
the app must look and work exactly as it does upright: the page box is as wide as the viewport is
high, the portrait layout is used, everything can be reached, and a complete flow gives the right
files.  Tablets are left alone.
"""
from __future__ import annotations

import os

import pytest

from app_driver import AppDriver
from conftest import CORPUS, OUT, SHARE_STUB
from verify import verify_outputs

PHOTO = os.path.join(CORPUS, "01_사진_가로_4032x3024.jpg")
PDF = os.path.join(CORPUS, "21_여러쪽_5쪽_크기제각각.pdf")

# what a real phone reports; the emulated browsers do not all do it by themselves
ANGLE_STUB = """
(() => {
  const angle = %d;
  const type = angle === 0 ? 'portrait-primary' : angle === 180 ? 'portrait-secondary' : angle === 90 ? 'landscape-primary' : 'landscape-secondary';
  const target = window.screen.orientation || {};
  try {
    Object.defineProperty(target, 'angle', { configurable: true, get: () => angle });
    Object.defineProperty(target, 'type', { configurable: true, get: () => type });
    if (!window.screen.orientation) Object.defineProperty(window.screen, 'orientation', { configurable: true, value: target });
  } catch (error) { /* keep what the browser has */ }
  try { Object.defineProperty(window, 'orientation', { configurable: true, get: () => (angle === 270 ? -90 : angle) }); } catch (error) {}
})();
"""

GEOMETRY = """() => {
  const body = document.body.getBoundingClientRect();
  const app = document.querySelector('#app');
  const title = document.querySelector('.h1').getBoundingClientRect();
  const dock = document.querySelector('#dock').getBoundingClientRect();
  return {
    turn: document.documentElement.dataset.turn || '',
    viewport: [window.innerWidth, window.innerHeight],
    body: [Math.round(body.left), Math.round(body.top), Math.round(body.width), Math.round(body.height)],
    box: [document.body.offsetWidth, document.body.offsetHeight],
    app: [app.clientWidth, app.clientHeight, app.scrollHeight],
    boxHeight: document.body.offsetHeight,
    title: [Math.round(title.left), Math.round(title.top), Math.round(title.width), Math.round(title.height)],
    dock: [Math.round(dock.left), Math.round(dock.top), Math.round(dock.width), Math.round(dock.height)],
    wide: getComputedStyle(document.querySelector('.w-only')).display !== 'none',
    pageScroll: document.documentElement.scrollWidth > window.innerWidth + 1 || document.documentElement.scrollHeight > window.innerHeight + 1,
  };
}"""

PHONES = {
    "android": {"engine": "chromium", "device": "Pixel 7"},
    "iphone": {"engine": "webkit", "device": "iPhone 13"},
}


def open_phone(browsers, playwright_instance, base_url, name, angle, request, size=None):
    spec = PHONES[name]
    options = dict(playwright_instance.devices[spec["device"]])
    width, height = size or (options["viewport"]["width"], options["viewport"]["height"])
    options["viewport"] = {"width": width, "height": height}
    if angle in (90, 270):
        options["viewport"] = {"width": height, "height": width}
    options.update(accept_downloads=True, locale="ko-KR")
    context = browsers(spec["engine"]).new_context(**options)
    context.add_init_script(ANGLE_STUB % angle)
    if name == "iphone":
        context.add_init_script(SHARE_STUB)
    page = context.new_page()
    driver = AppDriver(page, base_url, name, shots_dir=os.path.join(OUT, "shots", "upright", request.node.name))
    driver.open()
    return driver, context, (width, height)


@pytest.mark.parametrize("name", list(PHONES))
@pytest.mark.parametrize("angle,turn", [(90, "left"), (270, "right"), (180, "over")])
def test_a_turned_phone_shows_the_upright_page(browsers, playwright_instance, base_url, request, name, angle, turn):
    driver, context, (width, height) = open_phone(browsers, playwright_instance, base_url, name, angle, request)
    try:
        page = driver.page
        g = page.evaluate(GEOMETRY)
        driver.shot("home")
        assert g["turn"] == turn, g
        assert g["body"][:2] == [0, 0], g                       # the turned box covers the viewport exactly
        assert abs(g["body"][2] - g["viewport"][0]) <= 1 and abs(g["body"][3] - g["viewport"][1]) <= 1, g
        assert abs(g["box"][0] - width) <= 1, g                  # laid out as wide as the upright phone
        assert g["app"][0] <= width and not g["wide"], g        # portrait layout, not the two-pane one
        assert not g["pageScroll"], g
        if turn == "left":     # the headline is near the left edge of the screen, running upwards
            assert g["title"][0] < g["viewport"][0] / 2 and g["title"][3] > g["title"][2], g
            assert g["dock"][0] > g["viewport"][0] / 2, g       # the action button is at the far end (the phone's bottom)
        elif turn == "right":
            assert g["title"][0] > g["viewport"][0] / 2 and g["title"][3] > g["title"][2], g
            assert g["dock"][0] < g["viewport"][0] / 2, g
        else:
            assert g["title"][1] > g["viewport"][1] / 2 and g["dock"][1] < g["viewport"][1] / 2, g

        # a whole flow, in the turned page: every control must be reachable and the files right
        assert driver.pick_files([PHOTO]) == "confirm"
        driver.confirm(True)
        assert driver.choose_split(6) == "3x2"
        # the action area is the last row of the box: its far edge is the box's (the phone's) bottom edge
        edge = page.evaluate("() => { const d = document.querySelector('#dock'); return [d.offsetTop + d.offsetHeight, document.body.offsetHeight]; }")
        assert abs(edge[0] - edge[1]) <= 1, edge
        driver.split_yes()
        driver.choose_output("pdf", "original")
        files = driver.save()
        assert verify_outputs(PHOTO, files, rows=2, cols=3, fmt="pdf", paper="original") == []
        assert driver.no_horizontal_overflow()
        assert driver.console_problems() == []
    finally:
        context.close()


# Dragging a finger over the turned page is not tested here: the headless browsers cannot be made to
# produce touch scrolling (synthetic gestures do nothing, touch emulation crashes the page).  Both
# engines map a drag into the turned scroller's own direction; that part needs a look on a real phone.


def test_upright_phone_and_tablet_are_left_alone(browsers, playwright_instance, base_url, request):
    driver, context, _size = open_phone(browsers, playwright_instance, base_url, "android", 0, request)
    try:
        g = driver.page.evaluate(GEOMETRY)
        assert g["turn"] == "" and not g["pageScroll"], g
    finally:
        context.close()
    # a tablet on its side keeps its landscape layout
    options = dict(playwright_instance.devices["iPad (gen 7) landscape"])
    options.update(locale="ko-KR")
    context = browsers("webkit").new_context(**options)
    context.add_init_script(ANGLE_STUB % 90)
    try:
        page = context.new_page()
        driver = AppDriver(page, base_url, "iphone")
        driver.open()
        g = page.evaluate(GEOMETRY)
        assert g["turn"] == "" and g["wide"], g
    finally:
        context.close()
