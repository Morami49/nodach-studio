"""
Nodach Live client: pure logic (no audio, keyboard or network), so it can be tested on any OS.

live_client.py does the Windows work (microphone, hotkeys, local transcription, HTTP) and calls these
functions to clean a transcript, spot "task:" / "build:" lines, shape the Studio events it posts, pick the
desktop pointer's latest point, and cut always-on audio into phrases.

Event shapes (the same as Studio's Live panel writes, studio/index.html "Live session"):
  live.line     {ts, kind:"live.line", item:<session>, branch, author, body:{session, speaker, text, ts, via, point?}}
  item.create   a session:  {id:"LV-...", type:"idea", status:"live", source:"live", live:{schema:"live.v1", ...}}
  item.create   a draft:    {id:"TK-...", type:"change", status:"draft", refs:[<session>], source:"live",
                             meta:{kind:"task"|"build", from_session, from_line:<doc id of the line>}}
Only text and a trimmed point leave the PC: never audio, screenshots, window titles or file paths.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
import secrets

VERSION = "0.2.2"
LINE_KIND = "live.line"
MAX_TEXT = 2000
POINT_MAX_AGE_S = 120          # a pointer point older than this isn't attached to a new line

# Same rule as Studio's LIVE_TASK: the first word is task or build, then a separator. Speech-to-text often writes
# the spoken colon as a comma or full stop, so those count too.
TASK_RE = re.compile(r"^\s*(task|build)\s*[:,.\-–—]\s*(\S.*)$", re.I | re.S)
# Whisper's non-speech captions, e.g. "[BLANK_AUDIO]", "(music)", "*coughs*"
NOISE_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    """Studio's timestamp form: UTC, milliseconds, Z."""
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stamp(prefix: str, t: dt.datetime) -> str:
    """Item ids like Studio's PTR.stamp: LV-yymmdd-hhmmss-xxxx. The prefix never changes when things are renamed."""
    t = t.astimezone(dt.timezone.utc)
    return f"{prefix}-{t:%y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def doc_id() -> str:
    """A shared-record document id the hub accepts (letters, digits, - and _; not hub-)."""
    return "lv" + secrets.token_hex(10)


def clean_text(text) -> str:
    t = NOISE_RE.sub(" ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:MAX_TEXT]


def parse_task(text):
    """{"kind": "task"|"build", "title": ...} for a task line, else None."""
    m = TASK_RE.match(str(text or ""))
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(".")
    return {"kind": m.group(1).lower(), "title": title} if title else None


def author(me: dict) -> dict:
    """The author block Studio writes: the hub's id for you (github:<login>, or email:<hash> for email accounts;
    never an address), owner or member, and the same label the page uses."""
    owner = bool(me.get("owner"))
    return {"id": me["id"], "role": "owner" if owner else "member", "label": "Nowed" if owner else "Member"}


def shareable_point(p):
    """The part of a desktop pointer event (pointer.v1) that may go into a shared line: which app and element, and
    the note typed with it. Window titles, screenshots, file paths, cursor and ancestors stay on the PC."""
    if not isinstance(p, dict) or p.get("schema") != "pointer.v1":
        return None
    el = p.get("element") or {}
    out = {"schema": "pointer.v1", "kind": p.get("kind") or "pointer.desktop", "id": p.get("id"),
           "app": ((p.get("process") or {}).get("name") or "")[:60],
           "element": {"type": (el.get("type") or "")[:40], "name": (el.get("name") or "")[:80]}}
    if p.get("note"):
        out["note"] = str(p["note"])[:500]
    return out


def latest_point(jsonl_lines, now: dt.datetime, used=(), max_age_s: int = POINT_MAX_AGE_S):
    """The newest pointer point (mode "point") from the pointer's events.jsonl lines that is fresh and not used yet."""
    best = None
    for raw in jsonl_lines:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue   # a half-written line
        if not isinstance(e, dict) or e.get("schema") != "pointer.v1" or e.get("mode") not in ("point", None) or e.get("id") in used:
            continue
        try:
            at = dt.datetime.fromisoformat(str(e.get("at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        if (now - at).total_seconds() > max_age_s or at > now + dt.timedelta(seconds=5):
            continue
        if best is None or at >= best[0]:
            best = (at, e)
    return best[1] if best else None


def line_event(session: str, me: dict, text: str, t: dt.datetime, via: str = "voice", point=None, branch: str = "main") -> dict:
    ts = iso(t)
    body = {"session": session, "speaker": author(me)["label"], "text": clean_text(text), "ts": ts, "via": via}
    if point:
        body["point"] = point
    return {"ts": ts, "kind": LINE_KIND, "item": session, "branch": branch, "body": body, "author": author(me)}


def task_event(session: str, line_doc_id: str, task: dict, me: dict, t: dt.datetime, branch: str = "main") -> dict:
    ts = iso(t)
    word = "Build: " if task["kind"] == "build" else "Task: "
    body = {"id": stamp("TK", t), "type": "change", "title": (word + task["title"])[:96],
            "body": f"{task['title']}\n\nDrafted from a live line ({session}, {ts}). Draft only: nothing is built until an owner approves it.",
            "status": "draft", "refs": [session], "source": "live",
            "meta": {"kind": task["kind"], "from_session": session, "from_line": line_doc_id}}
    return {"ts": ts, "kind": "item.create", "item": None, "branch": branch, "body": body, "author": author(me)}


def session_event(me: dict, t: dt.datetime, title: str = "", branch: str = "main") -> dict:
    ts = iso(t)
    sid = stamp("LV", t)
    body = {"id": sid, "type": "idea", "title": (title or "Live session " + ts[:16].replace("T", " "))[:96],
            "body": "Live session in progress. Lines arrive as live.line events; replies as comments on this item.",
            "refs": [], "status": "live", "source": "live",
            # 51a: instant replies (the hub's Live responder) start on for an owner's sessions; Studio's toggle changes it
            "live": {"schema": "live.v1", "started": ts, "audience": [], "via": "desktop", "responder": bool(me.get("owner"))}}
    return {"ts": ts, "kind": "item.create", "item": None, "branch": branch, "body": body, "author": author(me)}


def latest_live_session(docs, me_id: str):
    """The newest session you started that is still live, from /studio/api/events rows ([{doc_id, event}])."""
    started, status = {}, {}
    for d in docs:
        e = (d or {}).get("event") or {}
        b = e.get("body") or {}
        if e.get("kind") == "item.create" and b.get("source") == "live" and (e.get("author") or {}).get("id") == me_id and b.get("id") not in started:
            started[b["id"]] = e.get("ts") or ""
            status[b["id"]] = b.get("status", "open")
        elif e.get("kind") == "status" and e.get("item") in started:
            status[e["item"]] = b.get("status")
    live = [(ts, sid) for sid, ts in started.items() if status.get(sid) == "live"]
    return max(live)[1] if live else None


def segments(levels, threshold: float, frame_ms: int, silence_ms: int = 800, min_ms: int = 400):
    """Always-on mode: cut a stream of per-frame loudness (RMS) into phrases. Returns [(start, end)] frame indexes
    (end exclusive): a phrase starts at the first loud frame and ends after silence_ms of quiet; phrases shorter
    than min_ms of sound are dropped (clicks, coughs). A phrase still open at the end is returned too."""
    out, start, last_loud = [], None, None
    quiet_frames = max(1, silence_ms // frame_ms)
    for i, v in enumerate(levels):
        if v >= threshold:
            if start is None:
                start = i
            last_loud = i
        elif start is not None and i - last_loud >= quiet_frames:
            if (last_loud - start + 1) * frame_ms >= min_ms:
                out.append((start, last_loud + 1))
            start = last_loud = None
    if start is not None and (last_loud - start + 1) * frame_ms >= min_ms:
        out.append((start, last_loud + 1))
    return out


TOKEN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}\.\d{10,16}\.[0-9a-f]{64}$")


def clean_token(raw):
    """A pasted nodach_session value, tidied; None if it isn't one.

    Windows consoles turn Ctrl+V into a control character instead of pasting, and DevTools copies can carry
    quotes, spaces or the 'nodach_session=' prefix. Only a well-formed login.expires.signature is accepted,
    so a bad paste is caught here instead of as a 401 from the hub."""
    if not isinstance(raw, str):
        return None
    t = "".join(ch for ch in raw if ch.isprintable()).strip().strip('"').strip("'").strip()
    if t.lower().startswith("nodach_session="):
        t = t.split("=", 1)[1].strip()
    t = t.rstrip(";").strip()
    return t if TOKEN_RE.match(t) else None


MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def local_model_dir(roots, model: str):
    """A ready faster-whisper model folder (faster-whisper-<model>, all four files) under any root, else None.
    Lets the model come from a zip when the PC can't reach the model host."""
    for root in roots:
        d = Path(root) / f"faster-whisper-{model}"
        if all((d / f).is_file() for f in MODEL_FILES):
            return d
    return None


# ---------------------------------------------------------------- the window (live_gui.py): pure helpers
HUBS = {"staging": "https://staging.nodach.com", "production": "https://studio.nodach.com"}

# Keys other Nodach tools (and the Claude desktop app) already use. Ctrl+Alt+Space is Claude's quick-entry box.
RESERVED_HOTKEYS = {
    "<ctrl>+<alt>+m": "Nodach pointer: point",
    "<ctrl>+<alt>+b": "Nodach pointer: batch",
    "<ctrl>+<alt>+q": "Nodach pointer: quit",
    "<ctrl>+<alt>+p": "ActionMap capture",
    "<ctrl>+<alt>+l": "Live: send latest point (command line)",
    "<ctrl>+<alt>+a": "Live: always-on (command line)",
    "<ctrl>+<alt>+<space>": "Claude desktop app: quick entry",
}
GUI_HOTKEY = "<ctrl>+<alt>+j"

STEPS = ("record", "transcribe", "send", "sign-in", "session", "point", "mic", "model", "report", "setup")
OUTCOMES = ("ok", "failed", "empty speech", "info")
REPORT_ROWS = 30


def hotkey_clash(combo: str):
    """What already uses this key combination, or None if it's free."""
    return RESERVED_HOTKEYS.get(str(combo or "").lower().replace(" ", ""))


def activity_row(step: str, outcome: str, detail: str = "", t: dt.datetime | None = None) -> dict:
    """One row of the window's activity list. Unknown steps/outcomes are kept but marked, so nothing is lost."""
    t = t or utcnow()
    return {"ts": iso(t), "time": t.astimezone().strftime("%H:%M:%S"),
            "step": step if step in STEPS else f"?{step}", "outcome": outcome if outcome in OUTCOMES else f"?{outcome}",
            "detail": scrub(detail)[:160]}


def format_row(row: dict) -> str:
    """A fixed-width line for the plain-text report."""
    return f"{row.get('time', ''):8}  {row.get('step', ''):10}  {row.get('outcome', ''):12}  {row.get('detail', '')}".rstrip()


def record_detail(seconds: float, peak: float) -> str:
    return f"{seconds:.1f} s recorded, peak level {peak:.2f}"


def transcribe_detail(seconds: float, chars: int, took_s: float) -> str:
    return f"{seconds:.1f} s of audio -> {chars} chars in {took_s:.1f} s"


def send_detail(status, chars: int, point: bool = False, draft: str = "", error: str = "") -> str:
    parts = [f"hub {status}" if status else "hub unreachable", f"{chars} chars" if chars else "no text"]
    if point:
        parts.append("with pointer point")
    if draft:
        parts.append(f"draft {draft}")
    if error:
        parts.append(error)
    return " · ".join(parts)


def level_fraction(rms: float) -> float:
    """Microphone loudness (RMS, 0..1) as a 0..1 bar length on a decibel scale: -60 dB is empty, 0 dB full."""
    import math
    if not rms or rms <= 0:
        return 0.0
    return max(0.0, min(1.0, (20 * math.log10(rms) + 60) / 60))


def talk_label(recording: bool, seconds: float = 0.0, always: bool = False) -> str:
    if not recording:
        return "● Talk"
    m, s = divmod(int(seconds), 60)
    return f"■ Stop listening  {m}:{s:02d}" if always else f"■ Stop and send  {m}:{s:02d}"


_LOOPBACK = ("stereo mix", "what u hear", "wave out", "loopback", "mixage stéréo", "stereomix")
_VIRTUAL = ("droidcam", "virtual", "cable output", "voicemeeter", "vb-audio", "obs", "nvidia broadcast", "krisp", "iriun")
_MAPPER = ("sound mapper", "primary sound capture")


def mic_kind(name: str) -> str:
    """mic | virtual | loopback | mapper. Loopback devices record what the PC plays (not your voice); virtual ones
    come from apps (phone-as-webcam, mixers) and are often silent; mappers are Windows aliases for the default."""
    n = (name or "").lower()
    if any(k in n for k in _MAPPER):
        return "mapper"
    if any(k in n for k in _LOOPBACK):
        return "loopback"
    if any(k in n for k in _VIRTUAL):
        return "virtual"
    return "mic"


_KIND_NOTE = {"virtual": " — app device, may be silent", "loopback": " — records PC sound, not your voice"}


def input_devices(devices, default_index=None, hostapis=None):
    """sounddevice.query_devices() → [{index, label, default, kind}] for inputs, real microphones first.

    Windows lists every microphone once per sound system (MME, DirectSound, WASAPI, WDM-KS); only the ones on the
    default device's sound system are kept. Order: real microphones (the Windows default first), then app/virtual
    devices, then loopback ('Stereo Mix'); Windows' mapper aliases are dropped. 20 Nov: the app picked DroidCam
    (silent) as default and Stereo Mix got chosen, so speech arrived as silence."""
    devs = [dict(d, index=d.get("index", i)) for i, d in enumerate(devices or []) if (d or {}).get("max_input_channels", 0) > 0]
    if not devs:
        return []
    by_index = {d["index"]: d for d in devs}
    default = by_index.get(default_index)
    if default is not None and "hostapi" in default:
        same = [d for d in devs if d.get("hostapi") == default["hostapi"]]
        devs = same or devs
    names = {}
    if hostapis:
        names = {i: (h or {}).get("name", "") for i, h in enumerate(hostapis)}
    out, seen = [], set()
    for d in devs:
        name = re.sub(r"\s+", " ", str(d.get("name") or "microphone")).strip()[:60]
        kind = mic_kind(name)
        if kind == "mapper" or name in seen:
            continue
        seen.add(name)
        label = name + (f" ({names[d['hostapi']]})" if names.get(d.get("hostapi")) and len({x.get("hostapi") for x in devs}) > 1 else "")
        out.append({"index": d["index"], "label": label + _KIND_NOTE.get(kind, ""), "default": d["index"] == default_index, "kind": kind})
    rank = {"mic": 0, "virtual": 1, "loopback": 2}
    out.sort(key=lambda d: (rank[d["kind"]], not d["default"]))
    return out


def pick_loudest(results, floor: float = 0.02):
    """results: [(device, peak)] from 'Find my mic'. The loudest real microphone above the floor, else the
    loudest non-loopback device above it, else None."""
    ok = [(d, p) for d, p in results if p >= floor and d.get("kind") != "loopback"]
    if not ok:
        return None
    mics = [x for x in ok if x[0].get("kind") == "mic"]
    return max(mics or ok, key=lambda x: x[1])[0]


def session_list(docs, me_id: str, owner: bool = False):
    """Every live session in /studio/api/events rows you can use: [{id, state, title, started, label}], live ones
    first, newest first within each group. Owners see all; members see the ones they started or were given."""
    found, order = {}, []
    for d in docs:
        e = (d or {}).get("event") or {}
        b = e.get("body") or {}
        if e.get("kind") == "item.create" and b.get("source") == "live" and str(b.get("id", "")).startswith("LV-") and b["id"] not in found:
            aud = ((b.get("live") or {}).get("audience") or [])
            mine = (e.get("author") or {}).get("id") == me_id
            if owner or mine or me_id in aud or not aud:
                found[b["id"]] = {"id": b["id"], "state": b.get("status", "open"), "title": str(b.get("title") or "")[:80],
                                  "started": (b.get("live") or {}).get("started") or e.get("ts") or ""}
                order.append(b["id"])
        elif e.get("kind") == "status" and e.get("item") in found:
            found[e["item"]]["state"] = b.get("status") or found[e["item"]]["state"]
    rows = sorted(found.values(), key=lambda r: r["started"], reverse=True)
    rows.sort(key=lambda r: r["state"] != "live")   # stable: live first, newest first inside each group
    for r in rows:
        r["label"] = f"{r['id']} · {r['state']}"
    return rows


def send_target(rows, chosen: str):
    """Before each send (F-live-stale-session, 20 Nov: lines went to LV-261120-180852-4slr 15 minutes after it ended,
    because a new session had started and the app still held the old one). rows: session_list(). Returns
    {"action": "send"|"switch"|"ask", "to": <session or None>, "state": <chosen's state>, "live": [live ids]}:
      send    the chosen session is still live, or isn't in the list (can't tell: keep going as before)
      switch  it isn't live any more and exactly one session is live: use that one
      ask     it isn't live and none or several are: hold the line and let him pick."""
    live = [r["id"] for r in rows or [] if r.get("state") == "live"]
    me = next((r for r in rows or [] if r.get("id") == chosen), None)
    if me is None or me.get("state") == "live":
        return {"action": "send", "to": chosen, "state": me.get("state") if me else None, "live": live}
    if len(live) == 1:
        return {"action": "switch", "to": live[0], "state": me.get("state"), "live": live}
    return {"action": "ask", "to": None, "state": me.get("state"), "live": live}


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_ANY_RE = re.compile(r"[A-Za-z0-9-]{1,39}\.\d{10,16}\.[0-9a-f]{64}")
_LONGHEX_RE = re.compile(r"\b[0-9a-f]{40,}\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?:home|Users|tmp|mnt|root|var)/)[^\s'\"<>|]*")


def scrub(text) -> str:
    """Make a line safe for a shared report: no sign-in values, email addresses or long secrets, and file paths cut
    down to the part inside the nodach-live folder (anything else becomes [path])."""
    t = str(text if text is not None else "")
    t = _TOKEN_ANY_RE.sub("[sign-in value]", t)
    t = t.replace("nodach_session=", "")
    t = _EMAIL_RE.sub("[email]", t)
    t = _LONGHEX_RE.sub("[hex]", t)

    def _path(m):
        p = m.group(0)
        parts = re.split(r"[\\/]+", p)
        if "nodach-live" in parts:
            return "/".join(parts[parts.index("nodach-live"):])
        return "[path]"
    return _PATH_RE.sub(_path, t)


def build_report(info: dict, rows, n: int = REPORT_ROWS) -> str:
    """The plain-text report the window copies or sends. info: version, os, python, hub, role, session, mic, model,
    model_status, settings (dict). Never the sign-in value, audio, transcripts, window titles or outside paths."""
    settings = info.get("settings") or {}
    head = [
        f"Nodach Live report · client {info.get('version', VERSION)} · {info.get('at') or iso(utcnow())}",
        f"os: {info.get('os', '?')} · python {info.get('python', '?')}",
        f"hub: {info.get('hub', '?')}",
        f"signed in: {info.get('role') or 'not signed in'}",
        f"session: {info.get('session') or 'none selected'}",
        f"microphone: {info.get('mic') or 'none'}",
        f"speech model: {info.get('model', '?')} · {info.get('model_status', '?')}",
        "settings: " + (", ".join(f"{k}={'on' if v is True else 'off' if v is False else v}" for k, v in settings.items()) or "-"),
    ]
    rows = list(rows or [])[-n:]
    fails = sum(1 for r in rows if r.get("outcome") == "failed")
    head.append(f"activity: last {len(rows)} rows, {fails} failed")
    head.append(f"{'time':8}  {'step':10}  {'outcome':12}  detail")
    body = [format_row(r) for r in rows] or ["(nothing yet)"]
    return "\n".join(scrub(x) for x in head + body) + "\n"


def report_event(session: str, me: dict, text: str, t: dt.datetime, branch: str = "main", auto: str = "") -> dict:
    """The report as a comment on the session item, so Claude can read it through the connector.
    auto: why it was sent by itself ("failure" or "session ended"); empty when he pressed Send report."""
    ts = iso(t)
    body = {"text": scrub(text)[:8000], "report": True}
    if auto:
        body["auto"] = auto
    return {"ts": ts, "kind": "comment", "item": session, "branch": branch, "body": body, "author": author(me)}


# ---------------------------------------------------------------- 0.2.2: automatic reports
AUTO_STEPS = ("record", "transcribe", "send", "sign-in", "session")   # a failed row in one of these sends a report
AUTO_EVERY_S = 300                                                    # at most one failure report per 5 minutes
AUTO_RETRY_S = 60                                                     # queued reports are tried again after this


def auto_report_due(row: dict, last_sent, now: dt.datetime, every_s: int = AUTO_EVERY_S) -> bool:
    """Should this activity row send an automatic report now? Only failed rows of AUTO_STEPS, and not within
    every_s of the last automatic failure report (last_sent: datetime or None). Report rows never trigger one."""
    if not row or row.get("outcome") != "failed" or row.get("step") not in AUTO_STEPS:
        return False
    return last_sent is None or (now - last_sent).total_seconds() >= every_s


def auto_report_text(report: str, reason: str, row=None) -> str:
    """The filtered report with one line on top saying why it was sent by itself."""
    why = f"Automatic report ({reason})"
    if row:
        why += f": {row.get('step', '')} {row.get('outcome', '')} · {row.get('detail', '')}"
    return scrub(why)[:300] + "\n" + report


def retry_due(queued, last_try, now: dt.datetime, every_s: int = AUTO_RETRY_S) -> bool:
    """Is it time to try the locally queued reports again?"""
    return bool(queued) and (last_try is None or (now - last_try).total_seconds() >= every_s)
