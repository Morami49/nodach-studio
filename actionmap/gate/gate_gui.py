"""
Nodach Gate window: what Claude is doing on this PC, live, with the big Pause all switch.

Opened by start-gate.bat, or by the Gate server when Claude desktop starts it (zones.json gui.autostart).
Everything here is a button, toggle or checkbox. It talks to the server only through files in
%USERPROFILE%\\nodach-gate: it reads actions.jsonl and writes state.json (pause, toggles, 'Keep last 10 s',
automatic-report checkboxes), so it works even if the server restarts.

The window title starts with "Nodach Gate": Gate refuses any Claude action aimed at it.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_core as core      # noqa: E402
import gate_studio as studio  # noqa: E402

HOME = studio.HOME
LOG = HOME / "actions.jsonl"
STATE = HOME / "state.json"
COLOURS = {"never": "#f8d0d0", "confirm": "#fde9b8", "auto": "#dff3df", "hidden": "#e4e4e4", "invalid": "#e4e4e4",
           "clip": "#d6e6fb", "report": "#ece0f8", "view": "#d6e6fb", "start": "#ffffff", "error": "#f8d0d0"}
REPORT_LABELS = [
    ("never_block", "On a never-zone block (text only, never pictures)"),
    ("error_or_glitch", "On an action error or a saved glitch clip"),
    ("repeat_fail", "When the same action fails twice in a row"),
    ("confirm_timeout", "When a confirm is blocked or times out"),
    ("run_end", "End of a Claude run (5 min with no action): summary"),
    ("session_summary", "Session summary when Gate closes or after 30 min idle"),
    ("daily_summary", "Daily summary at 21:00"),
]


def read_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _clipboard(root) -> str:
    try:
        return root.clipboard_get()
    except Exception:
        return ""


class GateWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        HOME.mkdir(parents=True, exist_ok=True)
        write_json(HOME / "gui.pid", {"pid": os.getpid(), "started": core.iso(time.time())})
        self.zones = core.load_zones(HOME / "zones.json")
        self.st = read_json(STATE, {})
        cap = core.capture_settings(self.zones, self.st)
        rs = core.auto_report_settings(self.zones, self.st)
        self.pos = 0
        self.entries = []
        root.title("Nodach Gate")
        root.geometry("1060x780")
        root.attributes("-topmost", False)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        self.pause_btn = tk.Button(top, font=("Segoe UI", 14, "bold"), width=22, command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        self.shots = tk.BooleanVar(value=cap.get("screenshots", True))
        self.clips = tk.BooleanVar(value=cap.get("clips", True))
        ttk.Checkbutton(top, text="Screenshots", variable=self.shots, command=self.save_state).pack(side="left", padx=12)
        ttk.Checkbutton(top, text="Clips", variable=self.clips, command=self.save_state).pack(side="left")
        ttk.Button(top, text="Keep last 10 s", command=self.keep_clip).pack(side="left", padx=12)
        ttk.Button(top, text="Open log folder", command=lambda: _open(HOME)).pack(side="right")
        self.status = ttk.Label(root, padding=(8, 0), foreground="#555")
        self.status.pack(fill="x")

        cols = ("time", "app", "tool", "zone", "decision", "by", "element", "note")
        widths = (70, 130, 90, 70, 80, 60, 200, 320)
        mid = ttk.Frame(root, padding=(8, 4))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=16)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        for z, colour in COLOURS.items():
            self.tree.tag_configure(z, background=colour)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        legend = ttk.Frame(root, padding=(8, 0))
        legend.pack(fill="x")
        for z, label in (("never", "never: refused"), ("confirm", "confirm: asked you"), ("auto", "auto"),
                         ("clip", "clip / view"), ("report", "report to Studio")):
            tk.Label(legend, text=" " + label + " ", bg=COLOURS[z]).pack(side="left", padx=2)

        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        s = ttk.LabelFrame(bottom, text="Studio", padding=6)
        s.pack(side="left", fill="y")
        self.hub = tk.StringVar(value=rs.get("hub", "staging"))
        r1 = ttk.Frame(s)
        r1.pack(fill="x")
        ttk.Label(r1, text="Hub").pack(side="left")
        ttk.Combobox(r1, textvariable=self.hub, values=list(studio.HUBS), width=11, state="readonly").pack(side="left", padx=4)
        self.hub.trace_add("write", lambda *a: (self.save_state(), self.refresh_sign_in()))
        ttk.Button(r1, text="Open Studio", command=lambda: webbrowser.open(studio.hub_url(self.hub.get()) + "/studio")).pack(side="left")
        ttk.Button(r1, text="Sign in", command=self.sign_in).pack(side="left", padx=4)
        ttk.Button(r1, text="Sign out", command=self.sign_out).pack(side="left")
        self.signin = ttk.Label(s, foreground="#555")
        self.signin.pack(anchor="w", pady=2)
        r2 = ttk.Frame(s)
        r2.pack(fill="x", pady=4)
        ttk.Button(r2, text="Copy report", command=self.copy_report).pack(side="left")
        ttk.Button(r2, text="Send report to Studio", command=self.send_report).pack(side="left", padx=4)
        self.pics = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="include pictures", variable=self.pics).pack(side="left")
        r3 = ttk.Frame(s)
        r3.pack(fill="x")
        self.outbox = ttk.Label(r3, foreground="#555")
        self.outbox.pack(side="left")
        ttk.Button(r3, text="Send queued now", command=self.flush).pack(side="left", padx=4)
        ttk.Label(s, foreground="#777", wraplength=360, justify="left",
                  text="Reports hold counts, zones, apps and element names: never typed text, window titles or sign-in. "
                       "Pictures go only to your encrypted Private tier (never the shared record), and only when ticked.").pack(anchor="w")

        a = ttk.LabelFrame(bottom, text="Automatic reports", padding=6)
        a.pack(side="left", fill="both", expand=True, padx=8)
        self.auto_on = tk.BooleanVar(value=rs.get("send_automatically", True))
        ttk.Checkbutton(a, text="Send automatically (master switch)", variable=self.auto_on, command=self.save_state).pack(anchor="w")
        self.auto_vars = {}
        for k, label in REPORT_LABELS:
            v = tk.BooleanVar(value=rs.get(k, k != "daily_summary"))
            self.auto_vars[k] = v
            ttk.Checkbutton(a, text=label, variable=v, command=self.save_state).pack(anchor="w", padx=14)
        self.auto_pics = tk.BooleanVar(value=rs.get("attach_pictures", True))
        ttk.Checkbutton(a, text="Attach pictures to error, repeat, confirm and glitch reports (Private tier only)",
                        variable=self.auto_pics, command=self.save_state).pack(anchor="w", padx=14)
        ttk.Label(a, foreground="#777", wraplength=520, justify="left", text=f"Limits: {rs.get('per_hour', 6)} an hour, {rs.get('min_gap_s', 300) // 60} min apart "
                  f"(blocks excepted), {rs.get('per_day', 20)} a day. Offline or signed out: queued and sent later.").pack(anchor="w")
        self.render_pause()
        self.refresh_sign_in()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    # ------------------------------------------------------------ state
    def save_state(self):
        st = read_json(STATE, {})
        st.update({"screenshots": self.shots.get(), "clips": self.clips.get(), "changed": core.iso(time.time()),
                   "auto_reports": {"send_automatically": self.auto_on.get(), "attach_pictures": self.auto_pics.get(),
                                    "hub": self.hub.get(), **{k: v.get() for k, v in self.auto_vars.items()}}})
        write_json(STATE, st)
        self.st = st

    def toggle_pause(self):
        st = read_json(STATE, {})
        st["paused"] = not st.get("paused", False)
        st["paused_changed"] = core.iso(time.time())
        write_json(STATE, st)
        self.st = st
        self._note("pause", "Pause all " + ("on: every action is blocked" if st["paused"] else "off"))
        self.render_pause()

    def render_pause(self):
        if self.st.get("paused"):
            self.pause_btn.config(text="PAUSED: click to resume", bg="#c33", fg="white", activebackground="#a22")
        else:
            self.pause_btn.config(text="Pause all", bg="#2e7d32", fg="white", activebackground="#1b5e20")

    def keep_clip(self):
        st = read_json(STATE, {})
        st["keep_clip_req"] = int(st.get("keep_clip_req", 0)) + 1
        write_json(STATE, st)
        self._note("gui", "Keep last 10 s asked (saved by the Gate server if it is running)")

    def _note(self, kind, text):
        e = core.note_entry(time.time(), "gui", detail=text, what=kind)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ list
    def tick(self):
        try:
            self.read_new()
            n = len(studio.pending())
            self.outbox.config(text=f"queued reports: {n}")
            acts = [e for e in self.entries if e.get("kind", "action") == "action"]
            self.status.config(text=f"{len(acts)} actions this window · log {LOG}")
        except Exception as e:
            self.status.config(text=f"couldn't read the log: {type(e).__name__}")
        self.root.after(500, self.tick)

    def read_new(self):
        if not LOG.exists():
            return
        with open(LOG, "r", encoding="utf-8") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        for line in data.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            self.entries.append(e)
            self.add_row(e)
        self.entries = self.entries[-2000:]

    def add_row(self, e):
        kind = e.get("kind", "action")
        t = (e.get("ts") or "")[11:19]
        if kind == "action":
            el = e.get("element") or {}
            note = e.get("reason", "") if e.get("zone") != "auto" else ""
            if not e.get("ok", True) and e.get("error"):
                note = (note + " · " if note else "") + "error: " + e["error"]
            if e.get("shot"):
                note += f"  [shot {Path(e['shot']).name}]"
            vals = (t, e.get("app", ""), e.get("tool", ""), e.get("zone", ""), e.get("decision", ""), e.get("by", ""),
                    f"{el.get('type', '')} {el.get('name', '')}".strip(), note)
            tag = e.get("zone", "auto")
        elif kind == "clip":
            vals = (t, "", "[clip]", e.get("trigger", ""), "", "", f"{e.get('frames', 0)} frames", e.get("path") or e.get("note", ""))
            tag = "clip"
        elif kind == "report":
            vals = (t, "", "[report]", e.get("trigger", ""), e.get("status", ""), "", e.get("item", ""),
                    (("auto-report " if e.get("trigger") != "manual" else "report ") + f"{e.get('status', '')} {e.get('detail', '')}").strip())
            tag = "report"
        elif kind == "view":
            vals = (t, e.get("app", ""), "[view]", "", "", "claude", "", (e.get("note", "") + " " + (e.get("shot") or e.get("reason", ""))))
            tag = "view"
        elif kind == "start":
            vals = (t, "", "[start]", "", "", "", f"Gate {e.get('version')} · windows-mcp {e.get('windows_mcp')}",
                    "hidden: " + ", ".join(e.get("hidden", [])))
            tag = "start"
        else:
            vals = (t, "", f"[{kind}]", "", "", "", "", e.get("detail", ""))
            tag = "error" if kind == "error" else "start"
        self.tree.insert("", "end", values=vals, tags=(tag,))
        kids = self.tree.get_children()
        if len(kids) > 1500:
            self.tree.delete(*kids[:200])
        self.tree.yview_moveto(1.0)

    # ------------------------------------------------------------ reports
    def report_text(self, trigger="manual"):
        cap = core.capture_settings(self.zones, read_json(STATE, {}))
        info = {"version": core.VERSION, "at": core.iso(time.time()), "os": f"{platform.system()} {platform.release()}",
                "python": platform.python_version(), "windows_mcp": _wmcp_version(),
                "zones_rev": f"{self.zones.get('revision', 0)}-{self.zones.get('_sha', '')}",
                "paused": bool(read_json(STATE, {}).get("paused")), "screenshots": cap.get("screenshots", True),
                "clips": cap.get("clips", True), "trigger": trigger}
        return core.build_report(info, self.entries)

    def copy_report(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.report_text())
        self.status.config(text="Report copied: paste it into the chat.")

    def send_report(self):
        files = []
        if self.pics.get():
            for e in reversed(self.entries):
                p = e.get("shot") or (e.get("path") if e.get("kind") == "clip" else None)
                if p and (HOME / p).exists():
                    files.append(str(HOME / p))
                if len(files) >= 5:
                    break
        p = studio.queue_report("manual", self.report_text(), files, self.hub.get())
        self._note("gui", f"manual report queued ({len(files)} pictures)")
        self.flush()
        return p

    def flush(self):
        def work():
            try:
                for r in studio.flush(core.auto_report_settings(self.zones, read_json(STATE, {})).get("attach_max_mb", 10)):
                    e = core.note_entry(time.time(), "report", status=r["status"], trigger=r["kind"], file=r["file"],
                                        item=r.get("item"), detail=r.get("detail", ""))
                    with open(LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
            except Exception as ex:
                msg = f"send failed: {type(ex).__name__}"
                self.root.after(0, lambda: self.status.config(text=msg))
            self.root.after(0, self.refresh_sign_in)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ sign-in
    def sign_in(self):
        t = core.clean_token(_clipboard(self.root))
        if not t:
            self.signin.config(text="Clipboard doesn't hold a nodach_session value: copy it from DevTools (see README), then click Sign in.")
            return
        url = studio.hub_url(self.hub.get())
        studio.save_token(url, t)
        self.root.clipboard_clear()        # don't leave the sign-in on the clipboard
        self.refresh_sign_in()
        self.flush()

    def sign_out(self):
        studio.forget_token(studio.hub_url(self.hub.get()))
        self.refresh_sign_in()

    def refresh_sign_in(self):
        url = studio.hub_url(self.hub.get())
        tok = studio.stored_token(url)
        if not tok:
            self.signin.config(text="Not signed in: reports are queued until you sign in.")
            return

        def work():
            try:
                me = studio.Hub(url, tok).me()
                msg = "Signed in as owner" if me.get("owner") else "Signed in, but not as the owner: reports stay queued (owner only)."
            except studio.HubError as e:
                msg = f"Sign-in not accepted ({e}); sign in again." if e.code == 401 else f"Hub not reachable now ({e}); reports wait in the outbox."
            self.root.after(0, lambda: self.signin.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def close(self):
        try:
            (HOME / "gui.pid").unlink()
        except OSError:
            pass
        self.root.destroy()


def _wmcp_version():
    try:
        from importlib.metadata import version
        return version("windows-mcp")
    except Exception:
        return "?"


def _open(p):
    try:
        os.startfile(str(p))          # Windows
    except Exception:
        webbrowser.open(Path(p).as_uri())


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    if not (HOME / "zones.json").exists() and core.DEFAULT_ZONES.exists():
        (HOME / "zones.json").write_bytes(core.DEFAULT_ZONES.read_bytes())
    root = tk.Tk()
    GateWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
