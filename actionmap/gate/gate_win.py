"""
Nodach Gate: the Windows side. What window and element an action lands on, cropped screenshots, window hashes and
the rolling clip buffer. Imports anywhere: on a PC without pywin32/uiautomation/Pillow (or not Windows) every
function answers "unknown" instead, and NullContext stands in (tests use it with fixed values).

Nothing here sends anything anywhere: pictures go to %USERPROFILE%\\nodach-gate\\shots and clips only.
"""
from __future__ import annotations

import collections
import threading
import time
from pathlib import Path

import gate_core as core

try:
    import win32gui
    import win32process
    import psutil
except Exception:          # not Windows, or pywin32 missing
    win32gui = win32process = psutil = None
try:
    import uiautomation as uia
except Exception:
    uia = None
try:
    from PIL import Image, ImageDraw, ImageGrab
except Exception:
    Image = ImageDraw = ImageGrab = None

AVAILABLE = win32gui is not None

if AVAILABLE:   # physical pixels, like Windows-MCP's coordinates and Pillow's grab (else crops drift on scaled screens)
    try:
        import ctypes
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # per-monitor v2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _proc_name(hwnd) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _win(hwnd) -> dict:
    if not hwnd:
        return {"hwnd": 0, "title": "", "app": "", "rect": None}
    try:
        title = win32gui.GetWindowText(hwnd)
        rect = tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        title, rect = "", None
    return {"hwnd": hwnd, "title": title, "app": _proc_name(hwnd), "rect": rect}


def foreground() -> dict:
    if not AVAILABLE:
        return _win(0)
    return _win(win32gui.GetForegroundWindow())


def window_at(x: int, y: int) -> dict:
    if not AVAILABLE:
        return _win(0)
    try:
        h = win32gui.WindowFromPoint((int(x), int(y)))
        root = win32gui.GetAncestor(h, 2) if h else 0     # GA_ROOT
        return _win(root or h)
    except Exception:
        return _win(0)


def _el(c) -> dict:
    try:
        r = c.BoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
    except Exception:
        rect = None
    try:
        pw = bool(c.GetPropertyValue(30019))   # UIA_IsPasswordPropertyId
    except Exception:
        pw = False
    return {"name": (getattr(c, "Name", "") or "")[:200], "type": (getattr(c, "ControlTypeName", "") or "").replace("Control", ""),
            "rect": rect, "is_password": pw}


def element_at(x: int, y: int) -> dict | None:
    if uia is None:
        return None
    try:
        with uia.UIAutomationInitializerInThread():
            c = uia.ControlFromPoint(int(x), int(y))
            return _el(c) if c else None
    except Exception:
        return None


def focused_element() -> dict | None:
    if uia is None:
        return None
    try:
        with uia.UIAutomationInitializerInThread():
            c = uia.GetFocusedControl()
            return _el(c) if c else None
    except Exception:
        return None


def settings_page_hint(hwnd) -> str:
    """Best effort: the Settings app's page header text (Windows 11 shows it as the first big Text in the page)."""
    if uia is None or not hwnd:
        return ""
    try:
        with uia.UIAutomationInitializerInThread():
            w = uia.ControlFromHandle(hwnd)
            names = []
            for c, depth in uia.WalkControl(w, maxDepth=8):
                if c.ControlTypeName in ("TextControl", "HyperlinkControl") and c.Name and c.AutomationId in (
                        "PageTitle", "PageHeader", "HeaderTitle", "BreadcrumbBarItemText", "TextBlock"):
                    names.append(c.Name)
                if len(names) >= 4:
                    break
            return " / ".join(names)
    except Exception:
        return ""


def find_window(name: str) -> dict:
    """The top-level window an App switch names (title contains name, case-insensitive)."""
    if not AVAILABLE or not name:
        return _win(0)
    found = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and name.lower() in win32gui.GetWindowText(h).lower():
                found.append(h)
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return _win(found[0]) if found else _win(0)


def screen_rect():
    try:
        import win32api
        return (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77),
                win32api.GetSystemMetrics(76) + win32api.GetSystemMetrics(78),
                win32api.GetSystemMetrics(77) + win32api.GetSystemMetrics(79))
    except Exception:
        return (0, 0, 7680, 4320)


def grab(rect):
    if ImageGrab is None or not rect:
        return None
    try:
        return ImageGrab.grab(bbox=tuple(int(v) for v in rect), all_screens=True)
    except Exception:
        return None


def img_hash(img) -> int | None:
    if img is None:
        return None
    try:
        return core.ahash(list(img.convert("L").resize((8, 8)).getdata()))
    except Exception:
        return None


def blank(reason: str, size=(480, 270)):
    if Image is None:
        return None
    im = Image.new("RGB", size, (40, 40, 40))
    try:
        ImageDraw.Draw(im).text((12, 12), "Nodach Gate: not captured\n" + reason[:120], fill=(230, 230, 230))
    except Exception:
        pass
    return im


class WinContext:
    """Context for decide(): the window the action lands in, the element there, and capture helpers."""

    def __init__(self):
        self.last_typed = None
        self.elements = []           # rows from the last Snapshot (fallback element names)

    def context(self, tool: str, args: dict) -> dict:
        args = args or {}
        loc = args.get("loc")
        if isinstance(loc, str):
            try:
                loc = [int(v) for v in loc.strip("[]() ").split(",")]
            except ValueError:
                loc = None
        fg = foreground()
        w = window_at(*loc) if loc and len(loc) == 2 else fg
        el = element_at(*loc) if loc and len(loc) == 2 else (focused_element() if tool in ("Shortcut", "Type") else None)
        if el is None and loc:
            r = core.element_near(self.elements, loc)
            if r:
                el = {"name": r["name"], "type": r["type"], "rect": None, "is_password": r["password"]}
        el = el or {}
        ctx = {"app": w["app"], "title": w["title"], "rect": w["rect"], "hwnd": w["hwnd"],
               "fg_app": fg["app"], "fg_title": fg["title"], "fg_rect": fg["rect"],
               "element_name": el.get("name", ""), "element_type": el.get("type", ""),
               "element_rect": el.get("rect"), "is_password": el.get("is_password", False),
               "loc": loc, "last_typed": self.last_typed, "page_hint": "", "now": time.time()}
        if core._in_procs(w["app"], ["SystemSettings", "ApplicationFrameHost"]) and "settings" in w["title"].lower():
            ctx["app"] = "SystemSettings.exe"
            ctx["page_hint"] = settings_page_hint(w["hwnd"])
        if tool == "App":
            t = find_window(str(args.get("name") or ""))
            ctx["target_app"], ctx["target_title"] = t["app"], t["title"]
        return ctx

    def window_hash(self, ctx) -> int | None:
        return img_hash(grab(ctx.get("rect") or ctx.get("fg_rect")))

    def shot(self, ctx, path: Path, margin: int = 40) -> bool:
        rect = core.shot_rect(ctx.get("element_rect"), ctx.get("loc"), screen_rect(), margin) or ctx.get("rect") or ctx.get("fg_rect")
        im = grab(rect)
        if im is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path, "PNG")
        return True

    def note_typed(self, ctx, now):
        self.last_typed = {"element_name": ctx.get("element_name", ""), "ts": now}


class NullContext(WinContext):
    """For tests and non-Windows runs: a fixed context, no pixels."""

    def __init__(self, **fixed):
        super().__init__()
        self.fixed = fixed
        self.hashes = collections.deque()

    def context(self, tool, args):
        c = {"app": "", "title": "", "element_name": "", "element_type": "", "is_password": False, "loc": (args or {}).get("loc"),
             "last_typed": self.last_typed, "page_hint": ""}
        c.update(self.fixed)
        return c

    def window_hash(self, ctx):
        return self.hashes.popleft() if self.hashes else None

    def shot(self, ctx, path, margin=40):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")   # placeholder bytes so the log has something to point at
        return True


class ClipBuffer(threading.Thread):
    """The last N seconds of the window in front, in memory only, as small frames. Never-zone in front: a blank
    frame with the reason. on_change(bits, t) is called when two frames differ (glitch detection)."""

    def __init__(self, is_never, settings: dict, on_change=None):
        super().__init__(daemon=True, name="gate-clips")
        self.is_never, self.on_change = is_never, on_change
        self.settings = settings
        self.frames = collections.deque()
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self._last_hash = None

    def run(self):
        while not self.stop_ev.is_set():
            fps = max(0.5, float(self.settings.get("clip_fps", 2)))
            t0 = time.time()
            if self.settings.get("clips", True) and AVAILABLE and Image is not None:
                self._tick(t0)
            else:
                with self.lock:
                    self.frames.clear()
            self.stop_ev.wait(max(0.05, 1 / fps - (time.time() - t0)))

    def _tick(self, now):
        fg = foreground()
        never = self.is_never(fg["app"], fg["title"])
        if never:
            im, reason = blank(never), never
        else:
            im, reason = grab(fg["rect"]), ""
            if im is not None:
                w = int(self.settings.get("clip_max_width", 480))
                if im.width > w:
                    im = im.resize((w, max(1, int(im.height * w / im.width))))
                h = img_hash(im)
                if self._last_hash is not None and h is not None and self.on_change:
                    bits = core.hamming(self._last_hash, h)
                    if bits:
                        self.on_change(bits, now)
                self._last_hash = h
        keep = float(self.settings.get("clip_seconds", 10))
        with self.lock:
            self.frames.append((now, im, reason))
            while self.frames and now - self.frames[0][0] > keep:
                self.frames.popleft()

    def save(self, folder: Path, stem: str):
        """Write the buffer as an animated GIF (or a frame folder if clip_format is 'frames'). (path, n, blank, t0, t1)."""
        with self.lock:
            frames = [f for f in self.frames if f[1] is not None]
        if not frames or Image is None:
            return None, 0, 0, time.time(), time.time()
        folder.mkdir(parents=True, exist_ok=True)
        n_blank = sum(1 for f in frames if f[2])
        size = frames[-1][1].size
        imgs = [f[1].convert("RGB").resize(size) for f in frames]
        if self.settings.get("clip_format", "gif") == "frames":
            d = folder / stem
            d.mkdir(exist_ok=True)
            for i, im in enumerate(imgs):
                im.save(d / f"{i:03d}.png")
            path = d
        else:
            path = folder / f"{stem}.gif"
            dur = int(1000 / max(0.5, float(self.settings.get("clip_fps", 2))))
            imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=dur, loop=0)
        return path, len(imgs), n_blank, frames[0][0], frames[-1][0]

    def stop(self):
        self.stop_ev.set()
