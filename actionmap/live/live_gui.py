"""
Nodach Live — the window (B-live-session, client 0.2.2)
=======================================================
A click-only way to talk into a Nodach Studio live session: no command line and no keyboard shortcuts needed.
start-live.bat opens this window; start-live-cli.bat still opens the old command-line client.

Top to bottom: hub and sign-in · session · microphone (device, level meter, speech model) · the Talk button ·
a text box · the activity list (every step with its outcome) · Copy report / Send report to Claude.

Speech is turned into text on this PC (faster-whisper); audio never leaves it. Only text lines, and the report
when you press its button, go to the hub. The sign-in value is read from the clipboard, kept only in
%USERPROFILE%\\nodach-live\\session-<hub>.txt, and never shown.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_core as core  # noqa: E402
import live_client as lc  # noqa: E402

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

TICK_MS = 100
ALWAYS_EVERY_MS = 250
QUIET_MS = 800
MIN_SECONDS = 0.3


# ---------------------------------------------------------------- microphone capture for the window
class Recorder:
    """One input stream while listening. Keeps the frames, each frame's loudness (for the meter and for cutting
    phrases) and the peak level. Nothing is written to disk."""

    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.frames: list = []
        self.levels: list = []
        self.level = 0.0
        self.peak = 0.0
        self.total = 0           # samples recorded since start (for the report)
        self.stream = None

    def _cb(self, data, frames, t, status):
        x = data[:, 0].copy()
        rms = float(lc.np.sqrt(lc.np.mean(x * x))) if len(x) else 0.0
        with self.lock:
            self.frames.append(x)
            self.levels.append(rms)
            self.level = rms
            self.peak = max(self.peak, float(lc.np.max(lc.np.abs(x))) if len(x) else 0.0)
            self.total += len(x)

    def start(self):
        self.stream = lc.sd.InputStream(device=self.device, samplerate=lc.RATE, channels=1, dtype="float32",
                                        blocksize=lc.RATE * lc.FRAME_MS // 1000, callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.level = 0.0

    def take(self, upto=None):
        """The audio so far (or the first `upto` frames), removed from the buffer."""
        with self.lock:
            if upto is None:
                frames, self.frames, self.levels = self.frames, [], []
            else:
                frames, self.frames, self.levels = self.frames[:upto], self.frames[upto:], self.levels[upto:]
        return lc.np.concatenate(frames) if frames else None

    def snapshot_levels(self):
        with self.lock:
            return list(self.levels)


# ---------------------------------------------------------------- the window
class App:
    def __init__(self, root: tk.Tk, hub_name: str = "staging", model: str = "base"):
        self.root = root
        self.ui: "queue.Queue" = queue.Queue()        # work for the window thread, from background threads
        self.tq: "queue.Queue" = queue.Queue()        # audio waiting to be transcribed (one at a time)
        self.rows: list = []
        self.hub = None
        self.me = None
        self.live = None
        self.sessions: list = []
        self.devices: list = []
        self.model_name = model
        self.model = None
        self.model_status = "loading"
        self.model_source = ""
        self.recorder = None
        self.recording = False
        self.always = False
        self.rec_start = 0.0
        self.last_toggle = 0.0
        self.hotkeys = None
        self.threshold = float(os.environ.get("NODACH_LIVE_LEVEL", "0.015"))
        self.auto_last = None          # when the last automatic failure report went (at most one per 5 minutes)
        self.auto_queue: list = []     # automatic reports that couldn't be sent yet, tried again every minute
        self.auto_try = None

        root.title(f"Nodach Live {core.VERSION}")
        root.geometry("780x760")
        root.minsize(640, 600)
        root.report_callback_exception = self._tk_error
        self._build(hub_name)
        threading.Thread(target=self._transcriber, daemon=True).start()
        self._load_devices()
        self._load_model()
        self._connect(stored=True)
        root.after(TICK_MS, self._tick)
        root.after(ALWAYS_EVERY_MS, self._always_tick)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ---------- layout
    def _build(self, hub_name: str):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="both", expand=True)

        # 1. hub and sign-in
        f1 = ttk.LabelFrame(top, text="1  Hub and sign-in")
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="Hub").grid(row=0, column=0, sticky="w", padx=4)
        self.hub_var = tk.StringVar(value=hub_name if hub_name in core.HUBS else "staging")
        hub_box = ttk.Combobox(f1, textvariable=self.hub_var, values=list(core.HUBS), state="readonly", width=12)
        hub_box.grid(row=0, column=1, sticky="w", padx=4)
        hub_box.bind("<<ComboboxSelected>>", lambda e: self._hub_changed())
        self.who_var = tk.StringVar(value="Not signed in")
        ttk.Label(f1, textvariable=self.who_var, width=30).grid(row=0, column=2, sticky="w", padx=8)
        ttk.Button(f1, text="Sign in", command=self.sign_in).grid(row=0, column=3, padx=4)
        ttk.Button(f1, text="Open Studio", command=self.open_studio).grid(row=0, column=4, padx=4)
        ttk.Label(f1, foreground="#555", wraplength=720, text=(
            "To sign in: open Studio in your browser and sign in, press F12 → Application → Cookies → "
            "nodach_session, copy its Cookie Value, then click Sign in (it is read from the clipboard, never shown).")
        ).grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=(2, 4))

        # 2. session
        f2 = ttk.LabelFrame(top, text="2  Session")
        f2.pack(fill="x", **pad)
        self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(f2, textvariable=self.session_var, state="readonly", width=40)
        self.session_box.grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.session_box.bind("<<ComboboxSelected>>", lambda e: self._session_picked())
        ttk.Button(f2, text="Refresh", command=self.refresh_sessions).grid(row=0, column=1, padx=4)
        self.start_btn = ttk.Button(f2, text="Start new session", command=self.start_session)
        self.start_btn.grid(row=0, column=2, padx=4)
        self.end_btn = ttk.Button(f2, text="End session", command=self.end_session)
        self.end_btn.grid(row=0, column=3, padx=4)
        # 0.2.2: shown (and the list turns yellow) when the chosen session has ended and Live can't tell which to use
        ttk.Style(self.root).configure("Pick.TCombobox", fieldbackground="#ffe08a", background="#ffb300")
        self.pick_var = tk.StringVar()
        self.pick_lbl = ttk.Label(f2, textvariable=self.pick_var, foreground="#a15c00")
        self.pick_lbl.grid(row=1, column=0, columnspan=4, sticky="w", padx=4)

        # 3. microphone
        f3 = ttk.LabelFrame(top, text="3  Microphone")
        f3.pack(fill="x", **pad)
        self.mic_var = tk.StringVar()
        self.mic_box = ttk.Combobox(f3, textvariable=self.mic_var, state="readonly", width=42)
        self.mic_box.grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.meter = tk.Canvas(f3, width=160, height=14, bg="#eee", highlightthickness=1, highlightbackground="#aaa")
        self.meter.grid(row=0, column=1, padx=8)
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, 14, fill="#3a3", width=0)
        self.find_btn = ttk.Button(f3, text="Find my mic", command=self.find_mic)
        self.find_btn.grid(row=0, column=2, padx=4)
        self.model_var = tk.StringVar(value=f"speech model ({self.model_name}): loading…")
        ttk.Label(f3, textvariable=self.model_var).grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
        self.install_btn = ttk.Button(f3, text="Install voice packages", command=self.install_packages)

        # 4. talk
        f4 = ttk.Frame(top)
        f4.pack(fill="x", **pad)
        style = ttk.Style()
        style.configure("Talk.TButton", font=("Segoe UI", 16, "bold"), padding=12)
        self.talk_btn = ttk.Button(f4, text=core.talk_label(False), style="Talk.TButton", command=self.toggle_talk)
        self.talk_btn.pack(fill="x")
        opts = ttk.Frame(f4)
        opts.pack(fill="x", pady=(4, 0))
        self.keep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Keep listening (each pause sends a line)", variable=self.keep_var).pack(side="left")
        self.hotkey_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Enable keyboard shortcut (Ctrl+Alt+J)", variable=self.hotkey_var,
                        command=self._hotkey_changed).pack(side="left", padx=16)

        # 5. text
        f5 = ttk.LabelFrame(top, text="5  Type a line")
        f5.pack(fill="x", **pad)
        self.text_var = tk.StringVar()
        entry = ttk.Entry(f5, textvariable=self.text_var)
        entry.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        entry.bind("<Return>", lambda e: self.send_typed())
        ttk.Button(f5, text="Send", command=self.send_typed).grid(row=0, column=1, padx=4)
        f5.columnconfigure(0, weight=1)
        self.point_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f5, text="Attach my latest pointer point", variable=self.point_var,
                        command=self._point_changed).grid(row=1, column=0, sticky="w", padx=4)
        ttk.Button(f5, text="Send latest point", command=self.send_point).grid(row=1, column=1, padx=4, pady=(0, 4))

        # 6. activity
        f6 = ttk.LabelFrame(top, text="6  Activity")
        f6.pack(fill="both", expand=True, **pad)
        cols = ("time", "step", "outcome", "detail")
        self.tree = ttk.Treeview(f6, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (70, 90, 115, 445)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, stretch=(c == "detail"))
        self.tree.tag_configure("failed", foreground="#c00")
        self.tree.tag_configure("empty", foreground="#b60")
        sb = ttk.Scrollbar(f6, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 7. report
        f7 = ttk.Frame(top)
        f7.pack(fill="x", **pad)
        ttk.Button(f7, text="Copy report", command=self.copy_report).pack(side="left")
        ttk.Button(f7, text="Send report to Claude", command=self.send_report).pack(side="left", padx=8)
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f7, text="Automatic reports", variable=self.auto_var).pack(side="left", padx=4)
        ttk.Label(f7, foreground="#555", text="The report holds steps and settings only: no audio, transcripts or sign-in. "
                  "Automatic: sent when a step fails (at most every 5 min) and when a session ends.", wraplength=420).pack(side="left", padx=8)
        self._update_buttons()

    # ---------- plumbing
    def later(self, fn, *a):
        """Run fn on the window thread (safe from any thread)."""
        self.ui.put((fn, a))

    def bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

    def row(self, step, outcome, detail=""):
        r = core.activity_row(step, outcome, str(detail))
        self.rows.append(r)
        tag = ("failed",) if outcome == "failed" else ("empty",) if outcome == "empty speech" else ()
        iid = self.tree.insert("", "end", values=(r["time"], r["step"], r["outcome"], r["detail"]), tags=tag)
        self.tree.see(iid)
        try:   # append-only local copy, so earlier runs can be compared
            lc.HOME.mkdir(parents=True, exist_ok=True)
            with (lc.HOME / "activity.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(dict(r, client=core.VERSION), ensure_ascii=False) + "\n")
        except OSError:
            pass
        if r["step"] != "report":
            self._auto_check(r)

    # ---------- automatic reports (0.2.2)
    def _auto_check(self, r):
        if not (getattr(self, "auto_var", None) and self.auto_var.get()):
            return
        now = core.utcnow()
        if core.auto_report_due(r, self.auto_last, now):
            self.auto_last = now
            self._auto_send(self.live.session if self.live else None, "failure", r)

    def _auto_send(self, sid, reason, r=None):
        self._post_auto({"sid": sid, "reason": reason, "text": core.auto_report_text(self.report_text(), reason, r)})

    def _post_auto(self, job):
        hub, me = self.hub, self.me
        sid = job["sid"] or (self.live.session if self.live else None)
        if not (hub and me and sid):
            return self._auto_queued(job, "not signed in or no session yet")
        job = dict(job, sid=sid)

        def work():
            try:
                hub.post(core.report_event(sid, me, job["text"], core.utcnow(), auto=job["reason"]))
            except Exception as e:
                self.later(self._auto_queued, job, f"hub {getattr(e, 'code', 0) or 'unreachable'}")
                return
            self.later(self._auto_log, job, "sent", f"hub {hub.last_status}")
        self.bg(work)

    def _auto_queued(self, job, why):
        self.auto_queue.append(job)
        self._auto_log(job, "queued", f"{why}; tried again in {core.AUTO_RETRY_S // 60} min")

    def _auto_log(self, job, what, detail):
        self.row("report", "ok" if what == "sent" else "info",
                 f"auto-report {what} ({job['reason']}){' to ' + job['sid'] if job.get('sid') and what == 'sent' else ''} · {detail}")
        try:
            lc.HOME.mkdir(parents=True, exist_ok=True)
            with (lc.HOME / "auto-reports.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": core.iso(core.utcnow()), "what": what, "reason": job["reason"], "session": job.get("sid"),
                                    "detail": core.scrub(detail)}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _auto_retry(self):
        now = core.utcnow()
        if core.retry_due(self.auto_queue, self.auto_try, now):
            self.auto_try = now
            jobs, self.auto_queue = self.auto_queue, []
            for j in jobs:
                self._post_auto(j)

    def _tk_error(self, exc, val, tb):
        self.row("setup", "failed", f"window error {exc.__name__}: {val}")

    def _live_report(self, step, outcome, detail, message):   # called from Live's sender thread
        self.later(self.row, step, outcome, detail)

    def _tick(self):
        while True:
            try:
                fn, a = self.ui.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*a)
            except Exception as e:
                self.row("setup", "failed", f"{type(e).__name__}: {e}")
        self._auto_retry()
        if self.recording and self.recorder:
            self.talk_btn.configure(text=core.talk_label(True, time.monotonic() - self.rec_start, self.always))
            frac = core.level_fraction(self.recorder.level)
        else:
            frac = 0.0
        self.meter.coords(self.meter_bar, 0, 0, int(160 * frac), 14)
        self.meter.itemconfigure(self.meter_bar, fill="#c33" if frac > 0.92 else "#3a3")
        self.root.after(TICK_MS, self._tick)

    def _update_buttons(self):
        owner = bool(self.me and self.me.get("owner"))
        self.start_btn.state(["!disabled"] if owner else ["disabled"])
        self.end_btn.state(["!disabled"] if owner and self.live else ["disabled"])

    # ---------- 1. hub and sign-in
    def hub_url(self):
        return core.HUBS.get(self.hub_var.get(), core.HUBS["staging"])

    def _hub_changed(self):
        self._set_live(None)
        self.me = self.hub = None
        self.who_var.set("Not signed in")
        self.sessions = []
        self.session_box.configure(values=[])
        self.session_var.set("")
        self._update_buttons()
        self._connect(stored=True)

    def open_studio(self):
        webbrowser.open(self.hub_url() + "/studio")

    def sign_in(self):
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            raw = ""
        t = core.clean_token(raw)
        if not t:
            self.row("sign-in", "failed", "the clipboard doesn't hold a nodach_session value (name.numbers.long-code)")
            return
        self._connect(token=t)

    def _connect(self, stored=False, token=None):
        url = self.hub_url()
        if stored:
            token = lc.stored_token(url)
            if not token:
                self.row("sign-in", "info", f"no saved sign-in for {self.hub_var.get()}: copy it and click Sign in")
                return
        self.who_var.set("Signing in…")

        def work():
            hub = lc.Hub(url, token)
            try:
                me = hub.me()
            except Exception as e:
                code = getattr(e, "code", 0)
                if code == 401 and stored:
                    lc.forget_token(url)
                self.later(self._signed_in, None, None, code, str(e), stored)
                return
            if not stored:
                lc.save_token(url, token)
            self.later(self._signed_in, hub, me, hub.last_status, "", stored)
        self.bg(work)

    def _signed_in(self, hub, me, code, err, stored):
        if not hub:
            self.who_var.set("Not signed in")
            hint = " (saved sign-in expired: copy it again)" if code == 401 and stored else ""
            self.row("sign-in", "failed", f"hub {code or 'unreachable'}{hint} · {err[:80]}")
            return
        self.hub, self.me = hub, me
        if not stored:
            try:   # don't leave the sign-in value sitting on the clipboard
                if core.clean_token(self.root.clipboard_get()):
                    self.root.clipboard_clear()
            except tk.TclError:
                pass
        role = "owner" if me.get("owner") else "member"
        self.who_var.set(f"Signed in as {role}")
        self.row("sign-in", "ok", f"hub {code} · {role}{' · saved sign-in' if stored else ''}")
        self._update_buttons()
        self.refresh_sessions()

    # ---------- 2. sessions
    def refresh_sessions(self, select=None):
        if not self.hub:
            self.row("session", "failed", "not signed in")
            return
        hub, me = self.hub, self.me

        def work():
            try:
                rows = core.session_list(hub.events(), me["id"], bool(me.get("owner")))
            except Exception as e:
                self.later(self.row, "session", "failed", f"list: hub {getattr(e, 'code', 0) or 'unreachable'} · {e}")
                return
            self.later(self._sessions_loaded, rows, select, hub.last_status)
        self.bg(work)

    def _sessions_loaded(self, rows, select, code):
        self.sessions = rows
        self.session_box.configure(values=[r["label"] for r in rows])
        live_n = sum(1 for r in rows if r["state"] == "live")
        self.row("session", "ok", f"list: hub {code} · {len(rows)} sessions, {live_n} live")
        current = self.live.session if self.live else None
        want = select or current or (rows[0]["id"] if rows and rows[0]["state"] == "live" else None)
        pick = next((r for r in rows if r["id"] == want), None)
        if pick:
            self.session_var.set(pick["label"])
            if pick["id"] != current:
                self._set_live(pick["id"])
        else:
            self.session_var.set("")
            if not rows or rows[0]["state"] != "live":
                self.row("session", "info", "no live session: click Start new session" if self.me.get("owner")
                         else "no live session: ask an owner to start one and give it to you")

    def _session_picked(self):
        label = self.session_var.get()
        r = next((r for r in self.sessions if r["label"] == label), None)
        if r:
            self._set_live(r["id"])
            if r["state"] != "live":
                self.row("session", "info", f"{r['id']} is {r['state']}, not live: each line goes to the one live session, or waits for you to pick")

    def _set_live(self, sid):
        held = []
        if self.live:
            held = self.live.take_held()
            self.live.close()
            self.live = None
        if sid and self.hub:
            self.live = lc.Live(self.hub, self.me, sid, attach_points=self.point_var.get(), report=self._live_report,
                                on_switch=lambda old, new: self.later(self._switched, old, new),
                                on_ask=lambda old, live: self.later(self._ask_pick, old, live))
            self.row("session", "ok", f"using {sid}")
            self._clear_pick()
            if held:
                self.row("send", "info", f"sending {len(held)} held line(s) to {sid}")
                self.live.resend(held)
        self._update_buttons()

    # 0.2.2 (F-live-stale-session): the session ended under us
    def _switched(self, old, new):
        self._clear_pick()
        self.refresh_sessions(new)

    def _ask_pick(self, old, live_ids):
        self.session_box.configure(style="Pick.TCombobox")
        self.pick_var.set(f"⚠ {old} has ended. Pick a live session above (or start one): held lines are sent to it."
                          if live_ids else f"⚠ {old} has ended and no session is live. Start a new session: held lines are sent to it.")
        self.refresh_sessions()

    def _clear_pick(self):
        self.session_box.configure(style="TCombobox")
        self.pick_var.set("")

    def start_session(self):
        if not (self.hub and self.me and self.me.get("owner")):
            self.row("session", "failed", "only a signed-in owner can start a session")
            return
        hub, me = self.hub, self.me

        def work():
            ev = core.session_event(me, core.utcnow())
            try:
                hub.post(ev)
            except Exception as e:
                self.later(self.row, "session", "failed", f"start: hub {getattr(e, 'code', 0) or 'unreachable'} · {e}")
                return
            self.later(self.row, "session", "ok", f"started {ev['body']['id']} · hub {hub.last_status}")
            self.later(self.refresh_sessions, ev["body"]["id"])
        self.bg(work)

    def end_session(self):
        if not self.live:
            self.row("session", "failed", "no session selected")
            return
        if not messagebox.askyesno("End session", f"End {self.live.session}? Lines already sent stay in Studio."):
            return
        live, auto = self.live, self.auto_var.get()

        def work():
            try:
                if live.end() and auto:
                    self.later(self._auto_send, live.session, "session ended")
            except Exception as e:
                self.later(self.row, "session", "failed", f"end: hub {getattr(e, 'code', 0) or 'unreachable'} · {e}")
            self.later(self.refresh_sessions)
        self.bg(work)

    # ---------- 3. microphone and model
    def _load_devices(self):
        if lc.AUDIO_MISSING or lc.sd is None:
            self.mic_box.configure(values=["microphone support not installed"])
            self.mic_box.current(0)
            self.mic_box.state(["disabled"])
            self.install_btn.grid(row=0, column=3, padx=4)
            self.row("mic", "failed", f"no microphone support ({lc.AUDIO_MISSING[:60]}): click Install voice packages")
            return
        try:
            default_in = lc.sd.default.device[0]   # (input, output) pair; -1 when Windows has no default
            self.devices = core.input_devices(list(lc.sd.query_devices()), default_in, list(lc.sd.query_hostapis()))
        except Exception as e:
            self.devices = []
            self.row("mic", "failed", f"couldn't list microphones: {type(e).__name__}: {e}")
        if not self.devices:
            self.mic_box.configure(values=["no microphone found"])
            self.mic_box.current(0)
            self.row("mic", "failed", "no microphone found: typed lines only")
            return
        self.mic_box.configure(values=[d["label"] for d in self.devices])
        self.mic_box.current(0)
        self.row("mic", "ok", f"{len(self.devices)} microphones · default: {self.devices[0]['label']}")

    def find_mic(self):
        """Listen 1.5 s on each input while he talks; pick the loudest real microphone and log every level."""
        if lc.AUDIO_MISSING or lc.sd is None or not self.devices:
            self.row("mic", "failed", "no microphones to test")
            return
        if getattr(self, "rec", None) is not None:
            self.row("mic", "info", "stop talking first, then Find my mic")
            return
        self.find_btn.state(["disabled"])
        self.row("mic", "info", f"testing {len(self.devices)} inputs: keep talking for {int(1.5 * len(self.devices)) + 1} s")

        def work():
            results = []
            for d in self.devices:
                r = Recorder(d["index"])
                try:
                    r.start()
                    time.sleep(1.5)
                    r.stop()
                    results.append((d, r.peak))
                    self.later(self.row, "mic", "info", f"{d['label'][:40]}: peak {r.peak:.3f}")
                except Exception as e:
                    r.stop()
                    results.append((d, 0.0))
                    self.later(self.row, "mic", "failed", f"{d['label'][:40]}: {type(e).__name__}")
            best = core.pick_loudest(results)
            self.later(self._found_mic, best)

        self.bg(work)

    def _found_mic(self, best):
        self.find_btn.state(["!disabled"])
        if best is None:
            self.row("mic", "failed", "no input heard you: check Windows Settings > Privacy > Microphone, and the mic's mute switch")
            return
        self.mic_var.set(best["label"])
        self.row("mic", "ok", f"using {best['label']}")

    def mic_device(self):
        label = self.mic_var.get()
        return next((d for d in self.devices if d["label"] == label), None)

    def _load_model(self):
        if lc.AUDIO_MISSING:
            self._model_done(None, "", "microphone support not installed")
            return

        def work():
            try:
                m, src = lc.load_model(self.model_name, say=lambda msg: None)
            except Exception as e:
                self.later(self._model_done, None, "", f"{type(e).__name__}: {e}")
                return
            self.later(self._model_done, m, src, "")
        self.bg(work)

    def _model_done(self, model, src, err):
        self.model, self.model_source = model, core.scrub(src)
        if model:
            self.model_status = "ready"
            self.model_var.set(f"speech model ({self.model_name}): ready · {self.model_source}")
            self.row("model", "ok", f"{self.model_name} ready · {self.model_source}")
        else:
            self.model_status = "not available → typed only"
            self.model_var.set(f"speech model ({self.model_name}): not available → typed only")
            self.row("model", "failed", f"{self.model_name} not available · {err[:120]}")

    def install_packages(self):
        req = HERE / "requirements.txt"
        self.row("setup", "info", "installing voice packages (a few minutes); the window stays usable")
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-len("pythonw.exe")] + "python.exe"

        def work():
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                p = subprocess.run([exe, "-m", "pip", "install", "-r", str(req)], capture_output=True, text=True,
                                   creationflags=flags, timeout=1800)
                ok = p.returncode == 0
                tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
                self.later(self.row, "setup", "ok" if ok else "failed",
                           "installed: close and reopen this window" if ok else f"pip exit {p.returncode} · {tail[0][:100]}")
            except Exception as e:
                self.later(self.row, "setup", "failed", f"{type(e).__name__}: {e}")
        self.bg(work)

    # ---------- 4. talk
    def toggle_talk(self):
        now = time.monotonic()
        if now - self.last_toggle < 0.4:   # a double click or a doubled key event: ignore the echo
            return
        self.last_toggle = now
        if self.recording:
            self._stop_recording()
            return
        if not self.live:
            self.row("record", "failed", "no session selected (sign in, then pick or start a session)")
            return
        if not self.model:
            self.row("record", "failed", f"speech model {self.model_status}: type the line instead")
            return
        d = self.mic_device()
        if not d:
            self.row("record", "failed", "no microphone selected")
            return
        rec = Recorder(d["index"])
        try:
            rec.start()
        except Exception as e:
            self.row("mic", "failed", f"couldn't open {d['label']}: {type(e).__name__}: {e}")
            return
        self.recorder, self.recording, self.always, self.rec_start = rec, True, self.keep_var.get(), now
        self.talk_btn.configure(text=core.talk_label(True, 0, self.always))
        self.row("record", "info", f"listening on {d['label']}" + (" · keep listening" if self.always else ""))

    def _stop_recording(self):
        rec, always = self.recorder, self.always
        self.recording, self.always, self.recorder = False, False, None
        rec.stop()
        self.talk_btn.configure(text=core.talk_label(False))
        secs = rec.total / lc.RATE
        audio = rec.take()
        left = 0 if audio is None else len(audio) / lc.RATE
        if always:
            self.row("record", "ok", f"stopped listening · {core.record_detail(secs, rec.peak)}")
            if left >= MIN_SECONDS and rec.peak >= self.threshold:
                self.tq.put((audio, "voice-always"))
            return
        if left < MIN_SECONDS:
            self.row("record", "empty speech", f"{core.record_detail(secs, rec.peak)} · too short to send")
            return
        quiet = rec.peak < self.threshold
        self.row("record", "empty speech" if quiet else "ok",
                 core.record_detail(secs, rec.peak) + (" · nothing louder than background (check the microphone)" if quiet else ""))
        self.tq.put((audio, "voice"))

    def _always_tick(self):
        try:
            rec = self.recorder
            if self.recording and self.always and rec:
                levels = rec.snapshot_levels()
                quiet = QUIET_MS // lc.FRAME_MS
                if len(levels) * lc.FRAME_MS > 30000:          # 30 s without a pause: send what there is
                    self.tq.put((rec.take(), "voice-always"))
                else:
                    done = [s for s in core.segments(levels, self.threshold, lc.FRAME_MS) if len(levels) - s[1] >= quiet]
                    if done:
                        audio = rec.take(done[-1][1] + quiet // 2)
                        self.row("record", "ok", f"phrase · {len(audio) / lc.RATE:.1f} s")
                        self.tq.put((audio, "voice-always"))
                    elif levels and max(levels) < self.threshold and len(levels) > 2 * quiet:
                        rec.take(len(levels) - quiet)            # drop old silence
        finally:
            self.root.after(ALWAYS_EVERY_MS, self._always_tick)

    def _transcriber(self):
        while True:
            audio, via = self.tq.get()
            secs = 0 if audio is None else len(audio) / lc.RATE
            t0 = time.monotonic()
            try:
                text = lc.transcribe(self.model, audio)
            except Exception as e:
                self.later(self.row, "transcribe", "failed", f"{type(e).__name__}: {e}")
                continue
            took = time.monotonic() - t0
            if not text:
                self.later(self.row, "transcribe", "empty speech", core.transcribe_detail(secs, 0, took) + " · no words heard")
                continue
            self.later(self.row, "transcribe", "ok", core.transcribe_detail(secs, len(text), took))
            self.later(self._say, text, via)

    def _say(self, text, via):
        if not self.live:
            self.row("send", "failed", "no session selected: line not sent")
            return
        self.live.say(text, via)

    def _hotkey_changed(self):
        if self.hotkey_var.get():
            clash = core.hotkey_clash(core.GUI_HOTKEY)
            if lc.keyboard is None:
                self.hotkey_var.set(False)
                self.row("setup", "failed", "keyboard shortcut needs the pynput package (Install voice packages)")
                return
            if clash:
                self.hotkey_var.set(False)
                self.row("setup", "failed", f"Ctrl+Alt+J is already used by {clash}")
                return
            try:
                self.hotkeys = lc.keyboard.GlobalHotKeys({core.GUI_HOTKEY: lambda: self.later(self.toggle_talk)})
                self.hotkeys.start()
                self.row("setup", "ok", "keyboard shortcut Ctrl+Alt+J on (same as clicking Talk)")
            except Exception as e:
                self.hotkey_var.set(False)
                self.row("setup", "failed", f"keyboard shortcut: {type(e).__name__}: {e}")
        elif self.hotkeys:
            self.hotkeys.stop()
            self.hotkeys = None
            self.row("setup", "ok", "keyboard shortcut off")

    # ---------- 5. text and points
    def send_typed(self):
        text = core.clean_text(self.text_var.get())
        if not text:
            return
        if not self.live:
            self.row("send", "failed", "no session selected: the text is still in the box")
            return
        self.text_var.set("")
        self.live.say(text, "typed")

    def _point_changed(self):
        if self.live:
            self.live.attach = self.point_var.get()

    def send_point(self):
        if not self.live:
            self.row("point", "failed", "no session selected")
            return
        was = self.live.attach
        self.live.attach = True
        try:
            self.live.point_only()
        finally:
            self.live.attach = was

    # ---------- 7. report
    def report_text(self):
        d = self.mic_device()
        info = {
            "version": core.VERSION, "os": platform.platform(terse=False), "python": platform.python_version(),
            "hub": f"{self.hub_var.get()} ({self.hub_url()})",
            "role": ("owner" if self.me.get("owner") else "member") if self.me else "",
            "session": self.live.session if self.live else "",
            "mic": d["label"] if d else self.mic_var.get(),
            "model": f"{self.model_name}{' · ' + self.model_source if self.model_source else ''}",
            "model_status": self.model_status,
            "settings": {"keep_listening": self.keep_var.get(), "attach_point": self.point_var.get(),
                         "shortcut_ctrl_alt_j": self.hotkey_var.get(), "speech_level": self.threshold,
                         "voice_packages": not bool(lc.AUDIO_MISSING), "recording_now": self.recording},
        }
        return core.build_report(info, self.rows)

    def copy_report(self):
        text = self.report_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.row("report", "ok", f"copied to the clipboard ({min(len(self.rows), core.REPORT_ROWS)} rows)")

    def send_report(self):
        if not (self.hub and self.live):
            self.row("report", "failed", "sign in and pick a session first (or use Copy report)")
            return
        text = self.report_text()
        hub, me, sid = self.hub, self.me, self.live.session

        def work():
            try:
                hub.post(core.report_event(sid, me, text, core.utcnow()))
            except Exception as e:
                self.later(self.row, "report", "failed", f"hub {getattr(e, 'code', 0) or 'unreachable'} · {e}")
                return
            self.later(self.row, "report", "ok", f"sent to {sid} as a comment · hub {hub.last_status}")
        self.bg(work)

    # ---------- close
    def _close(self):
        if self.recorder:
            self.recorder.stop()
        if self.hotkeys:
            self.hotkeys.stop()
        live = self.live
        if live:
            live.close()
            deadline = time.monotonic() + 5

            def wait():
                if live.out.unfinished_tasks and time.monotonic() < deadline:
                    self.root.after(100, wait)
                else:
                    self.root.destroy()
            wait()
        else:
            self.root.destroy()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nodach Live window (click to talk; audio stays on this PC).")
    ap.add_argument("--hub", default="staging", choices=list(core.HUBS), help="staging or production")
    ap.add_argument("--model", default=os.environ.get("NODACH_LIVE_MODEL", "base"), help="tiny, base or small")
    a = ap.parse_args(argv)
    try:
        root = tk.Tk()
        App(root, a.hub, a.model)
        root.mainloop()
        return 0
    except Exception:
        # pythonw has no console: keep the error where the report can point to it
        lc.HOME.mkdir(parents=True, exist_ok=True)
        (lc.HOME / "gui-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror("Nodach Live", "The window couldn't start. Details are in nodach-live\\gui-error.txt.")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
