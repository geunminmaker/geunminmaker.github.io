# -*- coding: utf-8 -*-
"""Open a .hwpx in the locally installed Hangul 2018 the ordinary way (Hwp.exe <path>) and OBSERVE only.

 - enumerates the top-level windows of the process we started (titles / classes)
 - captures the document window with PrintWindow (no input is sent to the app)
 - attaches through the COM Running Object Table and reads read-only properties
 - never calls Open/Save/RegisterModule, never clicks or dismisses anything
 - closes only the process it started (WM_CLOSE, document is unmodified)
usage: hancom_check.py <out_dir> <file.hwpx> [more files...]
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

import pythoncom
import win32com.client
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image

HWP_EXE = "C:/Program Files (x86)/HNC/Office 2018/HOffice100/Bin/Hwp.exe"
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def processes():
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    snap = k.CreateToolhelp32Snapshot(2, 0)
    e = PROCESSENTRY32()
    e.dwSize = ctypes.sizeof(e)
    res = []
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        res.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    return res


def descendants(root_pid):
    procs = processes()
    pids = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid, _ in procs:
            if ppid in pids and pid not in pids:
                pids.add(pid)
                changed = True
    return {pid: name for pid, _, name in procs if pid in pids}


def top_windows(pids):
    res = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in pids:
                res.append({"hwnd": h, "pid": pid, "title": win32gui.GetWindowText(h),
                            "class": win32gui.GetClassName(h),
                            "visible": bool(win32gui.IsWindowVisible(h)),
                            "enabled": bool(win32gui.IsWindowEnabled(h)),
                            "rect": list(win32gui.GetWindowRect(h)),
                            "owner": win32gui.GetWindow(h, win32con.GW_OWNER)})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return res


def child_texts(hwnd, limit=60):
    out = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h)
            if t and len(out) < limit:
                out.append((win32gui.GetClassName(h), t))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def capture(hwnd, path):
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    ok = ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)     # PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    im = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    im.save(path)
    win32gui.DeleteObject(bmp.GetHandle())
    mem.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    return {"ok": int(ok), "size": [w, h], "path": path}


def rot_hwp_objects():
    ctx = pythoncom.CreateBindCtx(0)
    rot = pythoncom.GetRunningObjectTable()
    res = []
    for mk in rot.EnumRunning():
        try:
            name = mk.GetDisplayName(ctx, None)
        except Exception:
            continue
        if "hwpobject" in name.lower():
            try:
                obj = rot.GetObject(mk)
                res.append((name, win32com.client.Dispatch(obj.QueryInterface(pythoncom.IID_IDispatch))))
            except Exception as ex:
                res.append((name, ex))
    return res


GSO_KEYS = ("Width", "Height", "TreatAsChar", "HorzRelTo", "VertRelTo", "HorzAlign", "VertAlign",
            "HorzOffset", "VertOffset", "TextWrap", "FlowWithText", "AllowOverlap")
PAGE_KEYS = ("PaperWidth", "PaperHeight", "Landscape", "LeftMargin", "RightMargin", "TopMargin",
             "BottomMargin", "HeaderLen", "FooterLen", "GutterLen")


def read_props(hwp):
    """Read-only property access only (no Open/Save/Run/Execute)."""
    out = {}
    for prop in ("PageCount", "Version", "Path", "IsEmpty", "IsModified"):
        try:
            v = getattr(hwp, prop)
            out[prop] = v if isinstance(v, (int, float, str, bool, type(None))) else repr(v)
        except Exception as ex:
            out[prop] = "ERR %r" % (ex,)
    try:
        out["XHwpDocuments.Count"] = hwp.XHwpDocuments.Count
    except Exception as ex:
        out["XHwpDocuments.Count"] = "ERR %r" % (ex,)
    # walk the control list: secd = section definition, cold = columns, gso = drawing object / picture
    ctrls = []
    try:
        c = hwp.HeadCtrl
        n = 0
        while c is not None and n < 200:
            cid = str(c.CtrlID)
            item = {"id": cid}
            try:
                item["desc"] = c.UserDesc
            except Exception:
                pass
            try:
                anchor = c.GetAnchorPos(0)          # getter: paragraph that anchors the control
                item["para"] = anchor.Item("Para")
            except Exception:
                item["para"] = "ERR"
            try:
                ps = c.Properties
                if cid.strip() == "gso":
                    for key in GSO_KEYS:
                        try:
                            item[key] = ps.Item(key)
                        except Exception:
                            item[key] = "ERR"
                elif cid.strip() == "secd":
                    pd = ps.Item("PageDef")
                    for key in PAGE_KEYS:
                        try:
                            item[key] = pd.Item(key)
                        except Exception:
                            item[key] = "ERR"
            except Exception as ex:
                item["props"] = "ERR %r" % (ex,)
            ctrls.append(item)
            c = c.Next
            n += 1
    except Exception as ex:
        out["ctrl_walk_error"] = repr(ex)
    out["ctrls"] = ctrls
    return out


def is_dialog(w, hwp_pid):
    """A visible top-level window of Hwp.exe itself that is a dialog box or an owned popup."""
    if w["pid"] != hwp_pid:
        return False
    width, height = w["rect"][2] - w["rect"][0], w["rect"][3] - w["rect"][1]
    if w["class"] == "#32770":
        return True
    # The start-up splash is an owned, title-less 548x341 window; anything else that is owned counts.
    is_splash = (not w["title"]) and w["class"].startswith("HwndWrapper[") and width < 700 and height < 450
    return bool(w["owner"] and width > 1 and height > 1 and not is_splash)


def check(path, out_dir, wait=120):
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    result = {"file": name, "bytes": os.path.getsize(path)}
    before = [pid for pid, _, exe in processes() if exe.lower() == "hwp.exe"]
    if before:
        result["verdict"] = "SKIPPED: a Hwp.exe that we did not start is already running"
        return result
    t0 = time.time()
    proc = subprocess.Popen([HWP_EXE, os.path.abspath(path)])
    result["pid"] = proc.pid
    snapshots = []
    seen_named = None
    while time.time() - t0 < wait:
        time.sleep(1.0)
        pids = descendants(proc.pid)
        wins = [w for w in top_windows(pids) if w["visible"]]
        sig = [(w["class"], w["title"]) for w in wins]
        if not snapshots or snapshots[-1][1] != sig:
            snapshots.append((round(time.time() - t0, 1), sig))
        dialogs = [w for w in wins if is_dialog(w, proc.pid)]
        named = [w for w in wins if stem.lower() in w["title"].lower()]
        if named and not dialogs:
            seen_named = seen_named or time.time()
            if time.time() - seen_named > 5:       # give layout / image decoding a moment
                break
        if dialogs and time.time() - t0 > 20:
            break
        if proc.poll() is not None:
            break
    pids = descendants(proc.pid)
    wins = top_windows(pids)
    result["processes"] = sorted(set(pids.values()))
    result["window_timeline"] = snapshots
    result["visible_windows"] = [{k: w[k] for k in ("class", "title", "rect", "owner", "enabled")}
                                 for w in wins if w["visible"]]
    dialogs = [w for w in wins if w["visible"] and is_dialog(w, proc.pid)]
    result["helper_windows"] = [(pids.get(w["pid"]), w["class"][:40], w["title"], w["rect"]) for w in wins
                                if w["visible"] and w["pid"] != proc.pid]
    result["dialogs"] = [{"class": w["class"], "title": w["title"], "texts": child_texts(w["hwnd"])} for w in dialogs]
    named = [w for w in wins if w["visible"] and stem.lower() in w["title"].lower()]
    result["title_shows_file"] = bool(named)
    result["load_seconds"] = round(time.time() - t0, 1)
    if named:
        doc = named[0]
        result["doc_title"] = doc["title"]
        try:
            result["capture"] = capture(doc["hwnd"], os.path.join(out_dir, stem + ".png"))
        except Exception as ex:
            result["capture"] = "ERR %r" % (ex,)
    for i, w in enumerate(dialogs):
        try:
            capture(w["hwnd"], os.path.join(out_dir, "%s.dialog%d.png" % (stem, i)))
        except Exception:
            pass
    if named and not dialogs:
        try:
            objs = rot_hwp_objects()
            result["rot"] = [n for n, _ in objs]
            for n, o in objs:
                if not isinstance(o, Exception):
                    result["com"] = read_props(o)
                    break
            objs = None
        except Exception as ex:
            result["rot_error"] = repr(ex)
    # ---- close what we started (unmodified document -> no prompt expected)
    closed = "n/a"
    if proc.poll() is None:
        if not dialogs:
            for w in top_windows(descendants(proc.pid)):
                if w["visible"] and not w["owner"] and w["pid"] == proc.pid and w["title"]:
                    win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            try:
                proc.wait(90)
                closed = "WM_CLOSE"
            except subprocess.TimeoutExpired:
                result["windows_after_close_request"] = [
                    (w["class"], w["title"], child_texts(w["hwnd"])[:8])
                    for w in top_windows(descendants(proc.pid)) if w["visible"]]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except Exception:
                pass
            closed = "terminated"
    result["closed"] = closed
    ok = result["title_shows_file"] and not dialogs
    result["verdict"] = "OPENED" if ok else "NOT VERIFIED (see windows/dialogs)"
    return result


if __name__ == "__main__":
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    pythoncom.CoInitialize()
    results = []
    for f in sys.argv[2:]:
        r = check(f, out_dir)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        sys.stdout.flush()
        if r["verdict"].startswith("SKIPPED") or r.get("dialogs"):
            print("STOP: prompt/dialog or foreign instance - not continuing")
            break
        time.sleep(6)
    with open(os.path.join(out_dir, "hancom_results_%d.json" % int(time.time())), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
