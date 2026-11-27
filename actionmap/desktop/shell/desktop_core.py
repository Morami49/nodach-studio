"""
Nodach Desktop: the pure parts the window uses (paths, versions, module state, starting/stopping helpers, the
'Copy report' text). Standard library only; tested on Linux (tests/test_desktop.py).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("NODACH_DESKTOP_APP") or HERE.parent)
ROOT = Path(os.environ.get("NODACH_DESKTOP_HOME") or (Path.home() / "nodach"))
GATE_HOME = Path(os.environ.get("NODACH_GATE_HOME") or (Path.home() / "nodach-gate"))
POINTER_HOME = Path(os.environ.get("NODACH_POINTER_HOME") or (Path.home() / "nodach-pointer"))
LIVE_HOME = Path(os.environ.get("NODACH_LIVE_HOME") or (Path.home() / "nodach-live"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default if default is not None else {}


def write_json(p: Path, data) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def module_version(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return "?"
    m = re.search(r'^(?:VERSION|SHELL_VERSION|WORKER_VERSION)\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"


def versions(app: Path = APP) -> dict:
    app = Path(app)
    try:
        desk = (app / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        desk = "?"
    return {"desktop": desk, "shell": module_version(app / "shell" / "shell_mcp.py"),
            "worker": module_version(app / "shell" / "worker.py"), "gate": module_version(app / "gate" / "gate_core.py"),
            "pointer": module_version(app / "pointer" / "pointer_core.py"), "live": module_version(app / "live" / "live_core.py"),
            "previous": (lambda p: p.read_text(encoding="utf-8").strip() if p.exists() else None)(app / "_prev" / "VERSION")}


# ---------------------------------------------------------------- processes
def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def launch(script: Path, pid_name: str, root: Path = ROOT, log_name: str | None = None, popen=subprocess.Popen) -> int:
    """Start a module's window/app detached (no console), cwd = %USERPROFILE%\\nodach so app\\ can be swapped."""
    root = Path(root)
    (root / "run").mkdir(parents=True, exist_ok=True)
    out = subprocess.DEVNULL
    if log_name:
        (root / "logs").mkdir(parents=True, exist_ok=True)
        out = open(root / "logs" / log_name, "ab")
    try:
        p = popen([pythonw(), str(script)], cwd=str(root), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    finally:
        if out is not subprocess.DEVNULL:
            out.close()
    write_json(root / "run" / pid_name, {"pid": p.pid, "started": now_iso(), "script": Path(script).name})
    return p.pid


def stop(pid_name: str, root: Path = ROOT, run=subprocess.run) -> bool:
    info = read_json(Path(root) / "run" / pid_name, {})
    pid = info.get("pid")
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        import signal
        os.kill(int(pid), signal.SIGTERM)
    return True


def running(pid_name: str, root: Path = ROOT) -> bool:
    return pid_alive(read_json(Path(root) / "run" / pid_name, {}).get("pid"))


# ---------------------------------------------------------------- module state
def gate_paused(gate_home: Path = GATE_HOME) -> bool:
    return bool(read_json(Path(gate_home) / "state.json", {}).get("paused"))


def set_gate_paused(paused: bool, gate_home: Path = GATE_HOME) -> dict:
    """Same file and keys the Gate window uses (state.json: paused, paused_changed); other keys kept; logged."""
    gate_home = Path(gate_home)
    st = read_json(gate_home / "state.json", {})
    st["paused"] = bool(paused)
    st["paused_changed"] = now_iso()
    write_json(gate_home / "state.json", st)
    e = {"schema": "gate.v1", "kind": "gui", "ts": now_iso(), "session": "", "what": "pause",
         "detail": "Pause all " + ("on: every action is blocked" if paused else "off") + " (Nodach Desktop window)"}
    with open(gate_home / "actions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return st


def acting_state(root: Path = ROOT, linger_s: float = 1.5, now: float | None = None) -> dict:
    """Is Claude acting through Gate right now? From run\\acting.json (written by the worker around each Gate call).
    A call that ended under linger_s ago still shows, so a quick click is visible; a dead worker never shows as acting."""
    import time as _t
    a = read_json(Path(root) / "run" / "acting.json", {})
    now = now or _t.time()
    if not a or not pid_alive(a.get("pid")):
        return {"active": False, "tool": None}
    if a.get("active") or now - float(a.get("t") or 0) < linger_s:
        return {"active": True, "tool": a.get("tool"), "since": a.get("at")}
    return {"active": False, "tool": None}


def shell_status(root: Path = ROOT) -> dict:
    st = read_json(Path(root) / "run" / "shell-status.json", {})
    st["shell_alive"] = pid_alive(st.get("pid"))
    return st


def request_reload(root: Path = ROOT):
    p = Path(root) / "run" / "reload-request"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(now_iso(), encoding="utf-8")


def tail_jsonl(p: Path, n: int = 15) -> list[dict]:
    try:
        lines = Path(p).read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def claude_config_check(appdata: str, localappdata: str) -> list[dict]:
    """Which servers each Claude config has (names only, never values)."""
    out = []
    for p in config_paths(appdata, localappdata):
        servers = (read_json(p, {}) or {}).get("mcpServers") or {}
        out.append({"config": "msix" if "Packages" in str(p) else "appdata", "exists": p.exists(),
                    "servers": sorted(servers) if isinstance(servers, dict) else []})
    return out


MSIX_PKG = "Claude_pzs8sxrjxfjjc"


def config_paths(appdata: str, localappdata: str) -> list[Path]:
    out = [Path(appdata) / "Claude" / "claude_desktop_config.json"] if appdata else []
    pkg = Path(localappdata) / "Packages" / MSIX_PKG if localappdata else None
    if pkg and pkg.is_dir():
        out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    return out


def scrub(text: str) -> str:
    """Report text: home paths shortened, anything that looks like an email or a long token hidden."""
    home = str(Path.home())
    text = text.replace(home, "%USERPROFILE%").replace(home.replace("\\", "\\\\"), "%USERPROFILE%")
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text)
    return re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "[hidden]", text)


def build_report(app: Path = APP, root: Path = ROOT, events: list | None = None, env=os.environ) -> str:
    import platform
    v = versions(app)
    st = shell_status(root)
    ws = read_json(Path(root) / "run" / "worker-status.json", {})
    lines = [f"Nodach Desktop report · {now_iso()}",
             f"versions: desktop {v['desktop']} · shell {v['shell']} · worker {v['worker']} · gate {v['gate']} · "
             f"pointer {v['pointer']} · live {v['live']} · previous {v['previous'] or '-'}",
             f"os: {platform.system()} {platform.release()} · python {platform.python_version()}",
             f"shell: {'running' if st.get('shell_alive') else 'not running (Claude closed?)'} · worker "
             f"{(st.get('worker') or {}).get('state', '?')} · uptime {(st.get('worker') or {}).get('uptime_s', 0)} s · "
             f"restarts {(st.get('worker') or {}).get('restarts', 0)}"]
    if st.get("shell_restart_needed"):
        lines.append("NOTE: " + st["shell_restart_needed"])
    if st.get("last_reload"):
        lr = st["last_reload"]
        lines.append(f"last reload: {lr.get('at')} · {lr.get('reason')} · ok {lr.get('ok')} · {lr.get('took_s')} s")
    g = ws.get("gate") or {}
    lines.append(f"gate: {'paused' if gate_paused() else 'on'} · windows-mcp {'connected' if g.get('windows_mcp_connected') else 'NOT connected'}"
                 + (f" · {g['error']}" if g.get("error") else ""))
    p = ws.get("pointer") or {}
    lines.append(f"pointer: tools {'loaded' if p.get('loaded') else 'not loaded'} · capture app {'running' if running('pointer.pid', root) else 'off'}"
                 + (f" · {p['error']}" if p.get("error") else ""))
    lines.append(f"live: window {'open' if running('live.pid', root) else 'closed'}")
    for c in claude_config_check(env.get("APPDATA", ""), env.get("LOCALAPPDATA", "")):
        lines.append(f"claude config ({c['config']}): {'servers ' + ', '.join(c['servers']) if c['exists'] else 'missing'}")
    errs = st.get("errors") or []
    if errs:
        lines.append("recent errors:")
        lines += [f"  {e.get('at')} {e.get('error')}" for e in errs[-5:]]
    lines.append("shell log (last 10):")
    lines += ["  " + json.dumps({k: e[k] for k in e if k not in ("schema", "versions", "from", "to")}, ensure_ascii=False)
              for e in tail_jsonl(Path(root) / "logs" / "shell.jsonl", 10)]
    ups = tail_jsonl(Path(root) / "logs" / "updates.jsonl", 5)
    if ups:
        lines.append("updates (last 5):")
        lines += ["  " + json.dumps(e, ensure_ascii=False) for e in ups]
    if events:
        lines.append("this window (last 10):")
        lines += ["  " + e for e in events[-10:]]
    return scrub("\n".join(lines))
