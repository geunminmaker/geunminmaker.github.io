"""Watch a document in the real Office program the way a person would see it, and measure flicker.

    .venv/Scripts/python tools/watch_office.py word  <file.docx> [--mode print|read|protected] [--shots DIR]
    .venv/Scripts/python tools/watch_office.py ppt   <file.pptx> [--shots DIR]
    .venv/Scripts/python tools/watch_office.py hwp   <file.hwpx> [--shots DIR]

The program is started with a visible window (moved off the screen so it does not get in the way),
the document is left alone, and the window is photographed several times a second.  A picture that
"blinks" shows up as frames that differ from each other while nobody touches anything.

Windows + the Office program only.  Nothing is changed in the program's settings and a program
that was already running is left running.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

import win32con
import win32gui
import win32process
import win32ui
from PIL import Image, ImageChops, ImageStat

PW_RENDERFULLCONTENT = 2


def grab(hwnd: int) -> Image.Image:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = max(1, right - left), max(1, bottom - top)
    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    memory.SelectObject(bitmap)
    ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), PW_RENDERFULLCONTENT)
    info = bitmap.GetInfo()
    image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bitmap.GetHandle())
    memory.DeleteDC()
    source.DeleteDC()
    win32gui.ReleaseDC(hwnd, window_dc)
    return image


def park(hwnd: int, width: int = 1300, height: int = 950) -> None:
    """Show the window without activating it, off to the side of the screen."""
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    if ON_SCREEN:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 40, 40, width, height, win32con.SWP_SHOWWINDOW)
        return
    win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, -3000, 50, width, height,
                          win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)


ON_SCREEN = False  # --screen: photograph the real screen (shows what the graphics card puts there) instead of the window


def grab_screen(hwnd: int) -> Image.Image:
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd), all_screens=True).convert("RGB")


def watch(hwnd: int, seconds: float, shots: str | None, label: str, settle: float = 6.0) -> dict:
    take = grab_screen if ON_SCREEN else grab
    time.sleep(settle)
    frames = []
    started = time.time()
    while time.time() - started < seconds:
        frames.append(take(hwnd).convert("L").resize((650, 475)))
        time.sleep(0.07)  # not a multiple of the cursor blink
    changes = []
    for before, after in zip(frames, frames[1:]):
        diff = ImageChops.difference(before, after)
        changed = sum(diff.point(lambda value: 255 if value > 24 else 0).histogram()[255:]) / (diff.width * diff.height)
        changes.append(changed)
    if shots:
        os.makedirs(shots, exist_ok=True)
        full = take(hwnd)
        full.save(os.path.join(shots, f"{label}.png"))
        if changes and max(changes) > 0.01:  # keep the two frames that differ most
            worst = max(range(len(changes)), key=changes.__getitem__)
            frames[worst].save(os.path.join(shots, f"{label}-a.png"))
            frames[worst + 1].save(os.path.join(shots, f"{label}-b.png"))
    blank = ImageStat.Stat(frames[-1]).stddev[0] < 3
    return {"frames": len(frames), "changed_frames": sum(1 for c in changes if c > 0.01),
            "series": "".join("#" if c > 0.01 else "+" if c > 0.0005 else "." for c in changes),
            "largest_change": round(max(changes), 4) if changes else 0.0, "blank": blank}


def windows_of(pid: int) -> list[int]:
    found = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid \
                and win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found


def watch_word(path: str, mode: str, shots: str | None, seconds: float) -> dict:
    import win32com.client

    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = True
    word.DisplayAlerts = 0
    try:
        if mode == "protected":
            window = word.ProtectedViewWindows.Open(path)
            hwnd = win32gui.FindWindow("OpusApp", None) if not hasattr(window, "Hwnd") else 0
            hwnd = hwnd or word.ActiveProtectedViewWindow and win32gui.GetForegroundWindow()
            caption = window.Caption
            handles = []
            win32gui.EnumWindows(lambda h, _: handles.append(h) if caption in win32gui.GetWindowText(h) else True, None)
            hwnd = handles[0] if handles else hwnd
            view = "protected view"
        else:
            doc = word.Documents.Open(path, ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False)
            if mode == "read":
                word.ActiveWindow.View.ReadingLayout = True
            if mode == "many":  # several pages on screen at once, as on a phone or zoomed out
                word.ActiveWindow.View.Type = 3
                word.ActiveWindow.View.Zoom.Percentage = 20
            hwnd = word.ActiveWindow.Hwnd
            view = {1: "normal", 3: "print layout", 6: "web", 7: "read mode"}.get(word.ActiveWindow.View.Type, str(word.ActiveWindow.View.Type))
        park(hwnd)
        previous = win32gui.GetForegroundWindow()
        if mode == "active":
            # With the keyboard focus in the document the text cursor blinks; a picture drawn over the
            # cursor is repainted with every blink.  That only happens in the window that has the focus.
            word.Activate()
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001 - Windows may refuse; the result then says so
                pass
        result = watch(hwnd, seconds, shots, f"word-{mode}")
        result["view"] = view
        if mode == "active":
            result["had_focus"] = win32gui.GetForegroundWindow() == hwnd
            try:
                if previous:
                    win32gui.SetForegroundWindow(previous)
            except Exception:  # noqa: BLE001
                pass
        if mode != "protected":
            result["pages"] = doc.ComputeStatistics(2)
            result["shapes"] = doc.Shapes.Count
            result["inline_shapes"] = doc.InlineShapes.Count
        return result
    finally:
        try:
            for window in list(word.ProtectedViewWindows):
                window.Close()
            for doc in list(word.Documents):
                doc.Close(SaveChanges=0)
        finally:
            word.Quit()


def watch_powerpoint(path: str, shots: str | None, seconds: float) -> dict:
    import win32com.client

    already = "POWERPNT.EXE" in subprocess.run(["tasklist"], capture_output=True, text=True).stdout
    ppt = win32com.client.Dispatch("PowerPoint.Application")
    pres = ppt.Presentations.Open(path, ReadOnly=True, Untitled=False, WithWindow=True)
    try:
        hwnd = win32gui.FindWindow("PPTFrameClass", None)
        park(hwnd)
        result = watch(hwnd, seconds, shots, "ppt")
        result["slides"] = pres.Slides.Count
        result["slide_size_pt"] = (round(pres.PageSetup.SlideWidth, 1), round(pres.PageSetup.SlideHeight, 1))
        return result
    finally:
        pres.Close()
        if not already:
            ppt.Quit()


def watch_hangul(path: str, shots: str | None, seconds: float) -> dict:
    exe = None
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        for root, _dirs, files in os.walk(os.path.join(base, "HNC")) if base else []:
            if "Hwp.exe" in files:
                exe = os.path.join(root, "Hwp.exe")
                break
        if exe:
            break
    if not exe:
        return {"error": "Hwp.exe not found"}
    process = subprocess.Popen([exe, path])
    try:
        hwnd = 0
        for _ in range(60):
            time.sleep(1)
            handles = windows_of(process.pid)
            if handles:
                hwnd = handles[0]
                break
        if not hwnd:
            return {"error": "no window"}
        time.sleep(4)
        titles = [win32gui.GetWindowText(h) for h in windows_of(process.pid)]
        park(hwnd)
        result = watch(hwnd, seconds, shots, "hwp")
        result["windows"] = titles
        return result
    finally:
        process.terminate()


def main() -> int:
    global ON_SCREEN
    ON_SCREEN = "--screen" in sys.argv
    kind, path = sys.argv[1], os.path.abspath(sys.argv[2])
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "print"
    shots = os.path.abspath(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 8.0
    if kind == "word":
        result = watch_word(path, mode, shots, seconds)
    elif kind == "ppt":
        result = watch_powerpoint(path, shots, seconds)
    else:
        result = watch_hangul(path, shots, seconds)
    print(kind, mode if kind == "word" else "", os.path.basename(path), result)
    return 1 if result.get("error") or result.get("changed_frames", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
