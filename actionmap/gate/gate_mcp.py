"""
Nodach Gate: the MCP server Claude desktop connects to (as "nodach-gate"), in front of Windows-MCP.

    Claude desktop  --stdio-->  gate_mcp.py  --stdio-->  python -m windows_mcp serve --tools <UI allowlist>

- Starts Windows-MCP as its own child and is its client. Only the UI tools in gate_core.UI_TOOLS are offered to
  Claude (allowlist): Windows-MCP is also started with --tools set to that list, and anything else it still lists
  is hidden by Gate too. Hidden tools are logged by name at start (kind "start").
- Every request goes through gate_core.decide() first: never (refused), confirm (popup on this PC), auto.
- Every request is logged to %USERPROFILE%\\nodach-gate\\actions.jsonl (gate.v1), with screenshots/clips when the
  rules in gate_core say so, and automatic reports queued for Studio (gate_studio).
- Two tools of Gate's own: capture_view(note) and gate_status().

Run by Claude desktop (see install_gate.py). For a manual check: python gate_mcp.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_core as core      # noqa: E402
import gate_studio as studio  # noqa: E402
import gate_win as win        # noqa: E402

HOME = studio.HOME
LOG = HOME / "actions.jsonl"
STATE = HOME / "state.json"
ZONES = HOME / "zones.json"
KNOWN_WMCP_TOOLS = ("App", "ControlStatus", "Clipboard", "DisplayInventory", "Click", "Type", "Scroll", "Move",
                    "Shortcut", "Wait", "WaitFor", "Notification", "MultiSelect", "MultiEdit", "PowerShell", "Process",
                    "Scrape", "Snapshot", "Screenshot", "FileSystem", "Registry")

GATE_TOOL_DEFS = [
    {"name": "capture_view", "description": "Nodach Gate: save a screenshot of the window in front on Nowed's PC (kept locally, "
     "referenced in the Gate log) with a short note of why. Use when visual detail matters for the process record. Not "
     "taken while a protected window (password manager, banking, Windows Security) is in front.",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string", "description": "why this view matters"}}, "required": ["note"]}},
    {"name": "gate_status", "description": "Nodach Gate: zones revision, paused or not, screenshot/clip toggles, active "
     "'allow for 10 min' grants, and which Windows-MCP tools Gate hides.",
     "inputSchema": {"type": "object", "properties": {}}},
]
INSTRUCTIONS = (
    "Nodach Gate sits in front of Windows-MCP on Nowed's PC. Only UI tools are available (no PowerShell, files, "
    "registry or processes). Act by loc=[x, y] (the centre Snapshot shows), not label. Some actions need Nowed's OK in "
    "a popup on his PC (send/post/delete/pay/install..., Enter in a message box, Delete keys, launching apps not on his "
    "list); password managers, banking/payment pages, Windows Security/UAC, Settings > Accounts/Privacy and Claude's "
    "own window are refused. A refusal says why: don't try to work round it, tell Nowed. Prefer Snapshot (UI-tree text) "
    "over Screenshot to save tokens. When the desktop work is done, call ControlStatus with release=true.")


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


class Log:
    """Append-only gate.v1 log; the recent entries stay in memory for clips and reports."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.recent = collections.deque(maxlen=1000)
        self.listeners = []

    def add(self, entry: dict, t: float | None = None) -> dict:
        entry = dict(entry)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            entry["_t"] = t or time.time()
            self.recent.append(entry)
        for fn in list(self.listeners):
            try:
                fn(entry)
            except Exception:
                pass
        return entry


class Gate:
    def __init__(self, ctxp=None, home: Path = HOME, popup=None, child_cmd=None, studio_mod=studio, autostart_gui=True):
        self.home = Path(home)
        self.ctxp = ctxp or (win.WinContext() if win.AVAILABLE else win.NullContext())
        self.popup = popup or self._popup_process
        self.child_cmd = child_cmd
        self.studio = studio_mod
        self.log = Log(self.home / "actions.jsonl")
        self.session_id = "GS-" + dt.datetime.now().strftime("%y%m%d-%H%M%S")
        self.grants = core.Grants()
        self.lock = asyncio.Lock()
        self.zones_mtime = None
        self.zones = core.load_zones(None)
        self.reload_zones()
        self.exposed, self.hidden = [], []
        self.last_action_t = None
        self.last_run_end_t = None
        self.last_summary_t = None
        self.prev_action = None
        self.clip_limiter = core.ClipLimiter(**{k: self.zones["capture"]["glitch"][k] for k in ("cooldown_s", "per_hour")})
        self.limits = read_json(self.home / "report-limits.json", {})
        self.limits_lock = threading.Lock()
        self.clips = None
        self.autostart_gui = autostart_gui
        self.stop_ev = threading.Event()
        self.keep_req_seen = read_json(self.home / "state.json", {}).get("keep_clip_req", 0)

    # ------------------------------------------------------------ settings
    def zones_path(self) -> Path:
        return self.home / "zones.json"

    def reload_zones(self):
        p = self.zones_path()
        try:
            m = p.stat().st_mtime
        except OSError:
            m = None
        if m == self.zones_mtime and self.zones_mtime is not None:
            return
        self.zones_mtime = m
        try:
            self.zones = core.load_zones(p if m else None)
        except (OSError, ValueError) as e:
            self.log.add(core.note_entry(time.time(), "error", self.session_id, detail=f"zones.json unreadable, kept the last good rules ({e})"))
            return
        if m:   # keep every version seen (no silent overwrites)
            hist = self.home / "zones-history"
            hist.mkdir(parents=True, exist_ok=True)
            dst = hist / f"zones-r{self.zones.get('revision', 0)}-{self.zones['_sha']}.json"
            if not dst.exists():
                dst.write_bytes(p.read_bytes())

    def zones_rev(self) -> str:
        return f"{self.zones.get('revision', 0)}-{self.zones.get('_sha', '')}"

    def state(self) -> dict:
        return read_json(self.home / "state.json", {})

    def cap(self) -> dict:
        return core.capture_settings(self.zones, self.state())

    def reports(self) -> dict:
        return core.auto_report_settings(self.zones, self.state())

    def is_never(self, app, title) -> str | None:
        return core._never(self.zones, app, title, "", input_action=False)

    # ------------------------------------------------------------ child
    def child_params(self):
        from mcp.client.stdio import StdioServerParameters
        env = dict(os.environ)
        # 21 Nov (test 4): Windows-MCP's own control layer showed its 'AI is taking control' prompt and locked the
        # mouse, so Nowed couldn't click Gate's Allow and had to press its escape keys. Gate is the one consent
        # layer: Windows-MCP runs with its control layer off.
        env.update({"ANONYMIZED_TELEMETRY": "false", "PYTHONUTF8": "1", "WINDOWS_MCP_CONTROL": "false"})
        env.pop("WINDOWS_MCP_EXCLUDE_TOOLS", None)
        env["WINDOWS_MCP_TOOLS"] = ",".join(core.UI_TOOLS)
        test_cmd = os.environ.get("NODACH_GATE_CHILD_CMD")      # tests only: a stand-in for Windows-MCP
        cmd = self.child_cmd or (json.loads(test_cmd) if test_cmd else None) \
            or [sys.executable, "-m", "windows_mcp", "serve", "--no-control", "--tools", ",".join(core.UI_TOOLS)]
        return StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)

    async def connect(self, stack: contextlib.AsyncExitStack):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        r, w = await stack.enter_async_context(stdio_client(self.child_params(), errlog=open(os.devnull, "w")
                                                            if os.environ.get("NODACH_GATE_QUIET") else sys.stderr))
        self.child = await stack.enter_async_context(ClientSession(r, w))
        await self.child.initialize()
        listed = (await self.child.list_tools()).tools
        self.exposed, extra_hidden = core.filter_tools(listed)
        not_started = [n for n in KNOWN_WMCP_TOOLS if n not in core.UI_TOOLS]
        self.hidden = sorted(set(not_started) | {core.tool_name(t) for t in extra_hidden})
        self.log.add(core.note_entry(time.time(), "start", self.session_id, version=core.VERSION, zones_rev=self.zones_rev(),
                                     exposed=sorted(core.tool_name(t) for t in self.exposed) + list(core.GATE_TOOLS),
                                     hidden=self.hidden, hidden_by_gate_filter=sorted(core.tool_name(t) for t in extra_hidden),
                                     windows_mcp=_wmcp_version()))

    def tool_defs(self):
        out = []
        for t in self.exposed:
            d = t.model_dump(by_alias=True, exclude_none=True) if hasattr(t, "model_dump") else dict(t)
            d["inputSchema"] = core.strip_label(d.get("inputSchema") or {})
            if d.get("name") in ("Snapshot", "Screenshot"):
                (d["inputSchema"].get("properties") or {}).pop("save_path", None)
            # 21 Nov: Click/Type declared an output schema, so Claude rejected Gate's plain-text results
            # ("has an output schema but did not return structured content") although the action ran.
            d.pop("outputSchema", None)
            out.append(d)
        return out + GATE_TOOL_DEFS

    # ------------------------------------------------------------ the request path
    async def call(self, name: str, args: dict) -> dict:
        """Returns {"content": [...], "is_error": bool} with MCP content dicts or objects."""
        t0 = time.time()
        args = dict(args or {})
        self.reload_zones()
        st = self.state()
        cap = core.capture_settings(self.zones, st)
        if name not in {core.tool_name(t) for t in self.exposed} and name not in core.GATE_TOOLS:
            ctx = {}
            zone, reason = "hidden", f"{name} isn't offered by Nodach Gate"
            self._log_action(t0, name, args, ctx, zone, reason, "refused", "auto", ok=False, error=reason)
            return _err(f"Nodach Gate: {reason}.")
        async with self.lock:
            ctx = await asyncio.to_thread(self.ctxp.context, name, args)
            ctx["paused"] = bool(st.get("paused"))
            zone, reason = core.decide({"tool": name, "args": args}, ctx, self.zones)
            if name in core.GATE_TOOLS:
                return await self._gate_tool(name, args, ctx, t0, cap)
            if zone in ("never", "invalid", "hidden"):
                e = self._log_action(t0, name, args, ctx, zone, reason, "refused", "pause" if ctx["paused"] else "auto", ok=False, error=reason)
                self.maybe_report(e)
                return _err(f"Nodach Gate refused this ({zone}): {reason}." + ("" if zone == "invalid" else " Tell Nowed instead of working round it."))
            fg_never = bool(self.is_never(ctx.get("fg_app", ctx.get("app")), ctx.get("fg_title", ctx.get("title"))))
            shot = None
            reasons = core.shot_reasons("before", tool=name, zone=zone, ctx=ctx, settings=cap)
            if reasons and core.can_capture(zone, fg_never):
                shot = await self._shot(ctx, "+".join(reasons), cap)
            by = "auto"
            if zone == "confirm":
                app = (ctx.get("target_app") or str(args.get("name") or "")) if name == "App" else (ctx.get("app") or "")
                if self.grants.active(app, time.time()):
                    by = "grant"
                else:
                    answer = await asyncio.to_thread(self.popup, {
                        "action": describe(name, args, ctx), "app": app, "title": core.mask_text(ctx.get("title"), False, 80),
                        "element": ctx.get("element_name") or "", "reason": reason, "shot": str(self.home / shot) if shot else None,
                        "timeout_s": self.zones["confirm"].get("popup_timeout_s", 60),
                        "grant_minutes": self.zones["confirm"].get("grant_minutes", 10)})
                    decision, by = core.popup_outcome(answer, app, self.grants, time.time(), self.zones["confirm"].get("grant_minutes", 10))
                    if decision == "blocked":
                        e = self._log_action(t0, name, args, ctx, zone, reason, "blocked", by, ok=False,
                                             error="blocked by Nowed" if by == "user" else "no answer in time", shot=shot)
                        self.maybe_report(e)
                        return _err("Nodach Gate: Nowed " + ("blocked this." if by == "user" else "didn't answer the popup in time, so it was blocked.")
                                    + " Ask him in the chat before trying again.")
            before = await asyncio.to_thread(self.ctxp.window_hash, ctx) if name in core.INPUT_TOOLS and cap.get("screenshots", True) else None
            ok, err, result = True, "", None
            try:
                result = await self.child.call_tool(name, args)
                ok = not bool(getattr(result, "is_error", False))
                # 21 Nov: clicking Allow in the popup is mouse activity, so Windows-MCP then refused with
                # USER_CONTROL ("wait ~10 s"). After an approval, wait the time it asks for and try once more.
                wait = core.user_control_wait(_text_of(result)) if not ok else None
                if wait is not None and by in ("user", "grant"):
                    await asyncio.sleep(min(wait, 15) + 0.5)
                    result = await self.child.call_tool(name, args)
                    ok = not bool(getattr(result, "is_error", False))
                if not ok:
                    err = _text_of(result)[:300]
            except Exception as e:  # child died or rejected the call
                ok, err = False, f"{type(e).__name__}: {e}"
            if name == "Type":
                self.ctxp.note_typed(ctx, time.time())
            after = await asyncio.to_thread(self.ctxp.window_hash, ctx) if before is not None and ok else None
            reasons = core.shot_reasons("after", tool=name, zone=zone, ctx=ctx, settings=cap, ok=ok, before_hash=before, after_hash=after)
            if reasons and not shot and core.can_capture(zone, fg_never):
                shot = await self._shot(ctx, "+".join(reasons), cap)
            entry = self._log_action(t0, name, args, ctx, zone, reason, "allowed", by, ok=ok, error=err, shot=shot,
                                     extra={"after": reasons} if reasons else None)
            clip = self._keep_clip("error") if not ok else None
            self.maybe_report(entry, clip)
            self.last_action_t = time.time()
        if result is None:
            return _err(f"Nodach Gate: Windows-MCP failed: {err}")
        return self._shape_result(name, result)

    def _shape_result(self, name, result):
        content = list(getattr(result, "content", []) or [])
        if name == "Snapshot":
            out = []
            for c in content:
                if getattr(c, "type", "") == "text":
                    text, n = core.redact_snapshot(c.text, self.zones)
                    self.ctxp.elements = core.parse_elements(text)
                    if n:
                        text += f"\n\n[Nodach Gate hid {n} protected window(s).]"
                    c = type(c)(type="text", text=text)
                out.append(c)
            content = out
        return {"content": content, "is_error": bool(getattr(result, "is_error", False)),
                "structured_content": getattr(result, "structured_content", None)}

    def sign_in_state(self) -> dict:
        """Whether reports can reach Studio: saved sign-in present and accepted (never the value itself)."""
        hub_name = (self.reports() or {}).get("hub", "staging")
        url = studio.hub_url(hub_name)
        tok = studio.stored_token(url)
        if not tok:
            return {"hub": hub_name, "signed_in": False, "why": "no saved sign-in: click Sign in in the Gate window"}
        try:
            me = studio.Hub(url, tok).me()
            return {"hub": hub_name, "signed_in": True, "role": me.get("role") or ("owner" if me.get("owner") else "member")}
        except Exception as e:
            return {"hub": hub_name, "signed_in": False, "why": f"saved sign-in not accepted ({type(e).__name__})"}

    async def _gate_tool(self, name, args, ctx, t0, cap):
        if name == "gate_status":
            st = self.state()
            body = {"version": core.VERSION, "zones_rev": self.zones_rev(), "paused": bool(st.get("paused")),
                    "screenshots": cap.get("screenshots", True), "clips": cap.get("clips", True),
                    "grants_s_left": self.grants.snapshot(time.time()), "hidden_tools": self.hidden,
                    "auto_reports": self.reports(), "studio": self.sign_in_state(),
                    "reports_waiting": len(studio.pending())}
            return {"content": [{"type": "text", "text": json.dumps(body, indent=1)}], "is_error": False}
        note = str(args.get("note") or "")[:200]
        never = self.is_never(ctx.get("fg_app", ctx.get("app")), ctx.get("fg_title", ctx.get("title")))
        if never or ctx.get("paused"):
            reason = never or "Gate is paused"
            self.log.add(core.note_entry(t0, "view", self.session_id, note=note, shot=None, reason=f"not captured: {reason}"))
            return _err(f"Nodach Gate: no picture taken ({reason}).")
        if not cap.get("screenshots", True):
            self.log.add(core.note_entry(t0, "view", self.session_id, note=note, shot=None, reason="screenshots are off"))
            return _err("Nodach Gate: screenshots are turned off in the Gate window.")
        whole = dict(ctx, element_rect=None, loc=None, rect=ctx.get("fg_rect") or ctx.get("rect"))
        shot = await self._shot(whole, "asked", cap)
        self.log.add(core.note_entry(t0, "view", self.session_id, note=note, shot=shot, app=ctx.get("fg_app", ctx.get("app")),
                                     title=core.mask_text(ctx.get("fg_title", ctx.get("title")), False, 80)))
        return {"content": [{"type": "text", "text": f"Saved on Nowed's PC as {shot} (kept locally, logged)." if shot else "Couldn't capture."}],
                "is_error": not shot}

    async def _shot(self, ctx, why, cap):
        rel = Path("shots") / f"{dt.datetime.now():%y%m%d-%H%M%S-%f}-{why.replace('+', '_')}.png"
        ok = await asyncio.to_thread(self.ctxp.shot, ctx, self.home / rel, int(cap.get("shot_margin", 40)))
        return rel.as_posix() if ok else None

    def _log_action(self, t0, name, args, ctx, zone, reason, decision, by, ok, error="", shot=None, extra=None):
        e = core.log_entry(ts=t0, tool=name, args=args, ctx=ctx, zone=zone, reason=reason, decision=decision, by=by,
                           duration_ms=int((time.time() - t0) * 1000), ok=ok, error=error, shot=shot,
                           zones_rev=self.zones_rev(), session=self.session_id, extra=extra)
        return self.log.add(e, t0)

    # ------------------------------------------------------------ clips and automatic reports
    def _keep_clip(self, trigger):
        """Save the rolling buffer (error / user / glitch). Returns the clip log entry or None."""
        if not self.clips:
            return None
        now = time.time()
        if trigger != "glitch" and not core.clip_trigger(kind=trigger, now=now, settings=self.cap()):
            return None
        if not self.clip_limiter.allow(trigger, now):
            return None
        stem = f"{dt.datetime.now():%y%m%d-%H%M%S}-{trigger}"
        path, n, n_blank, t_from, t_to = self.clips.save(self.home / "clips", stem)
        rel = Path(path).relative_to(self.home).as_posix() if path else None
        e = self.log.add(core.clip_entry(ts=now, start=t_from, end=t_to + 0.5, trigger=trigger, path=rel, frames=n,
                                         blank=n_blank, actions=list(self.log.recent), session=self.session_id), now)
        if trigger == "glitch":
            self.maybe_report(e)
        return e

    def _on_frame_change(self, bits, now):
        t = core.clip_trigger(kind="frame", now=now, settings=self.cap(), last_action_t=self.last_action_t, frame_change_bits=bits)
        if t == "glitch":
            self._keep_clip("glitch")

    def maybe_report(self, entry, clip=None):
        """Queue the automatic report this entry asks for (if any), with its screenshot/clip when pictures are wanted."""
        is_action = entry.get("kind", "action") == "action"
        kind = core.auto_report_trigger(entry, self.reports(), self.prev_action if is_action else None)
        if is_action:
            self.prev_action = entry
        if not kind:
            return None
        files = []
        if core.wants_pictures(kind, self.reports()):
            for e in (entry, clip):
                p = (e or {}).get("shot") or (e or {}).get("path")
                if p:
                    files.append(str(self.home / p))
        self.auto_report(kind, files=files)
        return kind

    def auto_report(self, kind, files=(), now=None):
        now = now or time.time()
        rs = self.reports()
        with self.limits_lock:
            lim = core.ReportLimiter.from_settings(self.limits, rs)
            ok, why = lim.allow(kind, now)
            write_json(self.home / "report-limits.json", self.limits)
        if not ok:
            self.log.add(core.note_entry(now, "report", self.session_id, status="skipped", trigger=kind, detail=why))
            return
        text = core.build_report(self.report_info(kind), [e for e in self.log.recent if e.get("kind") in ("action", "clip", "view")])
        p = self.studio.queue_report(kind, text, files, rs.get("hub", "staging"), now)
        self.log.add(core.note_entry(now, "report", self.session_id, status="queued", trigger=kind, file=p.name,
                                     item=core.day_item_id(dt.datetime.fromtimestamp(now).date()),
                                     attachments=[Path(f).name for f in files]))
        threading.Thread(target=self.flush, daemon=True).start()

    def flush(self):
        try:
            for r in self.studio.flush(self.reports().get("attach_max_mb", 10)):
                self.log.add(core.note_entry(time.time(), "report", self.session_id, status=r["status"], trigger=r["kind"],
                                             file=r["file"], item=r.get("item"), detail=r.get("detail", "")))
        except Exception as e:
            self.log.add(core.note_entry(time.time(), "report", self.session_id, status="queued", detail=f"flush failed: {type(e).__name__}"))

    def report_info(self, trigger):
        cap = self.cap()
        return {"version": core.VERSION, "at": core.iso(time.time()), "os": f"{platform.system()} {platform.release()}",
                "python": platform.python_version(), "windows_mcp": _wmcp_version(), "zones_rev": self.zones_rev(),
                "paused": bool(self.state().get("paused")), "screenshots": cap.get("screenshots", True),
                "clips": cap.get("clips", True), "trigger": trigger}

    def background(self):
        """Every second: 'Keep last 10 s' requests; every 5 s: run-end / session / daily summaries; every 60 s: outbox."""
        n = 0
        while not self.stop_ev.wait(1):
            n += 1
            try:
                st = self.state()
                if st.get("keep_clip_req", 0) != self.keep_req_seen:
                    self.keep_req_seen = st.get("keep_clip_req", 0)
                    self._keep_clip("user")
                if n % 5 == 0:
                    self.periodic(time.time())
                if n % 60 == 0 and self.studio.pending():
                    self.flush()
            except Exception:
                pass

    def periodic(self, now, closing=False):
        rs = self.reports()
        if core.run_end_due(now=now, last_action_t=self.last_action_t, last_run_end_t=self.last_run_end_t, settings=rs) and not closing:
            self.last_run_end_t = now
            self.auto_report("run_end", now=now)
        if core.session_summary_due(now=now, last_action_t=self.last_action_t, last_summary_t=self.last_summary_t,
                                    closing=closing, settings=rs):
            self.last_summary_t = now
            self.auto_report("session_summary", now=now)
        today = dt.datetime.now()
        if core.daily_summary_due(now_local=today, last_daily_date=self.limits.get("daily_date"), settings=rs):
            self.limits["daily_date"] = today.date().isoformat()
            self.auto_report("daily_summary", now=now)

    def start_threads(self):
        if win.AVAILABLE:
            self.clips = win.ClipBuffer(self.is_never, self.cap(), self._on_frame_change)
            self.clips.start()
            threading.Thread(target=self._refresh_clip_settings, daemon=True).start()
        threading.Thread(target=self.background, daemon=True).start()
        if os.name == "nt" and self.autostart_gui and self.zones.get("gui", {}).get("autostart", True):
            start_gui_if_needed(self.home)

    def _refresh_clip_settings(self):
        while not self.stop_ev.wait(2):
            if self.clips:
                self.clips.settings.update(self.cap())

    def close(self):
        try:
            self.periodic(time.time(), closing=True)
        finally:
            self.stop_ev.set()
            if self.clips:
                self.clips.stop()

    # ------------------------------------------------------------ popup
    def _popup_process(self, req: dict) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(req, f)
        f.close()
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        try:
            out = subprocess.run([str(pyw if pyw.exists() else exe), str(HERE / "gate_popup.py"), f.name],
                                 capture_output=True, text=True, timeout=int(req.get("timeout_s", 60)) + 15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else "timeout"
        except Exception:
            return "timeout"
        finally:
            with contextlib.suppress(OSError):
                os.unlink(f.name)


def describe(name, args, ctx) -> str:
    el = ctx.get("element_name") or ""
    if name == "Click":
        return f"click '{el or 'at ' + str(args.get('loc'))}'"
    if name == "Type":
        secret = core.looks_secret(el, ctx.get("element_type", ""), ctx.get("is_password"))
        return f"type {core.mask_text(args.get('text'), secret, 60)!r} into '{el}'" + (" and press Enter" if core._truthy(args.get("press_enter")) else "")
    if name == "Shortcut":
        return f"press {args.get('shortcut')}" + (f" (focus: '{el}')" if el else "")
    if name == "App":
        return f"{args.get('mode', 'launch')} app '{args.get('name')}'"
    if name == "Move":
        return f"drag to '{el or args.get('loc')}'"
    return name


def _text_of(result) -> str:
    return " ".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [] if getattr(c, "type", "") == "text")


def _err(text):
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _wmcp_version() -> str:
    try:
        from importlib.metadata import version
        return version("windows-mcp")
    except Exception:
        return "?"


def gui_running(home: Path) -> bool:
    pid = read_json(home / "gui.pid", {}).get("pid")
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def start_gui_if_needed(home: Path):
    if gui_running(home) or not (HERE / "gate_gui.py").exists():
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    with contextlib.suppress(Exception):
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(HERE / "gate_gui.py")], cwd=str(HERE),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------------------------------------------------------- the MCP server
def build_server(gate: Gate):
    import mcp.types as types
    from mcp.server.lowlevel import Server

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(d) for d in gate.tool_defs()])

    async def call_tool(ctx, params):
        r = await gate.call(params.name, params.arguments or {})
        content = [types.TextContent(type="text", text=c["text"]) if isinstance(c, dict) else c for c in r["content"]]
        return types.CallToolResult(content=content, is_error=r["is_error"])   # no structured content: schemas are stripped

    return Server("nodach-gate", version=core.VERSION, instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(gate: Gate):
    from mcp.server.stdio import stdio_server
    server = build_server(gate)
    async with contextlib.AsyncExitStack() as stack:
        await gate.connect(stack)
        gate.start_threads()
        try:
            async with stdio_server() as (r, w):
                await server.run(r, w, server.create_initialization_options())
        finally:
            gate.close()


def ensure_home(home: Path = HOME):
    home.mkdir(parents=True, exist_ok=True)
    z = home / "zones.json"
    if not z.exists() and core.DEFAULT_ZONES.exists():
        z.write_bytes(core.DEFAULT_ZONES.read_bytes())
        return
    try:
        user = json.loads(z.read_text(encoding="utf-8"))
        default = json.loads(core.DEFAULT_ZONES.read_text(encoding="utf-8"))
        merged, added = core.upgrade_zones(user, default)
        if merged is not user:
            hist = home / "zones-history"
            hist.mkdir(exist_ok=True)
            (hist / f"zones-rev{user.get('revision')}-{dt.datetime.now():%y%m%d-%H%M%S}.json").write_bytes(z.read_bytes())
            z.write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass   # a broken zones.json is reported by the normal zone loader


async def check():
    """--check: start Windows-MCP through Gate and print what's offered and hidden (no actions)."""
    ensure_home()
    g = Gate(autostart_gui=False)
    async with contextlib.AsyncExitStack() as stack:
        await g.connect(stack)
        print("Nodach Gate", core.VERSION, "· zones", g.zones_rev(), "· windows-mcp", _wmcp_version())
        print("offered:", ", ".join(d["name"] for d in g.tool_defs()))
        print("hidden: ", ", ".join(g.hidden))
        print("log:    ", g.log.path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nodach Gate MCP server (stdio)")
    ap.add_argument("--check", action="store_true", help="start Windows-MCP through Gate, list tools, exit")
    a = ap.parse_args(argv)
    if a.check:
        asyncio.run(check())
        return 0
    ensure_home()
    asyncio.run(serve(Gate()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
