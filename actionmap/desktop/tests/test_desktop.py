"""Nodach Desktop tests (Linux, no Windows needed): python -m pytest -q actionmap/desktop/tests"""
from __future__ import annotations

import ast
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

DESK = Path(__file__).resolve().parents[1]
ACTIONMAP = DESK.parent
sys.path.insert(0, str(DESK))
sys.path.insert(0, str(DESK / "shell"))
import build_zip                # noqa: E402
import desktop_core             # noqa: E402
import install_desktop as inst  # noqa: E402
import updater                  # noqa: E402

FAKE_WORKER = DESK / "tests" / "fake_worker.py"
FAKE_WMCP = ACTIONMAP / "gate" / "tests" / "fake_wmcp.py"


# ---------------------------------------------------------------- helpers
@pytest.fixture
def home(tmp_path):
    """An installed Nodach Desktop: tmp/home/app (built like the release zip) and empty data folders."""
    src = build_zip.assemble(tmp_path / "src", "0.1.0")
    h = tmp_path / "home"
    inst.install_app(src / "app", h)
    return h


def env_for(home: Path, fake_worker=True, **extra):
    e = dict(os.environ, NODACH_DESKTOP_HOME=str(home), NODACH_DESKTOP_APP=str(home / "app"),
             NODACH_GATE_HOME=str(home.parent / "nodach-gate"), NODACH_POINTER_HOME=str(home.parent / "nodach-pointer"),
             NODACH_LIVE_HOME=str(home.parent / "nodach-live"), NODACH_GATE_QUIET="1", NODACH_DESKTOP_NO_WINDOW="1",
             NODACH_GATE_CHILD_CMD=json.dumps([sys.executable, str(FAKE_WMCP)]), NODACH_DESKTOP_BACKOFF="0.5,1,1")
    if fake_worker:
        e["NODACH_DESKTOP_WORKER_CMD"] = json.dumps([sys.executable, "-u", str(FAKE_WORKER)])
    e.update(extra)
    return e


class Client:
    """Claude desktop's side, raw JSON-RPC over the shell's stdio."""

    def __init__(self, home: Path, env: dict):
        self.p = subprocess.Popen([sys.executable, str(home / "app" / "shell" / "shell_mcp.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(home))
        self.replies, self.notes, self.n = {}, queue.Queue(), 0
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            if "id" in m and "method" not in m:
                with self.cv:
                    self.replies[m["id"]] = m
                    self.cv.notify_all()
            else:
                self.notes.put(m)

    def send(self, method, params=None, notify=False):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        if not notify:
            self.n += 1
            m["id"] = self.n
        self.p.stdin.write((json.dumps(m) + "\n").encode())
        self.p.stdin.flush()
        return m.get("id")

    def wait(self, mid, timeout=30):
        end = time.time() + timeout
        with self.cv:
            while mid not in self.replies:
                if not self.cv.wait(max(0.01, end - time.time())) and time.time() > end:
                    raise TimeoutError(f"no reply to {mid}")
            return self.replies.pop(mid)

    def req(self, method, params=None, timeout=30):
        return self.wait(self.send(method, params), timeout)

    def call(self, name, args=None, timeout=30):
        return self.req("tools/call", {"name": name, "arguments": args or {}}, timeout)["result"]

    def init(self):
        r = self.req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}, 60)
        self.send("notifications/initialized", notify=True)
        return r

    def note(self, method, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.notes.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if m.get("method") == method:
                return m
        raise TimeoutError(f"no {method}")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.p.kill()


def text(result):
    return " ".join(c.get("text", "") for c in result["content"])


def names(result):
    return [t["name"] for t in result["result"]["tools"]]


# ---------------------------------------------------------------- the shell
def test_shell_is_stdlib_only():
    tree = ast.parse((DESK / "shell" / "shell_mcp.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods - {"__future__"} <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)


def test_initialize_lists_tools_and_declares_list_changed(home):
    c = Client(home, env_for(home))
    try:
        r = c.init()["result"]
        assert r["capabilities"]["tools"]["listChanged"] is True
        assert r["serverInfo"]["name"] == "nodach-desktop" and "desktop_reload" in r["instructions"]
        assert names(c.req("tools/list")) == ["echo", "slow", "crash", "desktop_status", "desktop_reload"]
        assert json.loads(text(c.call("echo")))["version"] == "0.1.0"
    finally:
        c.close()


def test_version_change_waits_for_running_call_then_restarts_worker_and_emits_list_changed(home):
    c = Client(home, env_for(home))
    try:
        c.init()
        c.req("tools/list")
        pid1 = json.loads(text(c.call("echo")))["pid"]
        slow = c.send("tools/call", {"name": "slow", "arguments": {}})
        time.sleep(0.2)
        # an update swaps in a new app the same way the Update button does
        nxt = home / "app" / "_next"
        shutil.copytree(home / "app", nxt, ignore=shutil.ignore_patterns("_next", "_prev", "__pycache__"))
        (nxt / "VERSION").write_text("0.1.1\n")
        updater.swap(home / "app")
        r = c.wait(slow, 20)["result"]                         # the running call finished on the old worker
        assert not r.get("isError") and json.loads(text(r))["pid"] == pid1
        c.note("notifications/tools/list_changed", 20)
        after = json.loads(text(c.call("echo")))
        assert after["pid"] != pid1 and after["version"] == "0.1.1"
        st = json.loads(text(c.call("desktop_status")))
        assert st["last_reload"]["ok"] and "VERSION 0.1.0 -> 0.1.1" in st["last_reload"]["reason"]
        assert st["versions_loaded"]["desktop"] == "0.1.1" and st["shell_restart_needed"] is None
        log = [json.loads(x) for x in (home / "logs" / "shell.jsonl").read_text().splitlines()]
        assert [e["kind"] for e in log].count("reload") == 1
    finally:
        c.close()


def test_desktop_reload_tool_and_window_request(home):
    c = Client(home, env_for(home))
    try:
        c.init()
        pid1 = json.loads(text(c.call("echo")))["pid"]
        r = c.call("desktop_reload", {"reason": "wrote worker.py"})
        assert not r["isError"] and "Reloaded" in text(r)
        c.note("notifications/tools/list_changed")
        pid2 = json.loads(text(c.call("echo")))["pid"]
        assert pid2 != pid1
        desktop_core.request_reload(home)                     # the window's 'Reload tools' button
        c.note("notifications/tools/list_changed")
        assert json.loads(text(c.call("echo")))["pid"] != pid2
    finally:
        c.close()


def test_crash_gives_clear_error_then_restarts_with_backoff(home):
    c = Client(home, env_for(home))
    try:
        c.init()
        c.req("tools/list")
        pid1 = json.loads(text(c.call("echo")))["pid"]
        r = c.call("crash")
        assert r["isError"] and "stopped while this call was running" in text(r)
        r = c.call("echo")
        assert r["isError"] and "isn't running" in text(r)
        lst = c.req("tools/list")                              # the tool list stays while it restarts
        assert "echo" in names(lst) and "desktop_status" in names(lst)
        c.note("notifications/tools/list_changed", 20)
        assert json.loads(text(c.call("echo")))["pid"] != pid1
        st = json.loads(text(c.call("desktop_status")))
        assert st["worker"]["restarts"] == 1 and st["worker"]["state"] == "ready"
        assert any("exit code 3" in e["error"] for e in st["errors"])
    finally:
        c.close()


def test_worker_that_keeps_dying_is_retried_until_it_starts(home, tmp_path):
    flag = tmp_path / "die"
    flag.write_text("x")
    c = Client(home, env_for(home, FAKE_WORKER_DIE_AT_START=str(flag)))
    try:
        r = c.init()["result"]                                 # Claude still gets an answer
        assert r["capabilities"]["tools"]["listChanged"] is True
        assert names(c.req("tools/list")) == ["desktop_status", "desktop_reload"]
        assert c.call("echo")["isError"]
        time.sleep(1.5)
        st = json.loads(text(c.call("desktop_status")))
        assert st["worker"]["state"] in ("down", "starting") and st["worker"]["restarts"] >= 1
        flag.unlink()
        c.note("notifications/tools/list_changed", 20)
        assert not c.call("echo").get("isError")
    finally:
        c.close()


def test_status_says_when_the_shell_itself_changed(home):
    c = Client(home, env_for(home))
    try:
        c.init()
        p = home / "app" / "shell" / "shell_mcp.py"
        p.write_text(p.read_text() + "\n# changed\n")
        c.note("notifications/tools/list_changed", 20)      # the file change also reloads the worker
        st = json.loads(text(c.call("desktop_status")))
        assert "restart Claude once" in st["shell_restart_needed"]
    finally:
        c.close()


def test_real_worker_tool_list_is_gate_allowlist_plus_pointer_plus_desktop(home):
    sys.path.insert(0, str(ACTIONMAP / "gate"))
    import gate_core
    sys.path.insert(0, str(FAKE_WMCP.parent))
    import fake_wmcp
    ph = home.parent / "nodach-pointer"
    ph.mkdir()
    (ph / "events.jsonl").write_text(json.dumps({"id": "P1", "mode": "single", "note": "this button"}) + "\n")
    c = Client(home, env_for(home, fake_worker=False))
    try:
        c.init()
        got = names(c.req("tools/list", timeout=60))
        want = [t for t in fake_wmcp.NAMES if t in gate_core.UI_TOOLS]
        assert set(got) == set(want) | set(gate_core.GATE_TOOLS) | {"get_pointed_elements", "get_pointer_screenshot",
                                                                     "mark_pointed"} | {"desktop_status", "desktop_reload"}
        assert "PowerShell" not in got and "FileSystem" not in got
        ev = json.loads(text(c.call("get_pointed_elements")))
        assert ev[0]["id"] == "P1" and ev[0]["status"] == "new"
        assert "Marked 1" in text(c.call("mark_pointed", {"event_ids": ["P1"], "status": "done"}))
        assert json.loads(text(c.call("get_pointed_elements", {"status": "done"})))[0]["id"] == "P1"
        assert c.call("PowerShell", {"command": "x"})["isError"]
        assert not c.call("Wait", {"duration": 0}).get("isError")
        act = json.loads((home / "run" / "acting.json").read_text())   # the window's 'Claude is acting' source
        assert act["tool"] == "Wait" and act["active"] is False
        gs = json.loads(text(c.call("gate_status")))
        assert gs["version"] == gate_core.VERSION
        acts = (home.parent / "nodach-gate" / "actions.jsonl").read_text()
        assert '"tool": "Wait"' in acts and '"tool": "PowerShell"' in acts
        ws = json.loads((home / "run" / "worker-status.json").read_text())
        assert ws["gate"]["windows_mcp_connected"] and ws["pointer"]["loaded"]
        st = json.loads(text(c.call("desktop_status")))
        assert st["versions_on_disk"]["gate"] == gate_core.VERSION and st["versions_on_disk"]["pointer"] != "?"
    finally:
        c.close()


def test_worker_serves_pointer_when_windows_mcp_is_missing(home):
    c = Client(home, env_for(home, fake_worker=False, NODACH_GATE_CHILD_CMD=json.dumps(["/nonexistent/windows-mcp"])))
    try:
        c.init()
        got = names(c.req("tools/list", timeout=60))
        assert "get_pointed_elements" in got and "gate_status" in got
        r = c.call("Click", {"loc": [1, 1]})
        assert r["isError"]
        ws = json.loads((home / "run" / "worker-status.json").read_text())
        assert not ws["gate"]["windows_mcp_connected"] and ws["gate"]["error"]
    finally:
        c.close()


# ---------------------------------------------------------------- updates
def fake_release(tmp_path, ver="0.1.1", private=False, corrupt=False):
    z, s = build_zip.build(tmp_path / "rel", ver)
    zb = z.read_bytes()
    files = {
        "https://api.github.com/repos/Morami49/nodach-studio": json.dumps({"private": private}).encode(),
        "https://api.github.com/repos/Morami49/nodach-studio/releases?per_page=50": json.dumps([
            {"tag_name": "actionmap/gate-v0.1.0", "assets": []},
            {"tag_name": "desktop-v0.0.9", "assets": [{"name": "nodach-desktop-0.0.9.zip", "browser_download_url": "u/old.zip"},
                                                      {"name": "nodach-desktop-0.0.9.zip.sha256", "browser_download_url": "u/old.sha"}]},
            {"tag_name": f"desktop-v{ver}", "assets": [{"name": z.name, "browser_download_url": "u/new.zip"},
                                                       {"name": s.name, "browser_download_url": "u/new.sha"}]}]).encode(),
        "u/new.zip": zb[:-10] + b"x" * 10 if corrupt else zb,
        "u/new.sha": s.read_bytes(),
    }

    def getter(url, timeout=30):
        if private and url.endswith("nodach-studio"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return files[url]
    return getter


def test_update_verifies_swaps_and_rolls_back(home, tmp_path):
    app = home / "app"
    getter = fake_release(tmp_path)
    r = updater.update(home, app, getter)
    assert r["status"] == "updated" and (r["from"], r["to"]) == ("0.1.0", "0.1.1")
    assert (app / "VERSION").read_text().strip() == "0.1.1" and (app / "_prev" / "VERSION").read_text().strip() == "0.1.0"
    assert (app / "shell" / "shell_mcp.py").exists() and (app / "gate" / "gate_mcp.py").exists()
    assert not (app / "_next").exists() and not (app / ".swapping").exists()
    assert updater.update(home, app, getter)["status"] == "up_to_date"
    rb = updater.do_rollback(home, app)
    assert rb["status"] == "rolled_back" and (app / "VERSION").read_text().strip() == "0.1.0"
    assert (app / "_prev" / "VERSION").read_text().strip() == "0.1.1"          # roll forward again the same way
    assert updater.do_rollback(home, app)["status"] == "rolled_back" and (app / "VERSION").read_text().strip() == "0.1.1"
    kinds = [json.loads(x)["kind"] for x in (home / "logs" / "updates.jsonl").read_text().splitlines()]
    assert kinds == ["install", "update", "check", "rollback", "rollback"]


def test_update_refuses_a_bad_checksum(home, tmp_path):
    r = updater.update(home, home / "app", fake_release(tmp_path, corrupt=True))
    assert r["status"] == "failed" and "checksum" in r["message"]
    assert (home / "app" / "VERSION").read_text().strip() == "0.1.0" and not (home / "app" / "_next").exists()


def test_private_repo_says_updates_come_from_claude(home, tmp_path):
    r = updater.update(home, home / "app", fake_release(tmp_path, private=True))
    assert r["status"] == "none" and r["message"] == updater.NOT_LINKED


def test_update_url_manifest(home, tmp_path):
    z, s = build_zip.build(tmp_path / "hub", "0.2.0")
    (tmp_path / "hub" / "latest.json").write_text(json.dumps({"version": "0.2.0", "zip": z.name, "sha256": s.name}))
    (home / "desktop.json").write_text(json.dumps({"update_url": (tmp_path / "hub" / "latest.json").as_uri()}))
    r = updater.update(home, home / "app")                  # real urllib, file:// URLs
    assert r["status"] == "updated" and r["to"] == "0.2.0"


def test_failed_swap_changes_nothing(home, tmp_path):
    app = home / "app"
    nxt = app / "_next"
    shutil.copytree(app, nxt, ignore=shutil.ignore_patterns("_next", "_prev"))
    (nxt / "VERSION").write_text("9.9.9\n")
    before = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if "_next" not in p.parts)
    calls = []

    def mover(a, b):
        calls.append(a)
        if len(calls) == 7:          # fail part way through moving the new files in
            raise PermissionError("in use")
        os.replace(a, b)
    with pytest.raises(updater.UpdateError, match="nothing was changed"):
        updater.swap(app, mover=mover)
    after = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if "_next" not in p.parts)
    assert after == before and (app / "VERSION").read_text().strip() == "0.1.0" and not (app / ".swapping").exists()


def test_unpack_refuses_unsafe_paths(tmp_path):
    import zipfile
    zp = tmp_path / "bad.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../evil.txt", "x")
    (tmp_path / "app").mkdir()
    with pytest.raises(updater.UpdateError, match="unsafe"):
        updater.unpack_to_next(zp, tmp_path / "app")


def test_pick_release_takes_newest_desktop_tag():
    rel = [{"tag_name": "desktop-v0.1.10", "assets": [{"name": "nodach-desktop-0.1.10.zip", "browser_download_url": "a"},
                                                       {"name": "nodach-desktop-0.1.10.zip.sha256", "browser_download_url": "b"}]},
           {"tag_name": "desktop-v0.1.9", "assets": [{"name": "nodach-desktop-0.1.9.zip", "browser_download_url": "c"},
                                                      {"name": "nodach-desktop-0.1.9.zip.sha256", "browser_download_url": "d"}]},
           {"tag_name": "desktop-v0.2.0", "draft": True, "assets": []}]
    assert updater.pick_release(rel)["version"] == "0.1.10"


# ---------------------------------------------------------------- installer and bundle
def test_config_migration_removes_old_entries_keeps_others_backup_no_bom(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    old = {"mcpServers": {"nodach-gate": {"command": "py", "args": ["gate_mcp.py"]},
                          "nodach-pointer": {"command": "python", "args": ["pointer_mcp.py"]},
                          "windows-mcp": {"command": "uvx", "args": ["windows-mcp"]},
                          "tradingview": {"command": "node", "args": ["tv.js"]}},
           "preferences": {"x": 1}}
    p.write_bytes("\ufeff".encode() + json.dumps(old).encode())
    entry = inst.server_entry(r"C:\Python314\python.exe", r"C:\Users\n\nodach\app\shell\shell_mcp.py")
    import datetime as dt
    r = inst.write_config(p, entry, now=dt.datetime(2026, 11, 21, 9, 0, 0))
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    new = json.loads(raw)
    assert set(new["mcpServers"]) == {"nodach-desktop", "tradingview"} and new["preferences"] == {"x": 1}
    assert new["mcpServers"]["nodach-desktop"]["command"].endswith("python.exe")
    assert sorted(r["removed"]) == ["nodach-gate", "nodach-pointer", "windows-mcp"]
    assert Path(r["backup"]).name == "claude_desktop_config.json.bak-20261121-090000"
    assert json.loads(Path(r["backup"]).read_bytes().decode("utf-8-sig")) == old
    # a broken file is left alone
    p.write_text("{not json")
    with pytest.raises(ValueError):
        inst.write_config(p, entry)
    assert p.read_text() == "{not json"


def test_config_paths_include_msix_when_installed(tmp_path):
    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    assert inst.config_paths(str(appdata), str(local)) == [appdata / "Claude" / "claude_desktop_config.json"]
    (local / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    ps = inst.config_paths(str(appdata), str(local))
    assert ps[1] == local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    assert inst.python_exe(r"C:\Py\pythonw.exe") == r"C:\Py\python.exe"


def test_reinstall_keeps_previous_copy_and_data(tmp_path, home):
    src = build_zip.assemble(tmp_path / "src2", "0.1.2")
    r = inst.install_app(src / "app", home)
    assert (r["from"], r["to"]) == ("0.1.0", "0.1.2") and (home / "app" / "_prev" / "VERSION").exists()
    assert json.loads((home / "desktop.json").read_text())["update_url"] == ""


def test_bundle_layout(tmp_path):
    z, s = build_zip.build(tmp_path, "0.1.0")
    import zipfile
    n = set(zipfile.ZipFile(z).namelist())
    for f in ("install-desktop.bat", "install_desktop.py", "app/VERSION", "app/shell/shell_mcp.py", "app/shell/worker.py",
              "app/shell/desktop_gui.py", "app/shell/updater.py", "app/gate/gate_mcp.py", "app/gate/zones.json",
              "app/gate/gate_popup.py", "app/pointer/pointer_mcp.py", "app/pointer/pointer.py", "app/live/live_gui.py"):
        assert "nodach-desktop/" + f in n, f
    assert not any("test_" in x or "/tests/" in x or "install_gate" in x or "install-claude-config" in x for x in n)
    assert updater.parse_sha(s.read_text()) == updater.sha256_of(z)


# ---------------------------------------------------------------- window helpers
def test_gate_pause_toggle_keeps_other_state_and_logs(tmp_path):
    gh = tmp_path / "nodach-gate"
    gh.mkdir()
    (gh / "state.json").write_text(json.dumps({"screenshots": False}))
    desktop_core.set_gate_paused(True, gh)
    assert desktop_core.gate_paused(gh) and json.loads((gh / "state.json").read_text())["screenshots"] is False
    desktop_core.set_gate_paused(False, gh)
    log = [json.loads(x) for x in (gh / "actions.jsonl").read_text().splitlines()]
    assert [e["detail"].split(" (")[0] for e in log] == ["Pause all on: every action is blocked", "Pause all off"]


def test_launch_and_stop_track_the_pid(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n")
    pid = desktop_core.launch(script, "pointer.pid", tmp_path, log_name="pointer-app.log")
    assert desktop_core.running("pointer.pid", tmp_path)
    assert desktop_core.stop("pointer.pid", tmp_path)
    for _ in range(50):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not desktop_core.running("pointer.pid", tmp_path):
            break
        time.sleep(0.1)
    assert not desktop_core.running("pointer.pid", tmp_path)


def test_report_hides_emails_tokens_and_home(home):
    text_ = desktop_core.build_report(home / "app", home, events=["x someone@example.com " + "A" * 48 + " " + str(Path.home())],
                                      env={"APPDATA": str(home / "ad"), "LOCALAPPDATA": str(home / "lad")})
    assert "Nodach Desktop report" in text_ and "desktop 0.1.0" in text_
    assert "@example.com" not in text_ and "A" * 48 not in text_ and "[email]" in text_ and "[hidden]" in text_


def test_acting_state_for_the_orange_banner(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    now = time.time()
    (run / "acting.json").write_text(json.dumps({"active": True, "tool": "Click", "t": now, "pid": os.getpid()}))
    assert desktop_core.acting_state(tmp_path, now=now + 30) == {"active": True, "tool": "Click", "since": None}
    (run / "acting.json").write_text(json.dumps({"active": False, "tool": "Click", "t": now, "pid": os.getpid()}))
    assert desktop_core.acting_state(tmp_path, now=now + 1)["active"]           # a quick click stays visible briefly
    assert not desktop_core.acting_state(tmp_path, now=now + 5)["active"]
    (run / "acting.json").write_text(json.dumps({"active": True, "tool": "Type", "t": now, "pid": 999999}))
    assert not desktop_core.acting_state(tmp_path)["active"]                    # a dead worker never shows as acting
