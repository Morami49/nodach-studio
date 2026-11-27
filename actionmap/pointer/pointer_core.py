"""
Nodach Desktop Pointer: pure logic (no Windows-only packages), so it can be tested anywhere.

pointer.py does the Windows work (hotkeys, UI Automation, screenshots, clipboard) and calls
these functions to decide what to crop, whether an element is an opaque web view, and what
text to paste. test_pointer.py imports this module on any OS.
"""
import datetime as dt
import uuid

VERSION = "0.1.8"
SCHEMA = "pointer.v1"
PAD = 24                      # pixels of context around a normal element in the screenshot
MAX_AREA = 4_000_000          # elements bigger than this (whole windows) get a cursor-centred crop
SMALL_CROP = (600, 400)       # cursor-centred crop for tiny or huge named elements (v0.1.0 behaviour)
WEB_CROP = (900, 600)         # cursor-centred crop for opaque web views (F-pointer-webview)
OPAQUE_SCREEN_SHARE = 0.25    # an unnamed element covering more than this share of the screen is opaque

# Window classes that host web content. UI Automation often stops at one big pane for these
# (the Claude app and Edge/Chrome are Chromium; the Copilot app uses CopilotBrowserClientView;
# WebView2 apps use the Chromium classes plus "Intermediate D3D Window").
WEB_HOST_CLASSES = {
    "CopilotBrowserClientView",
    "Chrome_WidgetWin_0",
    "Chrome_WidgetWin_1",
    "Chrome_RenderWidgetHostHWND",
    "Intermediate D3D Window",
}
WEB_HOST_MARKERS = ("webview",)   # any class containing this (case-insensitive), e.g. "WebView2", "WebViewHost"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id(t: dt.datetime) -> str:
    # Stable prefix "PE-" (pointer event); renaming views or categories never changes ids.
    return f"PE-{t:%y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def is_web_host_class(class_name) -> bool:
    c = (class_name or "").strip()
    return bool(c) and (c in WEB_HOST_CLASSES or any(m in c.lower() for m in WEB_HOST_MARKERS))


def _area(rect: dict) -> int:
    return max(0, rect["right"] - rect["left"]) * max(0, rect["bottom"] - rect["top"])


CONTAINER_TYPES = {"PaneControl", "GroupControl", "DocumentControl", "WindowControl", "CustomControl"}


def is_opaque_element(element: dict, screen_w: int, screen_h: int) -> bool:
    """True when UI Automation gave us nothing useful: an unnamed element covering more than
    25% of the screen, or any element whose class is a known web host."""
    if is_web_host_class(element.get("class_name")):
        return True
    rect = element.get("rect")
    big = bool(rect) and screen_w > 0 and screen_h > 0 and _area(rect) > OPAQUE_SCREEN_SHARE * screen_w * screen_h
    if not (element.get("name") or "").strip():
        # v0.1.6: an unnamed container (a page section with no label, PE-261119-225107-837d: 22% of the
        # screen) tells us nothing at any size, so the picture is the useful part.
        return big or (element.get("type") or "") in CONTAINER_TYPES
    # v0.1.5: a named but huge container (e.g. the Claude app's "Primary pane", PE-261119-223659-6505) says
    # where you are, not what you mean, so the picture is still the useful part.
    return big and (element.get("type") or "") in CONTAINER_TYPES


def _fit(lo: int, hi: int, centre: int, size: int) -> tuple:
    """A span of `size` centred on `centre`, shifted (not shrunk) to stay inside [lo, hi];
    shrunk only when [lo, hi] itself is smaller than `size`."""
    if hi - lo <= size:
        return lo, hi
    a = centre - size // 2
    a = max(lo, min(a, hi - size))
    return a, a + size


def _norm_screen(screen) -> dict:
    if isinstance(screen, dict):
        return screen
    if len(screen) == 2:
        return {"left": 0, "top": 0, "right": int(screen[0]), "bottom": int(screen[1])}
    l, t, r, b = screen
    return {"left": l, "top": t, "right": r, "bottom": b}


def crop_box(cursor, rect, screen, size=WEB_CROP) -> tuple:
    """(left, top, right, bottom) of a size[0] x size[1] box around the cursor, kept inside the
    element's rectangle where it overlaps the screen (so the crop stays on the web view and off
    the taskbar), or inside the screen when the rectangle is missing or doesn't hold the cursor.
    cursor: {"x","y"} or (x, y); rect: {"left","top","right","bottom"} or None;
    screen: same dict shape, (left, top, right, bottom) or (width, height)."""
    x, y = (cursor["x"], cursor["y"]) if isinstance(cursor, dict) else cursor
    s = _norm_screen(screen)
    bounds = s
    if rect:
        b = {"left": max(rect["left"], s["left"]), "top": max(rect["top"], s["top"]),
             "right": min(rect["right"], s["right"]), "bottom": min(rect["bottom"], s["bottom"])}
        if b["left"] <= x < b["right"] and b["top"] <= y < b["bottom"] and _area(b) > 0:
            bounds = b
    w, h = size
    l, r = _fit(bounds["left"], bounds["right"], x, w)
    t, bt = _fit(bounds["top"], bounds["bottom"], y, h)
    return l, t, r, bt


def element_box(rect: dict, cursor) -> tuple:
    """The v0.1.0 crop for a normal element: its rectangle plus PAD, or a 600x400 box around the
    cursor when the element is tiny or bigger than MAX_AREA."""
    x, y = (cursor["x"], cursor["y"]) if isinstance(cursor, dict) else cursor
    l, t, r, b = rect["left"] - PAD, rect["top"] - PAD, rect["right"] + PAD, rect["bottom"] + PAD
    if r - l < 8 or b - t < 8 or (r - l) * (b - t) > MAX_AREA:
        w, h = SMALL_CROP
        l, t, r, b = x - w // 2, y - h // 2, x + w // 2, y + h // 2
    return l, t, r, b


def summary(e: dict) -> str:
    el, w, p = e["element"], e["window"], e["process"]
    path = " > ".join(f'{a["type"]} "{a["name"]}"' if a["name"] else a["type"] for a in reversed(e["ancestors"]))
    lines = [
        f"[Nodach pointer {e['id']}] app={p['name']} window=\"{w['title']}\"",
        f"element: {el['type']} \"{el['name']}\" (AutomationId={el['automation_id'] or '-'}, Class={el['class_name'] or '-'})",
        f"path: {path or '-'}",
        f"rect: {el['rect']}",
    ]
    if e.get("opaque"):
        c = e.get("cursor") or {}
        lines.append(
            f"view: web content that Windows UI Automation can't see inside; the pointed spot is at "
            f"x={c.get('x')}, y={c.get('y')} — look at the screenshot of the area around the cursor"
        )
    lines.append(f"screenshot: {e['screenshot'] or e['screenshot_error']}")
    return "\n".join(lines)


def build_block(e: dict) -> str:
    """The text that goes on the clipboard (and into the chat when pasted)."""
    block = summary(e)
    if e.get("batch_refs"):
        block += f"\nbatch: {', '.join(e['batch_refs'])}"
    block += f"\nnote: {e.get('note') or '-'}"
    return block


def actionmap_event(e: dict, node_id: str, human_id: str) -> dict:
    """The same point as an ActionMap spine event (actionmap/event/v1, kind annotation, action point)."""
    el, w, p = e["element"], e["window"], e["process"]
    return {
        "schema_version": 1, "session_id": "pointer", "node_id": node_id,
        "actor": {"kind": "human", "id": "human:" + human_id}, "kind": "annotation", "action": "point",
        "target": {"app": p["name"], "window": w["title"], "element": el["name"], "control_type": el["type"],
                   "automation_id": el["automation_id"], "rect": el["rect"]},
        "payload": {"pointer_id": e["id"], "note": e["note"], "batch_refs": e["batch_refs"],
                    "screenshot_local": e["screenshot"], "ancestors": e["ancestors"], "pointer_schema": SCHEMA,
                    "opaque": bool(e.get("opaque")), "cursor": e.get("cursor")},
        "causes": [],
    }


def dib_bytes(image) -> bytes:
    """A PIL image as Windows CF_DIB clipboard data: a BMP file without its 14-byte file header."""
    import io
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "BMP")
    return buf.getvalue()[14:]


def captioned(img, text: str, width_chars: int = 110):
    """v0.1.7: the picture with the text block drawn underneath it. Claude's chat box (and most chat apps)
    take only the picture when both are on the clipboard (28a, 19 Nov), so the details travel inside it."""
    import textwrap
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for f in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(f, 13)   # a real font draws dashes and arrows (the built-in one shows boxes)
            break
        except Exception:
            continue
    font = font or ImageFont.load_default()
    lines = []
    for raw in (text or "").splitlines():
        lines += textwrap.wrap(raw, width_chars) or [""]
    line_h = 17
    pad = 8
    w = max(img.width, 8 + 7 * min(width_chars, max((len(l) for l in lines), default=0)))
    h = img.height + pad * 2 + line_h * len(lines)
    out = Image.new("RGB", (w, h), "white")
    out.paste(img.convert("RGB"), (0, 0))
    d = ImageDraw.Draw(out)
    d.rectangle([0, img.height, w, h], fill=(24, 32, 31))
    for i, l in enumerate(lines):
        d.text((pad, img.height + pad + i * line_h), l, fill=(240, 240, 240), font=font)
    return out
