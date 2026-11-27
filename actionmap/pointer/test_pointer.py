"""Tests for the Nodach Desktop Pointer that run on any OS (no Windows packages needed).

Run: python actionmap/pointer/test_pointer.py
Covers pointer_core (web-view detection, crop boxes, pasted text, ActionMap event), that pointer.py
imports off Windows, and pointer_mcp.py against a temp events.jsonl when mcp is installed.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = tempfile.mkdtemp(prefix="nodach-pointer-test-")
os.environ["NODACH_POINTER_HOME"] = TMP  # before importing pointer / pointer_mcp

import pointer_core as core  # noqa: E402

SCREEN_W, SCREEN_H = 1920, 1080
SCREEN = {"left": 0, "top": 0, "right": SCREEN_W, "bottom": SCREEN_H}


def el(name="", cls="", rect=(0, 0, 100, 30), type_="ButtonControl"):
    l, t, r, b = rect
    return {"type": type_, "name": name, "automation_id": "", "class_name": cls,
            "rect": {"left": l, "top": t, "right": r, "bottom": b}}


def event(**over):
    e = {"id": "PE-261119-120000-abcd", "cursor": {"x": 500, "y": 400},
         "element": el("Send", "Button"), "window": {"title": "Claude", "class_name": "Chrome_WidgetWin_1"},
         "process": {"pid": 1, "name": "claude.exe"}, "ancestors": [{"type": "WindowControl", "name": "Claude"}],
         "screenshot": "C:\\x\\shots\\PE-1.png", "screenshot_error": None, "note": "make it blue",
         "batch_refs": [], "opaque": False}
    e.update(over)
    return e


def test_opaque_detection():
    # The F-pointer-webview case: one big unnamed pane.
    assert core.is_opaque_element(el("", "", (0, 0, 1920, 1040), "PaneControl"), SCREEN_W, SCREEN_H)
    # Unnamed but small: normal element (e.g. an icon-only button), keep text-only behaviour.
    assert not core.is_opaque_element(el("", "", (0, 0, 200, 200)), SCREEN_W, SCREEN_H)
    # Exactly 25% is not "more than 25%".
    assert not core.is_opaque_element(el("", "", (0, 0, 960, 540)), SCREEN_W, SCREEN_H)
    assert core.is_opaque_element(el("", "", (0, 0, 961, 540)), SCREEN_W, SCREEN_H)
    # Big but named (e.g. a real document pane with a name): not opaque.
    assert not core.is_opaque_element(el("Document", "", (0, 0, 1920, 1040)), SCREEN_W, SCREEN_H)
    # Whitespace-only names count as unnamed.
    assert core.is_opaque_element(el("   ", "", (0, 0, 1920, 1040)), SCREEN_W, SCREEN_H)
    # Known web hosts are opaque at any size, named or not.
    for cls in ("CopilotBrowserClientView", "Chrome_WidgetWin_1", "Chrome_RenderWidgetHostHWND",
                "Microsoft.UI.Xaml.Controls.WebView2", "WebView2", "WebViewHost"):
        assert core.is_opaque_element(el("Copilot", cls, (10, 10, 50, 50)), SCREEN_W, SCREEN_H), cls
    # Ordinary named classes stay normal.
    for cls in ("Button", "Edit", "Shell_TrayWnd", "ToolbarWindow32"):
        assert not core.is_opaque_element(el("OK", cls), SCREEN_W, SCREEN_H), cls
    # Missing rect or a zero screen never crashes.
    assert not core.is_opaque_element({"name": "", "class_name": ""}, SCREEN_W, SCREEN_H)
    assert not core.is_opaque_element(el(""), 0, 0)


def test_crop_box_centres_on_cursor():
    rect = {"left": 0, "top": 0, "right": 1920, "bottom": 1040}
    assert core.crop_box({"x": 960, "y": 520}, rect, SCREEN) == (510, 220, 1410, 820)
    assert core.crop_box((960, 520), rect, (1920, 1080)) == (510, 220, 1410, 820)  # tuple forms


def test_crop_box_stays_inside_element_and_screen():
    rect = {"left": 0, "top": 0, "right": 1920, "bottom": 1040}  # web view above a 40px taskbar
    assert core.crop_box((5, 5), rect, SCREEN) == (0, 0, 900, 600)
    assert core.crop_box((1915, 1035), rect, SCREEN) == (1020, 440, 1920, 1040)  # off the taskbar
    # Element bigger than the screen: clamp to the screen.
    big = {"left": -50, "top": -50, "right": 3000, "bottom": 3000}
    assert core.crop_box((1910, 1070), big, SCREEN) == (1020, 480, 1920, 1080)
    # Element smaller than 900x600: crop is the element's overlap with the screen.
    small = {"left": 100, "top": 100, "right": 600, "bottom": 400}
    assert core.crop_box((300, 200), small, SCREEN) == (100, 100, 600, 400)
    # Rect that doesn't contain the cursor (stale UIA rect): fall back to the screen.
    assert core.crop_box((960, 540), {"left": 0, "top": 0, "right": 10, "bottom": 10}, SCREEN) == (510, 240, 1410, 840)
    # No rect at all.
    assert core.crop_box((960, 540), None, SCREEN) == (510, 240, 1410, 840)
    # Second monitor left of the primary (negative coordinates).
    mon2 = {"left": -1920, "top": 0, "right": 0, "bottom": 1080}
    assert core.crop_box((-10, 10), {"left": -1920, "top": 0, "right": 0, "bottom": 1040}, mon2) == (-900, 0, 0, 600)
    for (x, y) in [(0, 0), (1919, 1079), (960, 540), (37, 1001)]:
        l, t, r, b = core.crop_box((x, y), rect, SCREEN)
        assert r - l <= 900 and b - t <= 600 and l <= x <= r and t <= y <= b
        assert l >= 0 and t >= 0 and r <= 1920 and b <= 1080


def test_element_box_matches_v010():
    assert core.element_box({"left": 100, "top": 100, "right": 200, "bottom": 130}, (150, 115)) == (76, 76, 224, 154)
    # Zero-size element still gets its padded box (v0.1.0 did the same: PAD makes it 48x48).
    assert core.element_box({"left": 5, "top": 5, "right": 5, "bottom": 5}, (500, 400)) == (-19, -19, 29, 29)
    # Broken (inverted) rect -> 600x400 around the cursor (v0.1.0's x-300..x+300, y-200..y+200).
    assert core.element_box({"left": 300, "top": 5, "right": 100, "bottom": 9}, (500, 400)) == (200, 200, 800, 600)
    # Huge element -> same fallback.
    assert core.element_box({"left": 0, "top": 0, "right": 3000, "bottom": 2000}, (500, 400)) == (200, 200, 800, 600)


def test_summary_and_block():
    e = event()
    s = core.summary(e)
    lines = s.split("\n")
    assert lines[0] == '[Nodach pointer PE-261119-120000-abcd] app=claude.exe window="Claude"'
    assert lines[1] == 'element: ButtonControl "Send" (AutomationId=-, Class=Button)'
    assert lines[2] == 'path: WindowControl "Claude"'
    assert "view:" not in s  # named element: unchanged v0.1.3 text
    assert lines[-1] == "screenshot: C:\\x\\shots\\PE-1.png"
    b = core.build_block(e)
    assert b.endswith("\nnote: make it blue") and "batch:" not in b
    b2 = core.build_block(event(batch_refs=["PE-a", "PE-b"], note=None))
    assert "\nbatch: PE-a, PE-b\n" in b2 and b2.endswith("\nnote: -")
    # Opaque web view: the text says where the cursor was and points to the image.
    o = core.summary(event(opaque=True, element=el("", "", (0, 0, 1920, 1040), "PaneControl")))
    assert "view: web content" in o and "x=500, y=400" in o
    assert o.split("\n")[1] == 'element: PaneControl "" (AutomationId=-, Class=-)'
    # Screenshot failure shows the error instead.
    assert core.summary(event(screenshot=None, screenshot_error="boom")).endswith("screenshot: boom")


def test_actionmap_event():
    ev = core.actionmap_event(event(opaque=True), "PC1", "nowed")
    assert ev["actor"] == {"kind": "human", "id": "human:nowed"}
    assert ev["kind"] == "annotation" and ev["action"] == "point" and ev["node_id"] == "PC1"
    assert ev["payload"]["pointer_id"] == "PE-261119-120000-abcd" and ev["payload"]["opaque"] is True
    assert ev["target"]["control_type"] == "ButtonControl"
    schema_path = HERE.parent / "schema" / "event.schema.json"
    try:
        import jsonschema
    except ImportError:
        print("  (jsonschema not installed: skipped ActionMap schema check)")
        return
    if not schema_path.exists():  # e.g. running from the unzipped nodach-pointer folder
        print("  (ActionMap schema not found next to this folder: skipped schema check)")
        return
    jsonschema.validate(ev, json.loads(schema_path.read_text(encoding="utf-8")))


def test_ids_and_version():
    t = core.utcnow()
    i = core.new_id(t)
    assert i.startswith("PE-") and len(i) == len("PE-261119-120000-abcd")
    assert core.VERSION == "0.1.8" and core.SCHEMA == "pointer.v1"


def test_dib_bytes():
    try:
        from PIL import Image
    except ImportError:
        print("  (pillow not installed: skipped CF_DIB check)")
        return
    d = core.dib_bytes(Image.new("RGBA", (3, 2), (255, 0, 0, 255)))
    # BITMAPINFOHEADER: size 40, width 3, height 2, 24 bits per pixel; no "BM" file header.
    assert d[:2] != b"BM"
    assert int.from_bytes(d[0:4], "little") == 40
    assert int.from_bytes(d[4:8], "little", signed=True) == 3
    assert int.from_bytes(d[8:12], "little", signed=True) == 2
    assert int.from_bytes(d[14:16], "little") == 24


def test_pointer_imports_off_windows():
    import pointer
    assert pointer.VERSION == core.VERSION
    assert "<ctrl>+<alt>+m" in pointer.HOTKEYS and pointer.HOTKEYS["<ctrl>+<alt>+m"] == "point"
    assert str(pointer.EVENTS).startswith(TMP)


class _FakeRoot:
    def __init__(self):
        self.clip = None

    def clipboard_clear(self):
        self.clip = ""

    def clipboard_append(self, s):
        self.clip += s


class _FakeClipboard:
    def __init__(self):
        self.log = []

    def OpenClipboard(self):
        self.log.append("open")

    def EmptyClipboard(self):
        self.log.append("empty")

    def SetClipboardData(self, fmt, data):
        self.log.append((fmt, data))

    def CloseClipboard(self):
        self.log.append("close")


def test_handle_clipboard_routing():
    """Any element with a picture -> image + text in one clipboard session (v0.1.8); no pywin32 -> text."""
    import types
    import pointer
    saved = {k: getattr(pointer, k) for k in ("capture", "ask_note", "to_actionmap", "win32clipboard", "win32con")}
    fake = _FakeClipboard()
    try:
        pointer.ask_note = lambda root, e: ("fix this", True)
        pointer.to_actionmap = lambda e: "ActionMap not running (saved locally only)"
        pointer.win32clipboard = fake
        pointer.win32con = types.SimpleNamespace(CF_DIB=8, CF_UNICODETEXT=13)

        class _Img:
            def convert(self, mode):
                return self

            def save(self, buf, fmt):
                buf.write(b"BM" + b"\0" * 12 + b"DIBDATA")

        def cap(opaque):
            def f():
                e = event(opaque=opaque, note=None)
                pointer.last_image[e["id"]] = _Img()
                return e
            return f

        root = _FakeRoot()
        pointer.capture = cap(True)
        pointer.handle(root, "point")
        assert root.clip is None, "opaque path must not overwrite the clipboard via tk"
        assert fake.log[0] == "open" and fake.log[1] == "empty" and fake.log[-1] == "close"
        assert fake.log[2] == (8, b"DIBDATA")
        assert fake.log[3][0] == 13 and fake.log[3][1].endswith("note: fix this") and "view: web content" in fake.log[3][1]

        fake.log.clear()
        pointer.capture = cap(False)
        pointer.handle(root, "point")
        # v0.1.8: named elements also get the captioned picture.
        assert root.clip is None and fake.log[2] == (8, b"DIBDATA")
        assert fake.log[3][1].endswith("note: fix this") and "view:" not in fake.log[3][1]

        # No pywin32: opaque still falls back to text.
        pointer.win32clipboard = None
        root2 = _FakeRoot()
        pointer.capture = cap(True)
        pointer.handle(root2, "point")
        assert root2.clip.endswith("note: fix this")
        assert pointer.last_image == {}
    finally:
        for k, v in saved.items():
            setattr(pointer, k, v)


def test_pointer_mcp():
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("  (mcp not installed: skipped pointer_mcp test; pip install \"mcp>=1.2,<3\")")
        return
    home = Path(TMP)
    (home / "shots").mkdir(parents=True, exist_ok=True)
    png = home / "shots" / "PE-1.png"
    try:
        from PIL import Image
        Image.new("RGB", (10, 10)).save(png)
    except ImportError:
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
    evs = [{"id": "PE-0", "mode": "batch-item", "batch_refs": []},
           {"id": "PE-1", "mode": "point", "batch_refs": ["PE-0"], "note": "make bigger", "screenshot": str(png)}]
    (home / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n{half-written", encoding="utf-8")
    import pointer_mcp as m
    got = json.loads(m.get_pointed_elements())
    assert [e["id"] for e in got] == ["PE-1"] and got[0]["status"] == "new"
    assert got[0]["batch_items"][0]["id"] == "PE-0"
    assert len(json.loads(m.get_pointed_elements(status="any"))) == 2
    assert m.mark_pointed(["PE-1"], "done") == "Marked 1 event(s) done"
    assert json.loads(m.get_pointed_elements()) == []
    assert [e["id"] for e in json.loads(m.get_pointed_elements(status="done"))] == ["PE-1"]
    img = m.get_pointer_screenshot("PE-1")
    assert type(img).__name__ == "Image"
    try:
        m.get_pointer_screenshot("PE-0")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        m.mark_pointed(["PE-1"], "bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # status.jsonl is append-only: the earlier state is still there.
    rows = (home / "status.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["status"] == "done"


def test_named_huge_container_is_opaque():
    from pointer_core import is_opaque_element
    pane = {"type": "GroupControl", "name": "Primary pane", "class_name": "dframe-pane", "rect": {"left": 779, "top": 45, "right": 1817, "bottom": 1022}}
    assert is_opaque_element(pane, 1920, 1080), "the Claude app's Primary pane should give a picture"
    item = {"type": "ListItemControl", "name": "1.1 Update", "class_name": "", "rect": {"left": 434, "top": 281, "right": 1074, "bottom": 480}}
    assert not is_opaque_element(item, 1920, 1080), "a named list item stays text-only"


def test_unnamed_container_any_size_is_opaque():
    from pointer_core import is_opaque_element
    side = {"type": "GroupControl", "name": "", "class_name": "static-composer-boot-fade df-nav-scroll-box", "rect": {"left": 264, "top": 50, "right": 768, "bottom": 967}}
    assert is_opaque_element(side, 1920, 1080), "an unnamed section of the Claude app should give a picture"
    btn = {"type": "ButtonControl", "name": "", "class_name": "", "rect": {"left": 10, "top": 10, "right": 40, "bottom": 40}}
    assert not is_opaque_element(btn, 1920, 1080), "a small unnamed button stays text-only"


def test_caption_is_drawn_under_the_picture():
    try:
        from PIL import Image
    except ImportError:
        print("skip caption (no pillow)"); return
    from pointer_core import captioned
    img = Image.new("RGB", (300, 200), "blue")
    out = captioned(img, "[Nodach pointer PE-1] app=claude.exe\nnote: make this bigger")
    assert out.width >= 300 and out.height > 200 + 20, out.size
    assert out.getpixel((10, 10)) == (0, 0, 255), "the picture itself is untouched"
    assert out.getpixel((2, 205)) != (0, 0, 255), "a caption band sits underneath"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} tests passed")
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
