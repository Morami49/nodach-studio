"""
Nodach Desktop shell: the one MCP entry Claude desktop starts ("nodach-desktop"). Standard library only.

    Claude desktop --stdio--> shell_mcp.py --stdio--> worker.py (Gate + Pointer tools; the code that changes)

The shell is kept small and dependency-free so it rarely changes; the worker carries the real work and is replaced
without restarting Claude:
- It watches app\\VERSION and the files under app\\ (not _next/_prev). When they change (or Claude calls
  desktop_reload, or the Desktop window asks), it lets calls already running finish, restarts the worker, and sends
  notifications/tools/list_changed so Claude reads the new tool list.
- If the worker crashes it is restarted with backoff; meanwhile tool calls get a clear error instead of hanging.
- desktop_status: versions of shell / gate / pointer / live, worker uptime, last reload, recent errors. If
  shell_mcp.py itself changed on disk, it says so ('restart Claude once to load the new shell').
Every event is appended to %USERPROFILE%\\nodach\\logs\\shell.jsonl (never rewritten).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SHELL_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("NODACH_DESKTOP_APP") or HERE.parent)
ROOT = Path(os.environ.get("NODACH_DESKTOP_HOME") or (Path.home() / "nodach"))
SKIP_DIRS = {"_next", "_prev", "_rollback", "_old", "__pycache__", ".git"}
SWAP_LOCK = ".swapping"
DESKTOP_TOOLS = [
    {"name": "desktop_status", "description": "Nodach Desktop: versions (shell, gate, pointer, live), worker state and uptime, "
     "last reload, recent errors, and whether Claude needs one restart to load a new shell.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "desktop_reload", "description": "Nodach Desktop: restart the worker so new files under %USERPROFILE%\\nodach\\app "
     "take effect (call after writing them). Calls already running finish first; the tool list is refreshed.",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
]
DESKTOP_NAMES = {t["name"] for t in DESKTOP_TOOLS}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def file_sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def module_version(path: Path) -> str:
    m = re.search(r'^(?:VERSION|SHELL_VERSION|WORKER_VERSION)\s*=\s*"([^"]+)"', read_text(path), re.M)
    return m.group(1) if m else "?"


def versions(app: Path) -> dict:
    return {"desktop": read_text(app / "VERSION") or "?",
            "shell_on_disk": module_version(app / "shell" / "shell_mcp.py"),
            "worker": module_version(app / "shell" / "worker.py"),
            "gate": module_version(app / "gate" / "gate_core.py"),
            "pointer": module_version(app / "pointer" / "pointer_core.py"),
            "live": module_version(app / "live" / "live_core.py")}


def app_signature(app: Path) -> tuple:
    """What a reload depends on: VERSION's text and every file's size and mtime under app (not _next/_prev)."""
    files = []
    for dirpath, dirnames, filenames in os.walk(app):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f.endswith((".pyc", ".tmp")) or f == SWAP_LOCK:
                continue
            p = Path(dirpath) / f
            try:
                s = p.stat()
            except OSError:
                continue
            files.append((p.relative_to(app).as_posix(), s.st_size, s.st_mtime_ns))
    return (read_text(app / "VERSION"), tuple(files))


def backoff_steps() -> list[float]:
    raw = os.environ.get("NODACH_DESKTOP_BACKOFF")
    try:
        return [float(x) for x in raw.split(",")] if raw else [1, 2, 5, 10, 30, 60]
    except ValueError:
        return [1, 2, 5, 10, 30, 60]


class Shell:
    def __init__(self, app: Path = APP, root: Path = ROOT, worker_cmd=None, out=None, poll_s: float = 1.0,
                 drain_s: float = 150.0, init_s: float = 45.0):
        self.app, self.root = Path(app), Path(root)
        env_cmd = os.environ.get("NODACH_DESKTOP_WORKER_CMD")
        self.worker_cmd = worker_cmd or (json.loads(env_cmd) if env_cmd else [sys.executable, "-u", str(self.app / "shell" / "worker.py")])
        self.out = out or sys.stdout.buffer
        self.poll_s, self.drain_s, self.init_s = poll_s, drain_s, init_s
        self.out_lock = threading.Lock()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.proc = None
        self.gen = 0
        self.state = "stopped"          # stopped | starting | ready | draining | down
        self.started_t = None
        self.next_id = 1
        self.pending = {}               # shell id -> (client id, method, patch)
        self.queued = []                # client messages held while draining
        self.init_params = None
        self.init_result = None
        self.client_initialized = False
        self.init_waiter = {}
        self.cached_tools = []
        self.restarts = 0
        self.backoff_i = 0
        self.errors = []
        self.last_reload = None
        self.reload_lock = threading.Lock()
        self.closing = False
        self.restarting = False
        self.next_try = None
        self.shell_sha = file_sha(Path(__file__).resolve())
        self.shell_path = self.app / "shell" / "shell_mcp.py"
        self.sig = app_signature(self.app)
        self.loaded_versions = versions(self.app)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / "run").mkdir(parents=True, exist_ok=True)
        self.reload_req = self.root / "run" / "reload-request"
        self.reload_req_seen = self._mtime(self.reload_req)

    # ------------------------------------------------------------ small helpers
    def log(self, kind: str, **fields):
        e = {"schema": "desktop.shell.v1", "ts": now_iso(), "kind": kind, "shell": SHELL_VERSION, **fields}
        try:
            with open(self.root / "logs" / "shell.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self.write_status()

    def error(self, text: str):
        self.errors = (self.errors + [{"at": now_iso(), "error": text[:400]}])[-10:]
        self.log("error", error=text[:400])

    def send(self, msg: dict):
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.out_lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def to_worker(self, msg: dict) -> bool:
        p = self.proc
        if not p or p.poll() is not None:
            return False
        try:
            p.stdin.write((json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            p.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _mtime(p: Path):
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return None

    def status(self) -> dict:
        with self.lock:
            up = round(time.time() - self.started_t, 1) if self.started_t and self.state in ("ready", "draining") else 0
            disk = versions(self.app)
            changed = file_sha(self.shell_path) not in ("", self.shell_sha)
            return {"shell_running": SHELL_VERSION, "versions_loaded": self.loaded_versions, "versions_on_disk": disk,
                    "worker": {"state": self.state, "pid": self.proc.pid if self.proc else None, "uptime_s": up,
                               "restarts": self.restarts, "calls_in_flight": len(self.pending)},
                    "last_reload": self.last_reload, "errors": self.errors[-5:],
                    "shell_restart_needed": ("shell_mcp.py changed on disk: restart Claude once to load the new shell"
                                             if changed else None),
                    "app": str(self.app)}

    def write_status(self):
        try:
            p = self.root / "run" / "shell-status.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(self.status(), pid=os.getpid(), at=now_iso()), indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            pass

    # ------------------------------------------------------------ worker process
    def start_worker(self) -> bool:
        """Start a worker and (if Claude has initialized) repeat the handshake with it. Returns True when ready."""
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.state = "starting"
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NODACH_DESKTOP_APP=str(self.app),
                       NODACH_DESKTOP_HOME=str(self.root))
            errlog = open(self.root / "logs" / "worker-stderr.log", "ab")
            errlog.write(f"\n--- worker start {now_iso()} gen {gen}\n".encode())
            errlog.flush()
            try:
                self.proc = subprocess.Popen(self.worker_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                             cwd=str(self.root), env=env,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                errlog.close()
                self.state = "down"
                self.error(f"worker couldn't start: {e}")
                return False
            errlog.close()
            self.started_t = time.time()
            threading.Thread(target=self.read_worker, args=(self.proc, gen), daemon=True).start()
        if self.init_params is not None and not self.handshake(gen):
            with self.lock:
                self.state = "down"
            return False
        with self.lock:
            if gen != self.gen or self.proc.poll() is not None:
                self.state = "down"
                return False
            self.state = "ready"
            self.loaded_versions = versions(self.app)
            self.cond.notify_all()
        self.log("worker_ready", gen=gen, pid=self.proc.pid, versions=self.loaded_versions)
        return True

    def handshake(self, gen) -> bool:
        ev = threading.Event()
        iid = f"shell-init-{gen}"
        self.init_waiter = {"id": iid, "event": ev, "result": None}
        if not self.to_worker({"jsonrpc": "2.0", "id": iid, "method": "initialize", "params": self.init_params}):
            return False
        if not ev.wait(self.init_s) or not self.init_waiter.get("result"):
            self.error(f"worker didn't answer initialize within {self.init_s:.0f} s")
            self.kill_worker()
            return False
        self.init_result = self.init_waiter["result"]
        self.to_worker({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def kill_worker(self, grace: float = 3.0):
        p = self.proc
        if not p:
            return
        with self.lock:
            self.gen += 1            # the old reader's EOF is then not a crash
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(grace)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass

    def read_worker(self, proc, gen):
        for line in iter(proc.stdout.readline, b""):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.on_worker_message(msg)
        self.on_worker_exit(proc, gen)

    def on_worker_message(self, msg: dict):
        if "method" in msg:
            if "id" in msg:         # the worker asking the client something: not used by Gate/Pointer
                self.to_worker({"jsonrpc": "2.0", "id": msg["id"], **({"result": {}} if msg["method"] == "ping" else
                                {"error": {"code": -32601, "message": "not supported by nodach-desktop"}})})
            else:
                self.send(msg)
            return
        mid = msg.get("id")
        w = self.init_waiter
        if w and mid == w.get("id"):
            w["result"] = msg.get("result")
            w["event"].set()
            return
        with self.lock:
            item = self.pending.pop(mid, None)
            self.cond.notify_all()
        if not item:
            return
        cid, method, patch = item
        out = dict(msg, id=cid)
        if patch == "tools" and "result" in out:
            tools = list((out["result"] or {}).get("tools") or [])
            self.cached_tools = tools
            out["result"] = dict(out["result"], tools=tools + DESKTOP_TOOLS)
        self.send(out)

    def on_worker_exit(self, proc, gen):
        try:
            code = proc.wait(5)
        except subprocess.TimeoutExpired:
            code = None
        with self.lock:
            crashed = gen == self.gen and not self.closing
            if not crashed:
                return
            ran = time.time() - (self.started_t or time.time())
            if ran > 60:
                self.backoff_i = 0
            self.state = "down"
            lost = list(self.pending.items())
            self.pending.clear()
            self.cond.notify_all()
        w = self.init_waiter
        if w and not w["event"].is_set():
            w["event"].set()
        self.error(f"worker stopped (exit code {code}) after {ran:.0f} s; restarting")
        for _, (cid, method, _) in lost:
            self.reply_error(cid, method, "Nodach Desktop's worker stopped while this call was running (it is restarting). "
                                          "Check the result on screen before trying again.")
        self.schedule_restart()

    def schedule_restart(self):
        with self.lock:
            if self.restarting or self.closing:
                return
            self.restarting = True
        threading.Thread(target=self.restart_with_backoff, daemon=True).start()

    def restart_with_backoff(self):
        try:
            self._restart_loop()
        finally:
            with self.lock:
                self.restarting, self.next_try = False, None

    def _restart_loop(self):
        steps = backoff_steps()
        while not self.closing:
            wait = steps[min(self.backoff_i, len(steps) - 1)]
            self.backoff_i += 1
            with self.lock:
                self.next_try = time.time() + wait
            time.sleep(wait)
            if self.closing:
                return
            with self.lock:
                if self.state not in ("down",):
                    return
                self.restarts += 1
            if self.start_worker():
                self.after_worker_change()
                return

    def after_worker_change(self):
        with self.lock:
            q, self.queued = self.queued, []
        for m in q:
            self.on_client_message(m)
        if self.client_initialized:
            self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    # ------------------------------------------------------------ reload
    def reload(self, reason: str) -> dict:
        with self.reload_lock:
            t0 = time.time()
            before = dict(self.loaded_versions)
            with self.lock:
                if self.state == "ready":
                    self.state = "draining"
                deadline = time.time() + self.drain_s
                while self.pending and time.time() < deadline:
                    self.cond.wait(0.2)
                left = list(self.pending.items())
                self.pending.clear()
            for _, (cid, method, _) in left:
                self.reply_error(cid, method, "Nodach Desktop reloaded while this call was still running; check the screen before retrying.")
            self.log("reload_start", reason=reason, waited_s=round(time.time() - t0, 1), cut_short=len(left))
            self.kill_worker()
            ok = self.start_worker()
            self.last_reload = {"at": now_iso(), "reason": reason, "ok": ok, "took_s": round(time.time() - t0, 1),
                                "from": before, "to": self.loaded_versions}
            self.log("reload", **self.last_reload)
            if ok:
                self.backoff_i = 0
                self.after_worker_change()
            else:
                with self.lock:
                    self.state = "down"
                self.schedule_restart()
            return self.last_reload

    def watch(self):
        pending_sig, stable = None, 0
        while not self.closing:
            time.sleep(self.poll_s)
            try:
                if (self.app / SWAP_LOCK).exists():
                    stable = 0
                    continue
                m = self._mtime(self.reload_req)
                if m != self.reload_req_seen:
                    self.reload_req_seen = m
                    self.sig = app_signature(self.app)
                    self.reload("asked from the Desktop window")
                    continue
                sig = app_signature(self.app)
                if sig == self.sig:
                    pending_sig, stable = None, 0
                    continue
                if sig != pending_sig:           # still being written: wait until it stops changing
                    pending_sig, stable = sig, 0
                    continue
                stable += 1
                if stable >= 1:
                    old_v, new_v = self.sig[0], sig[0]
                    changed = len(set(sig[1]) ^ set(self.sig[1]))
                    self.sig, pending_sig, stable = sig, None, 0
                    why = f"VERSION {old_v} -> {new_v}" if old_v != new_v else f"{changed} file change(s) under app"
                    if self.state != "stopped":
                        self.reload(why)
            except Exception as e:     # never let the watcher die
                self.error(f"watcher: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ Claude's side
    def reply_error(self, cid, method, text):
        if method == "tools/call":
            self.send({"jsonrpc": "2.0", "id": cid, "result": {"content": [{"type": "text", "text": text}], "isError": True}})
        else:
            self.send({"jsonrpc": "2.0", "id": cid, "error": {"code": -32000, "message": text}})

    def forward(self, msg: dict, patch=None) -> bool:
        with self.lock:
            sid = self.next_id
            self.next_id += 1
            self.pending[sid] = (msg["id"], msg.get("method"), patch)
        if self.to_worker(dict(msg, id=sid)):
            return True
        with self.lock:
            self.pending.pop(sid, None)
        return False

    def down_text(self) -> str:
        nt = self.next_try
        wait = f" next try in {max(0, nt - time.time()):.0f} s;" if nt else ""
        last = self.errors[-1]["error"] if self.errors else "unknown"
        return (f"Nodach Desktop's worker isn't running right now ({self.state};{wait} last error: {last}). "
                "It restarts by itself; call desktop_status to see why, then try again.")

    def on_client_message(self, msg: dict):
        method, has_id = msg.get("method"), "id" in msg
        if method is None:       # a reply to something the worker asked: not used
            return
        if not has_id:
            if method == "notifications/initialized":
                self.client_initialized = True
            elif method == "notifications/cancelled":
                params = msg.get("params") or {}
                with self.lock:
                    sid = next((s for s, (c, _, _) in self.pending.items() if c == params.get("requestId")), None)
                if sid is not None:
                    self.to_worker(dict(msg, params=dict(params, requestId=sid)))
            elif self.state == "ready":
                self.to_worker(msg)
            return
        if method == "initialize":
            threading.Thread(target=self.on_initialize, args=(msg,), daemon=True).start()
            return
        if method == "ping":
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            return
        name = (msg.get("params") or {}).get("name")
        if method == "tools/call" and name in DESKTOP_NAMES:
            threading.Thread(target=self.desktop_tool, args=(msg,), daemon=True).start()
            return
        with self.lock:
            state = self.state
            if state in ("draining", "starting"):
                self.queued.append(msg)
                return
            if state == "stopped":      # no initialize (stateless client): start the worker on first use
                self.queued.append(msg)
                threading.Thread(target=self._first_start, daemon=True).start()
                return
        if state == "ready":
            if method == "tools/list":
                if (msg.get("params") or {}).get("cursor"):
                    ok = self.forward(msg)
                else:
                    ok = self.forward(msg, patch="tools")
            else:
                ok = self.forward(msg)
            if ok:
                return
        if method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": self.cached_tools + DESKTOP_TOOLS}})
        else:
            self.reply_error(msg["id"], method, self.down_text())

    def _first_start(self):
        if self.start_worker():
            self.after_worker_change()
        else:
            self.schedule_restart()

    def on_initialize(self, msg: dict):
        params = msg.get("params") or {}
        self.init_params = params
        with self.lock:
            fresh = self.state == "stopped"
        if fresh:
            if not self.start_worker():
                self.schedule_restart()
            else:
                self.after_worker_change()
        r = self.init_result or {}
        caps = dict(r.get("capabilities") or {})
        caps["tools"] = dict(caps.get("tools") or {}, listChanged=True)
        note = ("Nodach Desktop serves these tools through a small shell that reloads new code without restarting Claude: "
                "after writing files under %USERPROFILE%\\nodach\\app, call desktop_reload; desktop_status shows versions and errors.")
        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": r.get("protocolVersion") or params.get("protocolVersion") or "2025-06-18",
            "capabilities": caps,
            "serverInfo": {"name": "nodach-desktop", "version": f"{SHELL_VERSION} (desktop {self.loaded_versions.get('desktop')})"},
            "instructions": ((r.get("instructions") or "") + "\n\n" + note).strip()}})
        self.log("client_initialize", client=(params.get("clientInfo") or {}).get("name"), protocol=params.get("protocolVersion"))

    def desktop_tool(self, msg: dict):
        params = msg.get("params") or {}
        if params.get("name") == "desktop_status":
            body = self.status()
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": json.dumps(body, indent=1)}], "isError": False}})
            return
        reason = str((params.get("arguments") or {}).get("reason") or "desktop_reload called")[:200]
        self.sig = app_signature(self.app)
        r = self.reload(reason)
        extra = self.status().get("shell_restart_needed")
        text = (f"Reloaded in {r['took_s']} s: worker {self.state}, versions {json.dumps(r['to'])}. The tool list was refreshed."
                if r["ok"] else f"Reload failed: {self.down_text()}") + (f" Note: {extra}." if extra else "")
        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}], "isError": not r["ok"]}})

    # ------------------------------------------------------------ main loop
    def run(self, inp=None):
        inp = inp or sys.stdin.buffer
        self.log("shell_start", pid=os.getpid(), versions=self.loaded_versions, app=str(self.app))
        threading.Thread(target=self.watch, daemon=True).start()
        start_window_if_wanted(self.app, self.root)
        for line in iter(inp.readline, b""):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                if isinstance(m, dict):
                    self.on_client_message(m)
        self.closing = True
        self.log("shell_stop")
        self.kill_worker()


def start_window_if_wanted(app: Path, root: Path):
    """Open the Desktop window when Claude starts us (Windows; off with desktop.json "autostart_window": false)."""
    if os.name != "nt" or os.environ.get("NODACH_DESKTOP_NO_WINDOW"):
        return
    try:
        cfg = json.loads(read_text(root / "desktop.json") or "{}")
    except ValueError:
        cfg = {}
    if cfg.get("autostart_window", True) is False:
        return
    try:
        pid = json.loads(read_text(root / "run" / "desktop-window.pid") or "{}").get("pid")
    except ValueError:
        pid = None
    if pid and _pid_alive(int(pid)):
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    gui = app / "shell" / "desktop_gui.py"
    if gui.exists():
        try:
            subprocess.Popen([str(pyw if pyw.exists() else exe), str(gui)], cwd=str(root), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                       # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    Shell().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
