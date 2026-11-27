"""
Nodach Desktop Pointer (P-desktop-pointer) — v0.1.8
====================================================
Point at any element in any Windows app and tell your agent what you want.

  Ctrl+Alt+M  capture (mark) the element under the mouse, then type a note (Enter saves,
              Shift+Enter = new line, Esc discards)
  Ctrl+Alt+B  add the element under the mouse to a batch (no note yet); the next
              Ctrl+Alt+M closes the batch and its note applies to every item
  Ctrl+Alt+Q  quit

Each capture writes:
  - one append-only line to  <home>/events.jsonl   (schema "pointer.v1", never overwritten)
  - a cropped screenshot to  <home>/shots/<id>.png
  - a prompt-ready block to the clipboard (paste into any AI chat)

Web views (v0.1.4, F-pointer-webview): in apps drawn as web pages (the Claude app, the Copilot
app, browsers) Windows only reports one big unnamed pane, so the text alone says nothing. For
those the screenshot is a ~900x600 crop around the cursor, and the clipboard gets that IMAGE
plus the same text block: pasting gives the image where the app accepts images, text elsewhere.

<home> defaults to %USERPROFILE%\\nodach-pointer (override with NODACH_POINTER_HOME).
pointer_mcp.py exposes the same events to Claude Desktop / Claude Code as MCP tools.
The pure logic lives in pointer_core.py (tested on any OS by test_pointer.py).

Privacy: the script never reads field values (so typed passwords are not captured),
but screenshots show whatever is on screen. Don't point at things you wouldn't share.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pointer_core as core  # noqa: E402
from pointer_core import SCHEMA, VERSION, build_block, new_id, summary, utcnow  # noqa: E402,F401

# Physical-pixel coordinates so UI Automation rectangles and screenshots line up on scaled displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Windows-only packages. Guarded so this module (and its tests) import on any OS; main() refuses
# to run without them.
try:
    import tkinter as tk
except Exception:
    tk = None
try:
    import psutil
    import uiautomation as auto
    from PIL import ImageGrab
    from pynput import keyboard
except Exception as _err:  # ImportError, or OSError/AttributeError from ctypes.windll off Windows
    psutil = auto = ImageGrab = keyboard = None
    MISSING = str(_err)
else:
    MISSING = ""
try:
    import win32api
    import win32clipboard
    import win32con
except Exception:
    win32api = win32clipboard = win32con = None

HOME = Path(os.environ.get("NODACH_POINTER_HOME", str(Path.home() / "nodach-pointer")))
SHOTS = HOME / "shots"
EVENTS = HOME / "events.jsonl"
HOTKEYS = {"<ctrl>+<alt>+m": "point", "<ctrl>+<alt>+b": "batch", "<ctrl>+<alt>+q": "quit"}

jobs: "queue.Queue[str]" = queue.Queue()
pending_batch: list = []      # ids captured with Ctrl+Alt+B, waiting for a note
last_image = {}               # event id -> PIL image of its screenshot (for the image clipboard)


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def rect_of(c) -> dict:
    r = c.BoundingRectangle
    return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom}


def ancestors(c, limit: int = 8) -> list:
    out, p, n = [], safe(c.GetParentControl), 0
    while p is not None and n < limit:
        out.append({"type": safe(lambda: p.ControlTypeName, ""), "name": (safe(lambda: p.Name, "") or "")[:80]})
        p, n = safe(p.GetParentControl), n + 1
    return out


def top_window(c):
    root = auto.GetRootControl()
    w = c
    while True:
        p = safe(w.GetParentControl)
        if p is None or safe(lambda: auto.ControlsAreSame(p, root), False):
            return w
        w = p


def monitor_rect(x: int, y: int) -> dict:
    """The rectangle of the monitor under the cursor (primary screen if pywin32 is missing)."""
    if win32api is not None:
        try:
            l, t, r, b = win32api.GetMonitorInfo(win32api.MonitorFromPoint((x, y), 2))["Monitor"]
            return {"left": l, "top": t, "right": r, "bottom": b}
        except Exception:
            pass
    u = ctypes.windll.user32
    return {"left": 0, "top": 0, "right": u.GetSystemMetrics(0), "bottom": u.GetSystemMetrics(1)}


def grab(eid: str, box: tuple):
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{eid}.png"
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(path)
    last_image[eid] = img
    return str(path)


def capture() -> dict | None:
    x, y = auto.GetCursorPos()
    c = safe(lambda: auto.ControlFromPoint(x, y))
    if c is None:
        return None
    t = utcnow()
    eid = new_id(t)
    win = top_window(c)
    pid = safe(lambda: c.ProcessId)
    rect = safe(lambda: rect_of(c), {"left": x, "top": y, "right": x, "bottom": y})
    element = {
        "type": safe(lambda: c.ControlTypeName, ""),
        "name": (safe(lambda: c.Name, "") or "")[:200],
        "automation_id": safe(lambda: c.AutomationId, ""),
        "class_name": safe(lambda: c.ClassName, ""),
        "rect": rect,
    }
    mon = safe(lambda: monitor_rect(x, y), {"left": 0, "top": 0, "right": 1920, "bottom": 1080})
    opaque = core.is_opaque_element(element, mon["right"] - mon["left"], mon["bottom"] - mon["top"])
    box = core.crop_box((x, y), rect, mon) if opaque else core.element_box(rect, (x, y))
    shot, shot_error = None, None
    try:
        shot = grab(eid, box)
    except Exception as e:  # screenshot failure must not lose the element data
        shot_error = str(e)
    return {
        "schema": SCHEMA,
        "tool": "nodach-desktop-pointer",
        "tool_version": VERSION,
        "id": eid,
        "kind": "pointer.desktop",
        "at": t.isoformat(),
        "cursor": {"x": x, "y": y},
        "element": element,
        "window": {
            "title": (safe(lambda: win.Name, "") or "")[:200],
            "class_name": safe(lambda: win.ClassName, ""),
            "handle": safe(lambda: win.NativeWindowHandle),
        },
        "process": {"pid": pid, "name": safe(lambda: psutil.Process(pid).name())},
        "ancestors": ancestors(c),
        "opaque": opaque,
        "shot_box": list(box),
        "screenshot": shot,
        "screenshot_error": shot_error,
        "note": None,
        "batch_refs": [],
        "mode": None,
    }


def append(event: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


ACTIONMAP_URL = os.environ.get("ACTIONMAP_URL", "http://127.0.0.1:8765").rstrip("/")
ACTIONMAP_TOKEN = os.environ.get("ACTIONMAP_TOKEN", "")


def to_actionmap(e: dict) -> str:
    """Also record the point in ActionMap's spine (actionmap/event/v1) when it's running, so every
    tool that reads ActionMap (its map UI, its MCP connector) sees what you pointed at.
    The local events.jsonl line stays the source of truth; this never rewrites it."""
    ev = core.actionmap_event(e, os.environ.get("COMPUTERNAME", "win-desktop"),
                              os.environ.get("ACTIONMAP_HUMAN_ID", "local"))
    req = urllib.request.Request(f"{ACTIONMAP_URL}/events", data=json.dumps(ev).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-ActionMap-Token": ACTIONMAP_TOKEN} if ACTIONMAP_TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=2):
            return "sent to ActionMap"
    except Exception:
        return "ActionMap not running (saved locally only)"


def copy_image_and_text(img, text: str) -> None:
    """One clipboard session holding both CF_DIB (the image) and CF_UNICODETEXT (the block), so a
    paste gives the image in apps that take images and the text everywhere else."""
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        try:
            pic = core.captioned(img, text)
        except Exception:   # a caption must never cost the picture
            pic = img
        win32clipboard.SetClipboardData(win32con.CF_DIB, core.dib_bytes(pic))
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def ask_note(root, e: dict):
    """Returns (note, keep). keep=False when the user pressed Esc."""
    win = tk.Toplevel(root)
    win.title("Nodach pointer")
    win.attributes("-topmost", True)
    win.geometry(f"+{e['cursor']['x'] + 20}+{e['cursor']['y'] + 20}")
    head = summary(e).split("\n")[1]
    if e.get("opaque"):
        head += "\n(web view: the picture shows the area around the cursor)"
    if pending_batch:
        head += f"\n(+ {len(pending_batch)} batched element(s))"
    tk.Label(win, text=head, justify="left", wraplength=460).pack(padx=10, pady=(10, 4), anchor="w")
    box = tk.Text(win, width=62, height=4)
    box.pack(padx=10, pady=(0, 4))
    tk.Label(win, text="Enter = save · Shift+Enter = new line · Esc = discard", fg="#666").pack(padx=10, pady=(0, 8), anchor="w")
    result = {"note": None, "keep": True}

    def save(_=None):
        result["note"] = box.get("1.0", "end").strip() or None
        win.destroy()
        return "break"

    def discard(_=None):
        result["keep"] = False
        win.destroy()

    box.bind("<Return>", save)
    box.bind("<Shift-Return>", lambda _: None)  # default behaviour: newline
    win.bind("<Escape>", discard)
    win.protocol("WM_DELETE_WINDOW", discard)
    win.after(50, box.focus_force)
    root.wait_window(win)
    return result["note"], result["keep"]


def handle(root, job: str) -> None:
    if job == "quit":
        root.quit()
        return
    e = capture()
    if e is None:
        print("No element under the cursor.")
        return
    if job == "batch":
        e["mode"] = "batch-item"
        append(e)
        last_image.pop(e["id"], None)
        pending_batch.append(e["id"])
        print(f"Batched {e['id']} ({len(pending_batch)} waiting)")
        return
    note, keep = ask_note(root, e)
    img = last_image.pop(e["id"], None)
    if not keep:
        print(f"Discarded {e['id']}")
        return
    e.update(note=note, batch_refs=list(pending_batch), mode="point")
    pending_batch.clear()
    append(e)
    block = build_block(e)
    how = "text copied to clipboard"
    if img is not None and win32clipboard is not None:
        try:
            copy_image_and_text(img, block)
            how = "IMAGE + text copied to clipboard"
        except Exception as err:
            root.clipboard_clear()
            root.clipboard_append(block)
            how = f"text copied to clipboard (image copy failed: {err})"
    else:
        root.clipboard_clear()
        root.clipboard_append(block)
        if img is not None and win32clipboard is None:
            how += " (install pywin32 to also copy the image: pip install -r requirements.txt)"
    print(f"Saved {e['id']} — {how} — {to_actionmap(e)}")
    if how.startswith("IMAGE"):
        print("  The captioned picture is on the clipboard. Paste (Ctrl+V) into a chat to attach it;")
        print("  apps that only take text get the text block instead.")


def pump(root) -> None:
    try:
        while True:
            handle(root, jobs.get_nowait())
    except queue.Empty:
        pass
    root.after(50, pump, root)


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Nodach Desktop Pointer runs on Windows only.")
    if MISSING or tk is None:
        sys.exit(f"Missing packages ({MISSING or 'tkinter'}). Run: pip install -r requirements.txt")
    HOME.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    listener = keyboard.GlobalHotKeys({k: (lambda j=j: jobs.put(j)) for k, j in HOTKEYS.items()})
    listener.start()
    print(f"Nodach Desktop Pointer {VERSION} — writing to {EVENTS}")
    print("Ctrl+Alt+M mark + note · Ctrl+Alt+B add to batch · Ctrl+Alt+Q quit")
    if win32clipboard is None:
        print("Note: pywin32 isn't installed, so web-view screenshots can't go on the clipboard (text still works).")
    root.after(50, pump, root)
    root.mainloop()
    listener.stop()


if __name__ == "__main__":
    main()
