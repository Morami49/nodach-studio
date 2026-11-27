"""
Nodach Gate: pure logic (no Windows, no network, no MCP), so every rule can be tested on any OS.

gate_mcp.py (the MCP server Claude desktop talks to) and gate_gui.py (the window) call these functions to:
  - filter Windows-MCP's tool list to the UI-only allowlist          filter_tools / hidden_tools
  - put every action in a trust zone (never / confirm / auto)        decide
  - remember "Allow for 10 min in this app" grants                   Grants
  - shape and mask the process-data log (gate.v1)                    log_entry / mask_text
  - decide when to keep a screenshot or a clip                       shot_reasons / clip_trigger / ClipLimiter
  - redact never-zone windows out of Snapshot text                   redact_snapshot
  - decide when an automatic report goes to Studio, and rate-limit   auto_report_trigger / ReportLimiter
  - build the filtered report (no typed text, no titles, no secrets) build_report / scrub

Every function takes time as an argument (no hidden clock), so the tests can replay any sequence.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

VERSION = "0.1.5"
SCHEMA = "gate.v1"
HERE = Path(__file__).resolve().parent
DEFAULT_ZONES = HERE / "zones.json"

# --------------------------------------------------------------------------- tools
# Allowlist, never a blocklist: a tool Windows-MCP adds later stays hidden until it is named here.
# (Windows-MCP 0.8.9 names; Snapshot/Screenshot are the UI-tree and image reads.)
UI_TOOLS = (
    "Snapshot", "Screenshot",            # read the screen (UI tree text first: fewer tokens)
    "Click", "Type", "Scroll", "Move",   # Move covers drag (drag=true / from_loc)
    "Shortcut",                          # keys and key combos
    "Wait", "WaitFor",
    "App",                               # launch / switch / resize (launch_executable refused, see decide)
    "ControlStatus",                     # read or release Windows-MCP's own "AI has control" lease
)
GATE_TOOLS = ("capture_view", "gate_status")  # Gate's own tools
INPUT_TOOLS = {"Click", "Type", "Scroll", "Move", "Shortcut"}
READ_TOOLS = {"Snapshot", "Screenshot"}
PASSIVE_TOOLS = {"Wait", "WaitFor", "ControlStatus"}
# Arguments Gate never forwards: they make Windows-MCP write files.
STRIPPED_ARGS = {"Snapshot": ("save_path",), "Screenshot": ("save_path",)}


def filter_tools(tools, allow=UI_TOOLS):
    """(exposed, hidden) from a list of tool dicts/objects with a .name or ["name"]."""
    exposed, hidden = [], []
    for t in tools:
        name = t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
        (exposed if name in allow else hidden).append(t)
    return exposed, hidden


def tool_name(t) -> str:
    return t.get("name") if isinstance(t, dict) else getattr(t, "name", "")


# --------------------------------------------------------------------------- zones
def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_zones(text: str, defaults: dict | None = None) -> dict:
    """zones.json text -> zones dict ("_comment" keys dropped). Missing keys come from the shipped defaults, so an
    older user file keeps working after an update. Adds "_sha" (first 12 hex of the file) for the log."""
    raw = json.loads(text)
    z = _strip_comments(raw)
    if defaults:
        z = deep_merge(defaults, z)
    z["_sha"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return z


def load_zones(path=None, defaults_path=DEFAULT_ZONES) -> dict:
    d = parse_zones(Path(defaults_path).read_text(encoding="utf-8-sig")) if Path(defaults_path).exists() else {}
    d.pop("_sha", None)
    if path and Path(path).exists():
        return parse_zones(Path(path).read_text(encoding="utf-8-sig"), d)
    d["_sha"] = "default"
    return d


def _rx(words) -> re.Pattern | None:
    words = [w for w in (words or []) if w]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![a-z0-9])", re.I)


def _hit(words, *texts):
    rx = _rx(words)
    if not rx:
        return None
    for t in texts:
        if t:
            m = rx.search(str(t))
            if m:
                return m.group(1)
    return None


def _proc(name) -> str:
    n = str(name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def _in_procs(app, procs) -> bool:
    a = _proc(app)
    return bool(a) and a in {_proc(p) for p in (procs or [])}


def norm_keys(shortcut: str) -> list[str]:
    aliases = {"del": "delete", "return": "enter", "control": "ctrl", "windows": "win", "escape": "esc"}
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", str(shortcut or "")) if p.strip()]
    return [aliases.get(p, p) for p in parts]


def _never(zones, app, title, element, *, input_action: bool):
    """(reason) if the window/element is in a never group, else None."""
    for g in zones.get("never", []):
        if g.get("applies_to", "all") == "input" and not input_action:
            continue
        label = g.get("name", "never")
        if _in_procs(app, g.get("processes")):
            return f"{label}: app {app}"
        w = _hit(g.get("title_words"), title)
        if w:
            return f"{label}: window title has '{w}'"
        w = _hit(g.get("element_words"), element)
        if w:
            return f"{label}: element named '{w}'"
        procs_scope = g.get("only_in_processes")
        if procs_scope and _in_procs(app, procs_scope):
            w = _hit(g.get("scoped_words"), title, element, *(g.get("_page_hints") or []))
            if w:
                return f"{label}: '{w}'"
    return None


def decide(action: dict, context: dict, zones: dict):
    """The trust zone for one request, before it is forwarded.

    action:  {"tool": "Click", "args": {...}}
    context: {"app": "notepad.exe", "title": "...", "element_name": "...", "element_type": "button",
              "is_password": bool, "page_hint": "...", "paused": bool,
              "last_typed": {"element_name": "...", "ts": float} | None, "now": float,
              "target_app": "...", "target_title": "..."}   # for App switch/launch: the window being asked for
    Returns (zone, reason), zone in "hidden" | "invalid" (bad arguments, refused) | "never" | "confirm" | "auto".
    """
    tool = action.get("tool", "")
    args = action.get("args") or {}
    if tool not in UI_TOOLS and tool not in GATE_TOOLS:
        return "hidden", f"{tool} is not a UI tool; Gate never exposes it"
    if tool in GATE_TOOLS:
        return "auto", "Gate's own tool"
    if context.get("paused"):
        return "never", "Gate is paused (Pause all is on in the Gate window)"
    if tool in PASSIVE_TOOLS:
        return "auto", "passive"
    if args.get("label") is not None and tool in INPUT_TOOLS:
        return "invalid", "Gate needs loc=[x, y] (the centre shown in Snapshot), not a label, so it can check the element there"
    for k in STRIPPED_ARGS.get(tool, ()):
        if args.get(k):
            return "invalid", f"{tool} {k} writes a file; Gate doesn't forward file writes (leave {k} out)"

    app, title = context.get("app") or "", context.get("title") or ""
    el = context.get("element_name") or ""
    hints = [context.get("page_hint") or ""]

    # Gate's own windows: Claude must never click or type into them (its Allow button lives there). Reading the
    # screen and opening other apps stay allowed while a Gate window is in front (21 Nov: with the Gate window
    # focused, every tool was refused, even Snapshot and launching Notepad).
    gate_titles = zones.get("gate_titles", ["Nodach Gate"])
    if tool == "App":
        target = str(args.get("name") or "")
        if _hit(gate_titles, target, context.get("target_title") or ""):
            return "never", "a Nodach Gate window (Claude can't answer Gate's own prompts)"
    elif tool not in READ_TOOLS and _hit(gate_titles, title):
        return "never", "a Nodach Gate window (Claude can't answer Gate's own prompts)"

    if tool == "App":
        mode = (args.get("mode") or "launch").lower()
        if mode == "launch_executable":
            return "never", "App launch_executable runs any program with arguments (a shell route); use launch by name"
        target = str(args.get("name") or "")
        t_app, t_title = context.get("target_app") or target, context.get("target_title") or target
        r = _never_with_hints(zones, t_app, t_title, target, hints, input_action=True)
        if r:
            return "never", r
        if mode == "launch":
            if _hit(zones.get("shell_apps", {}).get("names"), target) or _in_procs(target, zones.get("shell_apps", {}).get("processes")):
                return "never", f"launching a shell or system editor ('{target}') is a shell route"
            if not _app_on_list(target, zones.get("auto_apps", [])):
                return "confirm", f"launch '{target}': not on your auto list"
        return "auto", f"App {mode}"

    r = _never_with_hints(zones, app, title, el, hints, input_action=tool in INPUT_TOOLS)
    if r:
        return "never", r
    if tool in READ_TOOLS:
        return "auto", "read"

    cw = zones.get("confirm", {})
    keys = norm_keys(args.get("shortcut", "")) if tool == "Shortcut" else []

    if tool == "Shortcut":
        for combo in cw.get("key_combos", []):
            want = set(norm_keys(combo))
            if want and want.issubset(set(keys)) and (len(want) > 1 or len(keys) == 1 or want & {"delete"}):
                return "confirm", f"key combo {'+'.join(keys)}"
    if tool == "Click" and int(args.get("clicks", 1) or 0) == 0:
        return "auto", "hover"
    if tool in ("Click", "Shortcut", "Type") or (tool == "Move" and _truthy(args.get("drag")) or args.get("from_loc")):
        if tool != "Shortcut" or (keys and keys[-1] in ("enter", "space")):
            w = _hit(cw.get("element_words"), el)
            if w and not (tool == "Type" and not _truthy(args.get("press_enter"))):
                return "confirm", f"{tool.lower()} on an element named like '{w}'"
    # Enter after typing into a message/comment/reply box
    enter = (tool == "Type" and _truthy(args.get("press_enter"))) or (tool == "Shortcut" and keys and keys[-1] == "enter")
    if enter:
        lt = context.get("last_typed") or {}
        fresh = lt and (context.get("now") is None or lt.get("ts") is None or context["now"] - lt["ts"] <= 300)
        typed_into = el if tool == "Type" else ((lt.get("element_name") if fresh else "") or el)
        w = _hit(cw.get("enter_after_typing_words"), typed_into)
        if w:
            return "confirm", f"Enter after typing into '{w}' (sends it)"
    if tool == "Type" and context.get("is_password"):
        return "confirm", "typing into a password field"
    sa = zones.get("shell_apps", {})
    if tool in INPUT_TOOLS and _in_procs(app, sa.get("processes")):
        return "confirm", f"input into a shell window ({app})"
    for g in zones.get("confirm_apps", []):
        if tool in INPUT_TOOLS and tool != "Scroll" and _in_procs(app, g.get("processes")):
            return "confirm", f"{g.get('name', 'confirm app')}: {app}"
    return "auto", "no rule matched"


def _never_with_hints(zones, app, title, el, hints, input_action):
    z = {"never": [dict(g, _page_hints=hints) for g in zones.get("never", [])]}
    return _never(z, app, title, el, input_action=input_action)


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _app_on_list(name: str, apps) -> bool:
    n = _proc(name)
    return bool(n) and any(n == _proc(a) for a in apps)


# --------------------------------------------------------------------------- grants
class Grants:
    """'Allow for 10 min in this app': an app-wide pass for confirm-zone actions (never-zone is never granted)."""

    def __init__(self):
        self._until = {}

    def add(self, app: str, now: float, minutes: float = 10) -> float:
        self._until[_proc(app)] = now + minutes * 60
        return self._until[_proc(app)]

    def active(self, app: str, now: float) -> bool:
        return self._until.get(_proc(app), 0) > now

    def clear(self):
        self._until.clear()

    def snapshot(self, now: float) -> dict:
        return {a: round(t - now) for a, t in self._until.items() if t > now}


def popup_outcome(answer: str | None, app: str, grants: Grants, now: float, minutes: float = 10):
    """The popup's answer -> (decision, by). None/timeout -> blocked."""
    if answer == "once":
        return "allowed", "user"
    if answer == "app10":
        grants.add(app, now, minutes)
        return "allowed", "user"
    if answer == "block":
        return "blocked", "user"
    return "blocked", "timeout"


# --------------------------------------------------------------------------- log
PASSWORD_WORDS = ("password", "passcode", "passphrase", "pin", "secret", "otp", "one-time code", "2fa", "security code", "cvv", "cvc")


def looks_secret(element_name: str, element_type: str = "", is_password: bool = False) -> bool:
    return bool(is_password) or bool(_hit(PASSWORD_WORDS, element_name)) or "password" in str(element_type).lower()


def mask_text(text, secret: bool, limit: int = 80) -> str:
    t = str(text or "")
    if secret:
        return f"[masked {len(t)} chars]"
    return t if len(t) <= limit else t[:limit] + f"…(+{len(t) - limit})"


def clean_args(tool: str, args: dict, ctx: dict) -> dict:
    a = dict(args or {})
    secret = looks_secret(ctx.get("element_name", ""), ctx.get("element_type", ""), ctx.get("is_password", False))
    if "text" in a:
        a["text"] = mask_text(a["text"], secret)
    for k in ("texts", "items"):
        if isinstance(a.get(k), list):
            a[k] = [mask_text(x, secret) if isinstance(x, str) else x for x in a[k]]
    if "note" in a:
        a["note"] = mask_text(a["note"], False, 200)
    for k, v in a.items():            # anything else long (e.g. a refused PowerShell command) is cut too
        if isinstance(v, str) and k not in ("text", "note"):
            a[k] = mask_text(v, False, 120)
    return a


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_entry(*, ts: float, tool: str, args: dict, ctx: dict, zone: str, reason: str, decision: str, by: str,
              duration_ms: int, ok: bool, error: str = "", shot: str | None = None, zones_rev: str = "",
              session: str = "", kind: str = "action", extra: dict | None = None) -> dict:
    e = {
        "schema": SCHEMA, "kind": kind, "ts": iso(ts), "session": session, "tool": tool,
        "args": clean_args(tool, args, ctx),
        "app": ctx.get("app") or "", "title": mask_text(ctx.get("title") or "", False, 80),
        "element": {"name": mask_text(ctx.get("element_name") or "", False, 80), "type": ctx.get("element_type") or ""},
        "zone": zone, "reason": reason, "decision": decision, "by": by,
        "duration_ms": int(duration_ms), "ok": bool(ok), "zones_rev": zones_rev,
    }
    if error:
        e["error"] = str(error)[:300]
    if shot:
        e["shot"] = shot
    if extra:
        e.update(extra)
    return e


def clip_entry(*, ts: float, start: float, end: float, trigger: str, path: str | None, frames: int,
               actions: list, session: str = "", blank: int = 0, note: str = "") -> dict:
    inside = [{"ts": a.get("ts"), "tool": a.get("tool"), "zone": a.get("zone"), "decision": a.get("decision")}
              for a in actions if start <= a.get("_t", 0) <= end]
    e = {"schema": SCHEMA, "kind": "clip", "ts": iso(ts), "session": session, "trigger": trigger,
         "from": iso(start), "to": iso(end), "frames": frames, "blank_frames": blank, "path": path,
         "actions": inside}
    if note:
        e["note"] = note
    return e


def note_entry(ts: float, kind: str, session: str = "", **fields) -> dict:
    return {"schema": SCHEMA, "kind": kind, "ts": iso(ts), "session": session, **fields}


# --------------------------------------------------------------------------- captures
CUSTOM_TYPES = {"pane", "custom", "document", "image", "group", "canvas", "window", ""}


def capture_settings(zones: dict, state: dict | None = None) -> dict:
    c = dict(zones.get("capture", {}))
    s = state or {}
    for k in ("screenshots", "clips"):
        if k in s:
            c[k] = bool(s[k])
    return c


def can_capture(zone: str, foreground_never: bool) -> bool:
    """Never take pixels while a never-zone window is in front."""
    return not foreground_never and zone != "never"


def shot_reasons(phase: str, *, tool: str, zone: str, ctx: dict, settings: dict, ok: bool = True,
                 before_hash=None, after_hash=None, asked: bool = False) -> list[str]:
    """Why to keep a cropped screenshot at this phase ("before" or "after"); [] = don't.

    before: confirm-zone (shown in the popup); target has no name or is canvas/custom/web content.
    after:  the action errored; the window looks unchanged (hash before == after) for an input action.
    asked:  Claude called capture_view."""
    if not settings.get("screenshots", True):
        return []
    if asked:
        return ["asked"]
    out = []
    if phase == "before":
        if tool not in INPUT_TOOLS and tool != "App":
            return []
        if zone == "confirm":
            out.append("confirm")
        if tool in ("Click", "Type", "Move") and (not (ctx.get("element_name") or "").strip()
                                                  or (ctx.get("element_type") or "").lower() in CUSTOM_TYPES):
            out.append("no-name" if not (ctx.get("element_name") or "").strip() else "custom-drawn")
    elif phase == "after":
        if not ok:
            out.append("error")
        elif tool in INPUT_TOOLS and tool != "Move" and before_hash is not None and after_hash is not None \
                and hamming(before_hash, after_hash) <= settings.get("unchanged_bits", 0):
            out.append("unchanged")
    return out


def ahash(gray_pixels, size: int = 8) -> int:
    """Average hash of a size*size grayscale list (0-255)."""
    px = list(gray_pixels)[: size * size]
    if not px:
        return 0
    avg = sum(px) / len(px)
    h = 0
    for p in px:
        h = (h << 1) | (1 if p >= avg else 0)
    return h


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def shot_rect(element_rect, loc, screen, margin: int = 40, fallback: int = 160):
    """The crop box (l, t, r, b): element rect + margin, or a box around loc, clipped to the screen (l, t, r, b)."""
    sl, st, sr, sb = screen
    if element_rect and element_rect[2] > element_rect[0] and element_rect[3] > element_rect[1]:
        l, t, r, b = element_rect
        l, t, r, b = l - margin, t - margin, r + margin, b + margin
    elif loc:
        x, y = loc
        l, t, r, b = x - fallback, y - fallback, x + fallback, y + fallback
    else:
        return None
    l, t, r, b = max(sl, l), max(st, t), min(sr, r), min(sb, b)
    return (l, t, r, b) if r > l and b > t else None


def clip_trigger(*, kind: str, now: float, settings: dict, last_action_t: float | None = None,
                 frame_change_bits: int = 0, session_active: bool = True) -> str | None:
    """Should the rolling clip be kept? kind: "error" | "user" | "frame".
    "frame" is a change between two frames: it counts as a glitch only when the UI changed by more than
    glitch.min_bits while no action ran in the last glitch.quiet_s and Claude was active in the last
    glitch.active_s (so a video playing while nobody drives the PC is not a glitch)."""
    if not settings.get("clips", True):
        return None
    if kind == "user":
        return "user"
    if kind == "error":
        return "error"
    if kind == "frame":
        g = settings.get("glitch", {})
        if not g.get("on", True) or last_action_t is None or not session_active:
            return None
        quiet = now - last_action_t
        if frame_change_bits >= g.get("min_bits", 20) and g.get("quiet_s", 2.0) <= quiet <= g.get("active_s", 20.0):
            return "glitch"
    return None


class ClipLimiter:
    """Glitch clips at most once per cooldown and max per hour; error and user clips always pass."""

    def __init__(self, cooldown_s: float = 30, per_hour: int = 6):
        self.cooldown_s, self.per_hour, self.times = cooldown_s, per_hour, []

    def allow(self, trigger: str, now: float) -> bool:
        if trigger in ("user", "error"):
            self.times.append(now)
            return True
        recent = [t for t in self.times if now - t < 3600]
        if (self.times and now - self.times[-1] < self.cooldown_s) or len(recent) >= self.per_hour:
            return False
        self.times.append(now)
        return True


# --------------------------------------------------------------------------- snapshot text
_TREE_RE = re.compile(r'^(?P<prefix>[│ ]*)(?:[├└]──\s)?(?P<rest>.*)$')
_WIN_RE = re.compile(r'^window "(?P<title>.*)"\s*$')
_EL_RE = re.compile(r'^\((?P<x>-?\d+),(?P<y>-?\d+)\)\s+(?P<type>\S+)\s+"(?P<name>.*)"\s+\[action:[^\]]*\](?P<meta>.*)$')
HIDDEN = "[hidden by Nodach Gate]"


def _tree_line(line: str):
    m = _TREE_RE.match(line)
    return len(m.group("prefix")), m.group("rest").strip()


def redact_snapshot(text: str, zones: dict) -> tuple[str, int]:
    """Cut every never-zone window out of Snapshot text: its subtree in the UI Tree becomes one line, and its title
    is replaced wherever else it appears (Focused Window, Opened Windows). Returns (text, windows hidden)."""
    lines = str(text).splitlines()
    out, skip_depth, titles = [], None, set()
    for line in lines:
        depth, rest = _tree_line(line)
        if skip_depth is not None:
            if depth > skip_depth and rest:
                continue
            skip_depth = None
        m = _WIN_RE.match(rest)
        if m:
            r = _never(zones, "", m.group("title"), "", input_action=False)
            if r:
                titles.add(m.group("title"))
                out.append(line.replace(m.group("title"), HIDDEN) + f"  (contents hidden: {r.split(':')[0]})")
                skip_depth = depth
                continue
        out.append(line)
    t = "\n".join(out)
    for title in sorted(titles, key=len, reverse=True):
        if title:
            t = t.replace(title, HIDDEN)
    # before the UI tree: any other line naming a never-zone window (Focused/Opened Windows list)
    head, sep, tail = t.partition("UI Tree:")
    head = "\n".join(HIDDEN if _never(zones, "", ln, "", input_action=False) else ln for ln in head.splitlines())
    t = head + sep + tail
    # 21 Nov: an Explorer window open on nodach_sec listed a client_secret file name. Any element whose name or
    # value names a secret is hidden, in every window, not only windows with a secret title.
    words = [w for g in zones.get("never", []) if g.get("redact_elements") for w in (g.get("title_words") or [])]
    rx = _rx(words)
    if rx:
        def scrub(line):
            if not rx.search(line):
                return line
            return re.sub(r'"[^"]*"', '"' + HIDDEN + '"', line)
        t = "\n".join(scrub(ln) for ln in t.splitlines())
    return t, len(titles)


def parse_elements(text: str) -> list[dict]:
    """Interactive/scrollable rows from Snapshot text: {x, y, type, name, window, password}."""
    rows, win = [], ""
    for line in str(text).splitlines():
        _, rest = _tree_line(line)
        m = _WIN_RE.match(rest)
        if m:
            win = m.group("title")
            continue
        m = _EL_RE.match(rest)
        if m:
            rows.append({"x": int(m.group("x")), "y": int(m.group("y")), "type": m.group("type"), "name": m.group("name"),
                         "window": win, "password": "password" in m.group("meta")})
    return rows


def element_near(rows, loc, tol: int = 3):
    """The Snapshot row whose centre is at loc (fallback when UI Automation can't say what's there)."""
    if not loc:
        return None
    for r in rows or []:
        if abs(r["x"] - loc[0]) <= tol and abs(r["y"] - loc[1]) <= tol:
            return r
    return None


def strip_label(schema: dict) -> dict:
    """Gate checks the element at a point, so actions go by loc=[x, y]; the label/id argument is removed."""
    s = copy.deepcopy(schema or {})
    props = s.get("properties") or {}
    props.pop("label", None)
    if isinstance(s.get("required"), list):
        s["required"] = [r for r in s["required"] if r != "label"]
    return s


# --------------------------------------------------------------------------- reports
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_ANY_RE = re.compile(r"[A-Za-z0-9-]{1,39}\.\d{10,16}\.[0-9a-f]{64}")
_LONGHEX_RE = re.compile(r"\b[0-9a-f]{40,}\b")
_LONGNUM_RE = re.compile(r"\b(?:\d[ -]?){12,19}\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?:home|Users|tmp|mnt|root|var)/)[^\s'\"<>|]*")
TOKEN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}\.\d{10,16}\.[0-9a-f]{64}$")


def clean_token(raw):
    """A pasted nodach_session value, tidied; None if it isn't one (copied from actionmap/live)."""
    if not isinstance(raw, str):
        return None
    t = "".join(ch for ch in raw if ch.isprintable()).strip().strip('"').strip("'").strip()
    if t.lower().startswith("nodach_session="):
        t = t.split("=", 1)[1].strip()
    t = t.rstrip(";").strip()
    return t if TOKEN_RE.match(t) else None


def scrub(text) -> str:
    """Safe for a shared report: no sign-in values, email addresses, card-like numbers, long secrets, and paths
    cut down to the part inside nodach-gate (anything else becomes [path])."""
    t = str(text if text is not None else "")
    t = _TOKEN_ANY_RE.sub("[sign-in value]", t)
    t = t.replace("nodach_session=", "")
    t = _EMAIL_RE.sub("[email]", t)
    t = _LONGHEX_RE.sub("[hex]", t)
    t = _LONGNUM_RE.sub("[number]", t)

    def _path(m):
        parts = re.split(r"[\\/]+", m.group(0))
        return "/".join(parts[parts.index("nodach-gate"):]) if "nodach-gate" in parts else "[path]"
    return _PATH_RE.sub(_path, t)


REPORT_ROWS = 30


def build_report(info: dict, entries, n: int = REPORT_ROWS, title: str = "Nodach Gate report") -> str:
    """The filtered report: counts per zone/decision, then the last n actions with app, tool, zone, decision, by,
    element name and type, time and outcome. Never typed text, window titles, screenshots, clips or sign-in."""
    entries = [e for e in (entries or []) if e.get("kind", "action") in ("action", "clip", "view")]
    acts = [e for e in entries if e.get("kind", "action") == "action"]
    counts: dict = {}
    for e in acts:
        k = f"{e.get('zone')}/{e.get('decision')}"
        counts[k] = counts.get(k, 0) + 1
    errs = sum(1 for e in acts if not e.get("ok", True))
    head = [
        f"{title} · gate {info.get('version', VERSION)} · {info.get('at', '')}",
        f"os: {info.get('os', '?')} · python {info.get('python', '?')} · windows-mcp {info.get('windows_mcp', '?')}",
        f"zones: rev {info.get('zones_rev', '?')} · paused: {'yes' if info.get('paused') else 'no'}"
        f" · screenshots {'on' if info.get('screenshots', True) else 'off'} · clips {'on' if info.get('clips', True) else 'off'}",
        f"trigger: {info.get('trigger', 'manual')}",
        f"actions: {len(acts)} · errors: {errs} · clips: {sum(1 for e in entries if e.get('kind') == 'clip')}",
        "by zone/decision: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"),
        f"{'time':8}  {'app':18} {'tool':10} {'zone':8} {'decision':9} {'by':7} {'ms':>6}  element",
    ]
    body = []
    for e in entries[-n:]:
        t = (e.get("ts") or "")[11:19]
        if e.get("kind") == "clip":
            body.append(f"{t:8}  {'':18} {'[clip]':10} {e.get('trigger', ''):8} {str(e.get('frames', 0)) + ' fr':9}")
            continue
        el = e.get("element") or {}
        name = (el.get("name") or "")[:40]
        line = (f"{t:8}  {(e.get('app') or '')[:18]:18} {(e.get('tool') or '')[:10]:10} {e.get('zone', ''):8} "
                f"{e.get('decision', ''):9} {e.get('by', ''):7} {e.get('duration_ms', 0):>6}  "
                f"{el.get('type', '')} \"{name}\"" + ("" if e.get("ok", True) else f"  ERROR {(e.get('error') or '')[:80]}"))
        if e.get("zone") in ("never", "confirm") and e.get("reason"):
            line += f"  ({e['reason'][:80]})"
        body.append(line)
    return "\n".join(scrub(x) for x in head + (body or ["(nothing yet)"])) + "\n"


AUTO_REPORT_KINDS = ("never_block", "error_or_glitch", "repeat_fail", "confirm_timeout", "run_end",
                     "session_summary", "daily_summary")
TEXT_ONLY_KINDS = {"never_block", "run_end", "session_summary", "daily_summary"}


def auto_report_settings(zones: dict, state: dict | None = None) -> dict:
    """zones.json auto_reports, with the Gate window's checkboxes (state.json) on top."""
    a = dict(zones.get("auto_reports", {}))
    for k, v in ((state or {}).get("auto_reports") or {}).items():
        if k in AUTO_REPORT_KINDS or k == "send_automatically" or k == "attach_pictures":
            a[k] = bool(v)
        elif k == "hub" and v in ("staging", "production"):
            a[k] = v
    return a


def action_sig(entry: dict) -> str:
    """Same action = same tool and the same (cleaned) arguments."""
    return entry.get("tool", "") + " " + json.dumps(entry.get("args") or {}, sort_keys=True, default=str)


def auto_report_trigger(entry: dict, settings: dict, prev_entry: dict | None = None) -> str | None:
    """Which automatic report a new log entry asks for (or None). prev_entry: the action before it."""
    if not settings.get("send_automatically", True):
        return None
    if entry.get("kind") == "clip":
        return "error_or_glitch" if entry.get("trigger") == "glitch" and settings.get("error_or_glitch") else None
    if entry.get("kind", "action") != "action":
        return None
    if entry.get("zone") == "never" and entry.get("decision") in ("refused", "blocked") \
            and not str(entry.get("reason", "")).startswith("Gate is paused"):
        return "never_block" if settings.get("never_block") else None
    if entry.get("zone") == "confirm" and entry.get("decision") == "blocked" and entry.get("by") in ("user", "timeout"):
        return "confirm_timeout" if settings.get("confirm_timeout") else None
    if not entry.get("ok", True) and entry.get("decision") == "allowed":
        if prev_entry and not prev_entry.get("ok", True) and action_sig(prev_entry) == action_sig(entry) \
                and settings.get("repeat_fail"):
            return "repeat_fail"
        if settings.get("error_or_glitch"):
            return "error_or_glitch"
    return None


def run_end_due(*, now: float, last_action_t: float | None, last_run_end_t: float | None, settings: dict) -> bool:
    """End of a Claude-driven run: no action for run_end_idle_s (5 min) after Claude actions; once per run."""
    if not settings.get("send_automatically", True) or not settings.get("run_end") or last_action_t is None:
        return False
    if last_run_end_t is not None and last_run_end_t >= last_action_t:
        return False
    return now - last_action_t >= settings.get("run_end_idle_s", 300)


def session_summary_due(*, now: float, last_action_t: float | None, last_summary_t: float | None,
                        closing: bool, settings: dict) -> bool:
    """A session summary when Gate closes, or after session_idle_s (30 min) with no action; once per stretch."""
    if not settings.get("send_automatically", True) or not settings.get("session_summary") or last_action_t is None:
        return False
    if last_summary_t is not None and last_summary_t >= last_action_t:
        return False
    return closing or now - last_action_t >= settings.get("session_idle_s", 1800)


def daily_summary_due(*, now_local: dt.datetime, last_daily_date: str | None, settings: dict) -> bool:
    if not settings.get("send_automatically", True) or not settings.get("daily_summary"):
        return False
    hh, mm = (int(x) for x in str(settings.get("daily_at", "21:00")).split(":"))
    return last_daily_date != now_local.date().isoformat() and (now_local.hour, now_local.minute) >= (hh, mm)


class ReportLimiter:
    """Automatic reports: at most per_hour an hour (6), a gap of min_gap_s (5 min) between them except never-zone
    blocks, and per_day a day (20). state is a plain dict so it can be saved to disk between runs."""

    def __init__(self, state: dict | None = None, per_hour: int = 6, min_gap_s: float = 300, per_day: int = 20):
        self.s = state if state is not None else {}
        self.s.setdefault("times", [])
        self.s.setdefault("last_gapped", None)
        self.per_hour, self.min_gap_s, self.per_day = per_hour, min_gap_s, per_day

    @classmethod
    def from_settings(cls, state, settings):
        return cls(state, settings.get("per_hour", 6), settings.get("min_gap_s", 300), settings.get("per_day", 20))

    def allow(self, kind: str, now: float) -> tuple[bool, str]:
        times = [t for t in self.s["times"] if now - t < 86400]
        self.s["times"] = times
        if len(times) >= self.per_day:
            return False, f"daily limit ({self.per_day} a day)"
        if sum(1 for t in times if now - t < 3600) >= self.per_hour:
            return False, f"hourly limit ({self.per_hour} an hour)"
        last = self.s["last_gapped"]
        if kind != "never_block" and last is not None and now - last < self.min_gap_s:
            return False, f"gap ({int(self.min_gap_s // 60)} min between reports)"
        times.append(now)
        if kind != "never_block":
            self.s["last_gapped"] = now
        return True, ""


def wants_pictures(kind: str, settings: dict, manual_include: bool = False) -> bool:
    """Automatic never-zone and summary reports are always text only; others follow attach_pictures.
    A manual send carries pictures only with the explicit 'include pictures' tick."""
    if kind == "manual":
        return bool(manual_include)
    return kind not in TEXT_ONLY_KINDS and bool(settings.get("attach_pictures", True))


def day_item_id(local_date: dt.date) -> str:
    """One Studio item per day for Gate reports: GATE-yymmdd (never renamed, so links keep working)."""
    return f"GATE-{local_date:%y%m%d}"


def author(me: dict) -> dict:
    owner = bool(me.get("owner"))
    return {"id": me["id"], "role": "owner" if owner else "member", "label": "Nowed" if owner else "Member"}


def _ts(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def day_item_event(item_id: str, me: dict, t: dt.datetime, branch: str = "main") -> dict:
    d = item_id.split("-", 1)[-1]
    body = {"id": item_id, "type": "idea", "title": f"Nodach Gate reports 20{d[0:2]}-{d[2:4]}-{d[4:6]}",
            "body": "Reports from Nodach Gate (the safety layer in front of Windows-MCP) for this day, one comment each: "
                    "counts, zones, element names; never typed text, window titles, screenshots or sign-in values.",
            "refs": [], "status": "open", "source": "gate", "meta": {"kind": "gate.day", "schema": SCHEMA}}
    return {"ts": _ts(t), "kind": "item.create", "item": None, "branch": branch, "body": body, "author": author(me)}


def report_event(item: str, me: dict, text: str, t: dt.datetime, trigger: str = "manual",
                 attachments: list | None = None, branch: str = "main") -> dict:
    """The comment. attachments: references only ({name, bytes, sha256, private_path, chunks} or {name, local: true});
    the pictures themselves never go into the shared record."""
    body = {"text": scrub(text)[:8000], "report": True, "source": "gate", "trigger": trigger}
    if attachments:
        body["attachments"] = [{k: v for k, v in a.items() if k in ("name", "bytes", "sha256", "private_path", "chunks", "local")}
                               for a in attachments]
    return {"ts": _ts(t), "kind": "comment", "item": item, "branch": branch, "body": body, "author": author(me)}


def item_exists(rows, item_id: str) -> bool:
    for d in rows or []:
        e = (d or {}).get("event") or {}
        if e.get("kind") == "item.create" and (e.get("body") or {}).get("id") == item_id:
            return True
    return False


CHUNK_BYTES = 40 * 1024   # raw bytes per private event; base64 keeps it under the hub's 64 KB event limit


def file_chunks(data: bytes, chunk: int = CHUNK_BYTES):
    import base64
    return [base64.b64encode(data[i:i + chunk]).decode("ascii") for i in range(0, len(data), chunk)] or [""]


def private_path(login: str, sub: str = "gate/files") -> str:
    return f"data/users/{login}/{sub}"


def file_doc_id(sha256_hex: str, n: int) -> str:
    return f"gf-{sha256_hex[:40]}-{n:04d}"


def chunk_event(name: str, sha: str, n: int, total: int, b64: str, t: dt.datetime) -> dict:
    return {"ts": _ts(t), "kind": "gate.file", "body": {"name": name, "sha256": sha, "part": n, "parts": total, "data": b64}}


def outbox_name(now: float, kind: str) -> str:
    return f"{int(now * 1000)}-{re.sub(r'[^a-z_]', '', kind)}.json"


def upgrade_zones(user: dict, default: dict):
    """Bring an installed zones.json up to a newer bundled revision without losing his edits.

    21 Nov: 0.1.1 shipped a new never group, but the installed zones.json (revision 1) was kept as it was, so the
    rule never took effect. New never groups (by name) are added, his own groups and settings are kept, and the
    bundled change notes are appended. Returns (zones, added_names) — added_names empty when nothing changed."""
    ur, dr = int(user.get("revision") or 0), int(default.get("revision") or 0)
    if dr <= ur:
        return user, []
    out = dict(user)
    have = {g.get("name") for g in user.get("never", [])}
    added = [g for g in default.get("never", []) if g.get("name") not in have]
    out["never"] = list(user.get("never", [])) + added
    changes = list(user.get("_changes") or [])
    for c in default.get("_changes") or []:
        if isinstance(c, dict) and int(c.get("revision") or 0) > ur and c not in changes:
            changes.append(c)
    out["_changes"] = changes
    out["revision"] = dr
    return out, [g.get("name") for g in added]


def user_control_wait(text: str):
    """Seconds Windows-MCP asks us to wait when it paused for the user (USER_CONTROL), else None."""
    try:
        d = json.loads(str(text or "").strip())
    except (ValueError, TypeError):
        return None
    if isinstance(d, dict) and d.get("code") == "USER_CONTROL":
        return float((d.get("status") or {}).get("wait_seconds") or 10)
    return None
