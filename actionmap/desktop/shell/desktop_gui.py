"""
Nodach Desktop window: one click-only window for Gate, Pointer and Live, with Update / Roll back / Copy report.
Claude desktop opens it when it starts nodach-desktop (and the Desktop shortcut opens it any time).
Gate's own Allow/Block popup stays separate; Gate's detailed log window opens from the 'Gate log' button.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop_core as core  # noqa: E402
import updater               # noqa: E402

import tkinter as tk         # noqa: E402
from tkinter import ttk      # noqa: E402

GREEN, RED, GREY, AMBER = "#2a7", "#c33", "#777", "#c80"
ORANGE, IDLE_BORDER = "#f60", "#ddd"


def _open_folder(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(p))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(p)])


class DesktopWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.app, self.home = core.APP, core.ROOT
        core.write_json(self.home / "run" / "desktop-window.pid", {"pid": os.getpid(), "started": core.now_iso()})
        self.events: list[str] = []
        self.busy = False
        root.title("Nodach Desktop")
        root.geometry("640x660")
        pad = {"padx": 8, "pady": 4}
        # the orange border + banner replace Windows-MCP's own 'AI is in control' overlay (off since Gate 0.1.5)
        self.border = tk.Frame(root, bg=IDLE_BORDER, padx=6, pady=6)
        self.border.pack(fill="both", expand=True)
        body = tk.Frame(self.border)
        body.pack(fill="both", expand=True)
        self.banner = tk.Label(body, font=("Segoe UI", 12, "bold"), anchor="w", padx=10, pady=6)
        self.banner.pack(fill="x")
        self.stop_btn = tk.Button(body, text="STOP CLAUDE NOW", font=("Segoe UI", 16, "bold"), bg=RED, fg="white",
                                  activebackground="#a22", activeforeground="white", command=self.stop_claude, height=1)
        self.stop_btn.pack(fill="x", padx=8, pady=(4, 0))
        self.was_acting = False

        head = ttk.Frame(body, padding=8)
        head.pack(fill="x")
        self.title = ttk.Label(head, font=("Segoe UI", 14, "bold"))
        self.title.pack(anchor="w")
        self.sub = ttk.Label(head, foreground=GREY)
        self.sub.pack(anchor="w")
        self.warn = tk.Label(head, fg=AMBER, anchor="w", justify="left", wraplength=580)
        self.warn.pack(fill="x")

        mods = ttk.LabelFrame(body, text="Modules", padding=8)
        mods.pack(fill="x", **pad)
        self.gate_lbl = tk.Label(mods, width=34, anchor="w")
        self.gate_lbl.grid(row=0, column=0, sticky="w")
        self.gate_btn = ttk.Button(mods, text="Pause Gate", command=self.toggle_gate, width=14)
        self.gate_btn.grid(row=0, column=1, **pad)
        ttk.Button(mods, text="Gate log", command=self.open_gate_log, width=12).grid(row=0, column=2, **pad)
        self.ptr_lbl = tk.Label(mods, width=34, anchor="w")
        self.ptr_lbl.grid(row=1, column=0, sticky="w")
        ttk.Button(mods, text="Start Pointer", command=self.start_pointer, width=14).grid(row=1, column=1, **pad)
        ttk.Button(mods, text="Stop Pointer", command=self.stop_pointer, width=12).grid(row=1, column=2, **pad)
        self.live_lbl = tk.Label(mods, width=34, anchor="w")
        self.live_lbl.grid(row=2, column=0, sticky="w")
        ttk.Button(mods, text="Open Live", command=self.open_live, width=14).grid(row=2, column=1, **pad)

        upd = ttk.LabelFrame(body, text="Versions and updates", padding=8)
        upd.pack(fill="x", **pad)
        self.ver_lbl = ttk.Label(upd, justify="left")
        self.ver_lbl.grid(row=0, column=0, columnspan=4, sticky="w")
        self.upd_btn = ttk.Button(upd, text="Update", command=self.update, width=12)
        self.upd_btn.grid(row=1, column=0, **pad)
        self.rb_btn = ttk.Button(upd, text="Roll back", command=self.rollback, width=12)
        self.rb_btn.grid(row=1, column=1, **pad)
        ttk.Button(upd, text="Reload tools", command=self.reload, width=12).grid(row=1, column=2, **pad)
        self.upd_lbl = tk.Label(upd, anchor="w", justify="left", wraplength=560, fg=GREY)
        self.upd_lbl.grid(row=2, column=0, columnspan=4, sticky="w")

        bottom = ttk.Frame(body, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Copy report", command=self.copy_report).pack(side="left")
        ttk.Button(bottom, text="Open folder", command=lambda: _open_folder(self.home)).pack(side="left", padx=8)
        self.copied = ttk.Label(bottom, foreground=GREEN)
        self.copied.pack(side="left")
        self.box = tk.Listbox(body, height=8)
        self.box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.note("window opened")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()
        self.fast_tick()

    # ------------------------------------------------------------ helpers
    def note(self, text: str):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        self.events.append(line)
        self.box.insert("end", line)
        self.box.see("end")

    def run_bg(self, label, fn, done):
        if self.busy:
            return
        self.busy = True
        self.upd_lbl.config(text=label, fg=GREY)

        def go():
            try:
                r = fn()
            except Exception as e:  # never leave the window stuck
                r = {"status": "failed", "message": f"{type(e).__name__}: {e}"}
            self.root.after(0, lambda: (setattr(self, "busy", False), done(r)))
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ periodic refresh
    def tick(self):
        try:
            self.refresh()
        except Exception:
            pass
        self.root.after(2000, self.tick)

    def fast_tick(self):
        try:
            self.show_acting()
        except Exception:
            pass
        self.root.after(300, self.fast_tick)

    def show_acting(self):
        a = core.acting_state(self.home)
        paused = core.gate_paused()
        if a["active"] and not paused:
            self.border.config(bg=ORANGE)
            self.banner.config(text=f"Claude is acting on this PC: {a.get('tool')}", bg=ORANGE, fg="white")
            if not self.was_acting:          # stay visible over other windows while Claude acts (no focus taken)
                self.root.attributes("-topmost", True)
        elif paused:
            self.border.config(bg=RED)
            self.banner.config(text="Gate is paused: Claude can't act on this PC", bg=RED, fg="white")
        else:
            self.border.config(bg=IDLE_BORDER)
            self.banner.config(text="Claude isn't acting on this PC", bg=self.root.cget("bg"), fg=GREY)
        if self.was_acting and not a["active"]:
            self.root.attributes("-topmost", False)
        self.was_acting = bool(a["active"])
        self.stop_btn.config(text="STOP CLAUDE NOW" if not paused else "Stopped (Gate paused): click Resume Gate to allow again",
                             state="normal")

    def stop_claude(self):
        """Same as Gate's Pause all: every next action is refused until Resume. Always clickable."""
        if not core.gate_paused():
            core.set_gate_paused(True)
            self.note("STOP pressed: Gate paused, Claude's next actions are refused")
        self.refresh()
        self.show_acting()

    def refresh(self):
        v = core.versions(self.app)
        st = core.shell_status(self.home)
        w = st.get("worker") or {}
        self.title.config(text=f"Nodach Desktop {v['desktop']}")
        if st.get("shell_alive"):
            state = w.get("state", "?")
            self.sub.config(text=f"Connected to Claude · worker {state} · up {int(w.get('uptime_s') or 0)} s · restarts {w.get('restarts', 0)}")
        else:
            self.sub.config(text="Claude isn't connected (open Claude desktop; it starts Nodach Desktop)")
        warn = []
        if st.get("shell_restart_needed"):
            warn.append(st["shell_restart_needed"].capitalize() + ".")
        errs = st.get("errors") or []
        if errs and w.get("state") != "ready":
            warn.append("Last error: " + errs[-1].get("error", ""))
        self.warn.config(text="  ".join(warn))
        paused = core.gate_paused()
        ws = core.read_json(self.home / "run" / "worker-status.json", {})
        wmcp = (ws.get("gate") or {}).get("windows_mcp_connected")
        if paused:
            self.gate_lbl.config(text="Gate: PAUSED (every action blocked)", fg=RED)
            self.gate_btn.config(text="Resume Gate")
        else:
            self.gate_lbl.config(text="Gate: on" + ("" if wmcp or not ws else " (Windows-MCP not connected)"),
                                 fg=GREEN if wmcp or not ws else AMBER)
            self.gate_btn.config(text="Pause Gate")
        on = core.running("pointer.pid", self.home)
        self.ptr_lbl.config(text="Pointer capture: on (Ctrl+Alt+M to mark)" if on else "Pointer capture: off", fg=GREEN if on else GREY)
        lv = core.running("live.pid", self.home)
        self.live_lbl.config(text="Live: window open" if lv else "Live: closed", fg=GREEN if lv else GREY)
        self.ver_lbl.config(text=f"shell {v['shell']} · worker {v['worker']} · gate {v['gate']} · pointer {v['pointer']} · "
                                 f"live {v['live']}\nprevious version kept: {v['previous'] or 'none'}")
        self.rb_btn.state(["!disabled"] if v["previous"] else ["disabled"])

    # ------------------------------------------------------------ buttons
    def toggle_gate(self):
        p = not core.gate_paused()
        core.set_gate_paused(p)
        self.note("Gate paused" if p else "Gate resumed")
        self.refresh()

    def open_gate_log(self):
        core.launch(self.app / "gate" / "gate_gui.py", "gate-window.pid", self.home)
        self.note("opened the Gate log window")

    def start_pointer(self):
        if core.running("pointer.pid", self.home):
            self.note("Pointer is already on")
            return
        core.launch(self.app / "pointer" / "pointer.py", "pointer.pid", self.home, log_name="pointer-app.log")
        self.note("Pointer started (Ctrl+Alt+M marks the element under the mouse)")
        self.root.after(1500, self.refresh)

    def stop_pointer(self):
        self.note("Pointer stopped" if core.stop("pointer.pid", self.home) else "Pointer wasn't running")
        self.root.after(800, self.refresh)

    def open_live(self):
        if core.running("live.pid", self.home):
            self.note("Live is already open")
            return
        core.launch(self.app / "live" / "live_gui.py", "live.pid", self.home, log_name="live-app.log")
        self.note("Live opened")

    def update(self):
        def done(r):
            colour = {"updated": GREEN, "up_to_date": GREEN, "failed": RED}.get(r.get("status"), GREY)
            self.upd_lbl.config(text=r.get("message") or r.get("status"), fg=colour)
            self.note(r.get("message") or r.get("status"))
            self.refresh()
        self.run_bg("Checking for updates...", lambda: updater.update(self.home, self.app,
                    progress=lambda s: self.root.after(0, lambda: self.upd_lbl.config(text=s))), done)

    def rollback(self):
        def done(r):
            self.upd_lbl.config(text=r["message"], fg=GREEN if r["status"] == "rolled_back" else RED)
            self.note(r["message"])
            self.refresh()
        self.run_bg("Rolling back...", lambda: updater.do_rollback(self.home, self.app), done)

    def reload(self):
        core.request_reload(self.home)
        self.note("asked Claude's connection to reload the tools")

    def copy_report(self):
        text = core.build_report(self.app, self.home, self.events)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.copied.config(text="Report copied: paste it into the chat")
        self.note("report copied")
        self.root.after(5000, lambda: self.copied.config(text=""))

    def close(self):
        try:
            (self.home / "run" / "desktop-window.pid").unlink()
        except OSError:
            pass
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
        DesktopWindow(root)
        root.mainloop()
        return 0
    except Exception:
        core.ROOT.mkdir(parents=True, exist_ok=True)
        (core.ROOT / "logs").mkdir(exist_ok=True)
        (core.ROOT / "logs" / "window-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
