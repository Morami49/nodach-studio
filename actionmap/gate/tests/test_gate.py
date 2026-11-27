"""Nodach Gate tests: pure rules, log masking, captures, automatic reports, config merge, outbox, and the proxy
end to end against a fake Windows-MCP (no Windows needed).  python -m pytest -q actionmap/gate/tests"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATE))
import gate_core as core        # noqa: E402
import install_gate as inst     # noqa: E402

Z = core.load_zones(None)


def d(tool, args=None, **ctx):
    return core.decide({"tool": tool, "args": args or {}}, ctx, Z)


# ------------------------------------------------------------------ zones
def test_zones_file_is_valid_and_versioned():
    raw = json.loads((GATE / "zones.json").read_text(encoding="utf-8"))
    assert raw["schema"] == "gate.zones.v1" and raw["revision"] >= 1 and raw["_changes"]
    assert "_comment" not in Z and Z["_sha"]


def test_bitwarden_window_is_never():
    z, r = d("Click", {"loc": [5, 5]}, app="Bitwarden.exe", title="Vault - Bitwarden", element_name="Copy")
    assert z == "never" and "password manager" in r
    assert d("Snapshot", app="KeePassXC.exe", title="db.kdbx")[0] == "never"   # reads too


def test_banking_title_and_card_field_are_never():
    assert d("Type", {"loc": [1, 1], "text": "x"}, app="chrome.exe", title="Barclays Online Banking - Google Chrome")[0] == "never"
    assert d("Click", {"loc": [1, 1]}, app="msedge.exe", title="Checkout - Shop")[0] == "never"
    assert d("Type", {"loc": [1, 1], "text": "4111"}, app="chrome.exe", title="Shop", element_name="Card number")[0] == "never"
    assert d("Click", {"loc": [1, 1]}, app="chrome.exe", title="Bankside Gallery tickets", element_name="x")[0] == "auto"


def test_uac_and_windows_security_are_never():
    assert d("Click", {"loc": [1, 1]}, app="consent.exe", title="User Account Control")[0] == "never"
    assert d("Shortcut", {"shortcut": "enter"}, app="SecHealthUI.exe", title="Windows Security")[0] == "never"


def test_settings_accounts_privacy_never_rest_confirm():
    assert d("Click", {"loc": [1, 1]}, app="SystemSettings.exe", title="Settings", element_name="Accounts")[0] == "never"
    assert d("Click", {"loc": [1, 1]}, app="SystemSettings.exe", title="Settings", element_name="Toggle", page_hint="Privacy & security / Location")[0] == "never"
    assert d("Click", {"loc": [1, 1]}, app="SystemSettings.exe", title="Settings", element_name="Bluetooth")[0] == "confirm"
    assert d("Scroll", {"loc": [1, 1]}, app="SystemSettings.exe", title="Settings", element_name="Display")[0] == "auto"


def test_send_button_is_confirm():
    z, r = d("Click", {"loc": [10, 10]}, app="OUTLOOK.EXE", title="Inbox", element_name="Send", element_type="Button")
    assert z == "confirm" and "send" in r.lower()
    for name in ("Delete", "Uninstall", "Buy now", "Sign out", "Publish", "Submit", "Pay"):
        assert d("Click", {"loc": [1, 1]}, app="x.exe", element_name=name)[0] == "confirm", name


def test_whole_words_only():
    assert d("Click", {"loc": [1, 1]}, app="chrome.exe", title="Form", element_name="Postcode")[0] == "auto"
    assert d("Click", {"loc": [1, 1]}, app="chrome.exe", title="Form", element_name="Sender name")[0] == "auto"


def test_hover_is_auto_even_on_send():
    assert d("Click", {"loc": [1, 1], "clicks": 0}, app="x.exe", element_name="Send")[0] == "auto"


def test_typing_in_notepad_is_auto():
    z, _ = d("Type", {"loc": [100, 200], "text": "hello"}, app="notepad.exe", title="Untitled - Notepad", element_name="Text editor")
    assert z == "auto"


def test_enter_after_typing_message_is_confirm():
    assert d("Type", {"loc": [1, 1], "text": "hi", "press_enter": True}, app="ms-teams.exe", element_name="Type a message")[0] == "confirm"
    last = {"element_name": "Write a comment", "ts": 0}
    assert d("Shortcut", {"shortcut": "Enter"}, app="chrome.exe", title="YouTube", last_typed=last)[0] == "confirm"
    assert d("Shortcut", {"shortcut": "ctrl+enter"}, app="chrome.exe", title="Mail", last_typed={"element_name": "Message body"})[0] == "confirm"
    assert d("Type", {"loc": [1, 1], "text": "hi"}, app="ms-teams.exe", element_name="Type a message")[0] == "auto"
    stale = {"element_name": "Write a comment", "ts": 0}
    assert d("Shortcut", {"shortcut": "Enter"}, app="chrome.exe", title="YouTube", last_typed=stale, now=1000)[0] == "auto"


def test_delete_keys_are_confirm():
    assert d("Shortcut", {"shortcut": "shift+delete"}, app="explorer.exe", title="Downloads")[0] == "confirm"
    assert d("Shortcut", {"shortcut": "Del"}, app="explorer.exe", title="Downloads")[0] == "confirm"
    assert d("Shortcut", {"shortcut": "ctrl+c"}, app="explorer.exe", title="Downloads")[0] == "auto"


def test_app_launch_rules():
    assert d("App", {"mode": "launch", "name": "Notepad"}, app="Claude.exe")[0] == "auto"
    assert d("App", {"mode": "launch", "name": "Steam"}, app="Claude.exe")[0] == "confirm"
    assert d("App", {"mode": "launch", "name": "PowerShell"}, app="Claude.exe")[0] == "never"
    assert d("App", {"mode": "launch_executable", "executable": "C:/x.exe"})[0] == "never"
    assert d("App", {"mode": "switch", "name": "Bitwarden"}, target_app="Bitwarden.exe", target_title="Bitwarden")[0] == "never"
    assert d("App", {"mode": "switch", "name": "Notepad"}, app="Claude.exe")[0] == "auto"


def test_claude_window_readable_not_clickable():
    assert d("Snapshot", app="claude.exe", title="Claude")[0] == "auto"
    assert d("Click", {"loc": [1, 1]}, app="claude.exe", title="Claude", element_name="Allow")[0] == "never"


def test_gate_windows_never_clickable():
    assert d("Click", {"loc": [1, 1]}, app="pythonw.exe", title="Nodach Gate — confirm", element_name="Allow once")[0] == "never"


def test_shell_window_input_confirm():
    assert d("Type", {"loc": [1, 1], "text": "dir"}, app="WindowsTerminal.exe", title="PowerShell")[0] == "confirm"


def test_powershell_tool_is_hidden():
    assert d("PowerShell", {"command": "rm -r C:/"})[0] == "hidden"
    for name in ("FileSystem", "Registry", "Process", "Clipboard", "Scrape", "MultiEdit", "BrandNewTool"):
        assert d(name)[0] == "hidden", name


def test_label_and_save_path_refused():
    assert d("Click", {"label": 3}, app="notepad.exe")[0] == "invalid"
    assert d("Snapshot", {"save_path": "C:/x.png"}, app="notepad.exe")[0] == "invalid"


def test_pause_blocks_everything_but_gate_tools():
    assert d("Wait", {"duration": 1}, paused=True)[0] == "never"
    assert d("Snapshot", paused=True)[0] == "never"
    assert d("gate_status", paused=True)[0] == "auto"


def test_password_field_typing_confirm():
    assert d("Type", {"loc": [1, 1], "text": "x"}, app="chrome.exe", title="Login", element_name="Password", is_password=True)[0] == "confirm"


# ------------------------------------------------------------------ tools
def test_allowlist_filtering():
    tools = [{"name": n} for n in ("Click", "PowerShell", "Snapshot", "FileSystem", "Registry", "Process", "Future")]
    exposed, hidden = core.filter_tools(tools)
    assert [t["name"] for t in exposed] == ["Click", "Snapshot"]
    assert {t["name"] for t in hidden} == {"PowerShell", "FileSystem", "Registry", "Process", "Future"}


def test_strip_label():
    s = core.strip_label({"type": "object", "properties": {"loc": {}, "label": {}}, "required": ["label"]})
    assert "label" not in s["properties"] and s["required"] == []


def test_grants_and_popup_outcome():
    g = core.Grants()
    assert core.popup_outcome("app10", "OUTLOOK.EXE", g, 1000) == ("allowed", "user")
    assert g.active("outlook", 1000 + 599) and not g.active("outlook", 1000 + 601)
    assert core.popup_outcome(None, "x", g, 0) == ("blocked", "timeout")
    assert core.popup_outcome("timeout", "x", g, 0) == ("blocked", "timeout")
    assert core.popup_outcome("block", "x", g, 0) == ("blocked", "user")
    assert core.popup_outcome("once", "x", g, 0) == ("allowed", "user")


# ------------------------------------------------------------------ log
def test_log_masks_password_and_truncates():
    pw = core.log_entry(ts=0, tool="Type", args={"text": "hunter2", "loc": [1, 2]}, ctx={"element_name": "Password", "is_password": True},
                        zone="confirm", reason="", decision="allowed", by="user", duration_ms=5, ok=True)
    assert pw["args"]["text"] == "[masked 7 chars]" and "hunter2" not in json.dumps(pw)
    pin = core.log_entry(ts=0, tool="Type", args={"text": "1234"}, ctx={"element_name": "Enter PIN"}, zone="auto", reason="",
                         decision="allowed", by="auto", duration_ms=1, ok=True)
    assert pin["args"]["text"].startswith("[masked")
    long = core.log_entry(ts=0, tool="Type", args={"text": "a" * 200}, ctx={"element_name": "Text editor", "title": "t" * 300},
                          zone="auto", reason="", decision="allowed", by="auto", duration_ms=1, ok=True)
    assert long["args"]["text"] == "a" * 80 + "…(+120)" and len(long["title"]) < 100
    assert long["schema"] == "gate.v1" and {"ts", "tool", "args", "app", "title", "element", "zone", "decision", "by", "duration_ms", "ok"} <= set(long)


# ------------------------------------------------------------------ snapshot text
SNAP = (GATE / "tests" / "fake_wmcp.py").read_text(encoding="utf-8").split("SNAP = '''")[1].split("'''")[0]


def test_redact_snapshot_hides_never_windows():
    text, n = core.redact_snapshot(SNAP, Z)
    assert n == 1 and "Card number" not in text and "Barclays" not in text and "Text editor" in text


def test_parse_elements_and_near():
    rows = core.parse_elements(SNAP)
    assert rows[0] == {"x": 100, "y": 200, "type": "edit", "name": "Text editor", "window": "Untitled - Notepad", "password": False}
    assert core.element_near(rows, [101, 199])["name"] == "Text editor" and core.element_near(rows, [0, 0]) is None


# ------------------------------------------------------------------ captures
CAP = core.capture_settings(Z)


def test_shot_reasons():
    sr = core.shot_reasons
    assert sr("before", tool="Click", zone="confirm", ctx={"element_name": "Send", "element_type": "Button"}, settings=CAP) == ["confirm"]
    assert sr("before", tool="Click", zone="auto", ctx={"element_name": "", "element_type": "Button"}, settings=CAP) == ["no-name"]
    assert sr("before", tool="Click", zone="auto", ctx={"element_name": "Map", "element_type": "Pane"}, settings=CAP) == ["custom-drawn"]
    assert sr("before", tool="Click", zone="auto", ctx={"element_name": "OK", "element_type": "Button"}, settings=CAP) == []
    assert sr("after", tool="Click", zone="auto", ctx={}, settings=CAP, ok=False) == ["error"]
    assert sr("after", tool="Click", zone="auto", ctx={}, settings=CAP, before_hash=5, after_hash=5) == ["unchanged"]
    assert sr("after", tool="Click", zone="auto", ctx={}, settings=CAP, before_hash=5, after_hash=6) == []
    assert sr("after", tool="Wait", zone="auto", ctx={}, settings=CAP, before_hash=5, after_hash=5) == []
    assert sr("before", tool="Click", zone="confirm", ctx={}, settings=dict(CAP, screenshots=False)) == []
    assert sr("before", tool="Wait", zone="auto", ctx={}, settings=CAP, asked=True) == ["asked"]


def test_toggles_override_zones():
    assert core.capture_settings(Z, {"screenshots": False, "clips": False}) == dict(CAP, screenshots=False, clips=False)


def test_never_capture_while_never_in_front():
    assert core.can_capture("auto", foreground_never=True) is False
    assert core.can_capture("never", foreground_never=False) is False
    assert core.can_capture("confirm", foreground_never=False) is True


def test_hash_and_rect():
    a = core.ahash([0] * 32 + [255] * 32)
    b = core.ahash([0] * 31 + [255] * 33)
    assert core.hamming(a, a) == 0 and core.hamming(a, b) == 1
    assert core.shot_rect((100, 100, 200, 150), None, (0, 0, 1920, 1080), 40) == (60, 60, 240, 190)
    assert core.shot_rect(None, [10, 10], (0, 0, 1920, 1080), 40, 160) == (0, 0, 170, 170)
    assert core.shot_rect(None, None, (0, 0, 1, 1)) is None


def test_clip_trigger_glitch_rules():
    ct = core.clip_trigger
    assert ct(kind="user", now=0, settings=CAP) == "user"
    assert ct(kind="error", now=0, settings=CAP) == "error"
    assert ct(kind="error", now=0, settings=dict(CAP, clips=False)) is None
    assert ct(kind="frame", now=105, settings=CAP, last_action_t=100, frame_change_bits=30) == "glitch"
    assert ct(kind="frame", now=100.5, settings=CAP, last_action_t=100, frame_change_bits=30) is None   # just acted
    assert ct(kind="frame", now=200, settings=CAP, last_action_t=100, frame_change_bits=30) is None     # nobody driving
    assert ct(kind="frame", now=105, settings=CAP, last_action_t=100, frame_change_bits=3) is None      # tiny change
    assert ct(kind="frame", now=105, settings=CAP, last_action_t=None, frame_change_bits=30) is None


def test_clip_limiter():
    lim = core.ClipLimiter(cooldown_s=30, per_hour=2)
    assert lim.allow("glitch", 0) and not lim.allow("glitch", 10) and lim.allow("glitch", 40)
    assert not lim.allow("glitch", 100)      # 2 an hour
    assert lim.allow("error", 101) and lim.allow("user", 102)


def test_clip_entry_lists_actions_inside_window():
    acts = [{"_t": 5, "ts": "a", "tool": "Click", "zone": "auto", "decision": "allowed"}, {"_t": 50, "tool": "Type"}]
    e = core.clip_entry(ts=20, start=0, end=20, trigger="error", path="clips/x.gif", frames=20, actions=acts)
    assert e["kind"] == "clip" and e["schema"] == "gate.v1" and [a["tool"] for a in e["actions"]] == ["Click"]


# ------------------------------------------------------------------ automatic reports
RS = core.auto_report_settings(Z)


def act(**kw):
    e = {"kind": "action", "tool": "Click", "args": {"loc": [1, 1]}, "zone": "auto", "decision": "allowed", "by": "auto", "ok": True}
    e.update(kw)
    return e


def test_report_defaults():
    assert RS["send_automatically"] and RS["never_block"] and RS["error_or_glitch"] and RS["repeat_fail"]
    assert RS["confirm_timeout"] and RS["run_end"] and RS["session_summary"] and not RS["daily_summary"]
    assert RS["hub"] == "staging" and RS["per_hour"] == 6


def test_report_triggers():
    t = core.auto_report_trigger
    assert t(act(zone="never", decision="refused", reason="password manager: app x"), RS) == "never_block"
    assert t(act(zone="never", decision="refused", reason="Gate is paused (...)"), RS) is None
    assert t(act(zone="confirm", decision="blocked", by="timeout"), RS) == "confirm_timeout"
    assert t(act(zone="confirm", decision="blocked", by="user"), RS) == "confirm_timeout"
    assert t(act(ok=False), RS) == "error_or_glitch"
    assert t(act(ok=False), RS, act(ok=False)) == "repeat_fail"
    assert t(act(ok=False), RS, act(ok=False, args={"loc": [9, 9]})) == "error_or_glitch"
    assert t({"kind": "clip", "trigger": "glitch"}, RS) == "error_or_glitch"
    assert t({"kind": "clip", "trigger": "user"}, RS) is None
    assert t(act(), RS) is None
    off = core.auto_report_settings(Z, {"auto_reports": {"send_automatically": False}})
    assert t(act(zone="never", decision="refused", reason="x"), off) is None
    assert t(act(ok=False), core.auto_report_settings(Z, {"auto_reports": {"error_or_glitch": False}})) is None


def test_never_reports_text_only():
    assert core.wants_pictures("never_block", RS) is False
    assert core.wants_pictures("run_end", RS) is False
    assert core.wants_pictures("error_or_glitch", RS) is True
    assert core.wants_pictures("error_or_glitch", dict(RS, attach_pictures=False)) is False
    assert core.wants_pictures("manual", RS) is False and core.wants_pictures("manual", RS, True) is True


def test_report_limiter():
    lim = core.ReportLimiter({}, per_hour=6, min_gap_s=300, per_day=20)
    assert lim.allow("error_or_glitch", 0) == (True, "")
    ok, why = lim.allow("error_or_glitch", 60)
    assert not ok and "gap" in why
    assert lim.allow("never_block", 61)[0]                     # blocks skip the gap
    for i in range(1, 5):
        assert lim.allow("run_end", 300 * i + 1)[0]
    ok, why = lim.allow("never_block", 1600)                    # 6 already this hour
    assert not ok and "hourly" in why
    assert lim.allow("error_or_glitch", 3700)[0]


def test_report_limiter_daily_and_saved_state():
    state = {}
    lim = core.ReportLimiter(state, per_hour=100, min_gap_s=0, per_day=3)
    assert all(lim.allow("never_block", i)[0] for i in range(3))
    again = core.ReportLimiter(json.loads(json.dumps(state)), per_hour=100, min_gap_s=0, per_day=3)
    ok, why = again.allow("never_block", 10)
    assert not ok and "daily" in why
    assert again.allow("never_block", 86400 + 5)[0]


def test_summary_timers():
    assert core.run_end_due(now=400, last_action_t=100, last_run_end_t=None, settings=RS)
    assert not core.run_end_due(now=200, last_action_t=100, last_run_end_t=None, settings=RS)
    assert not core.run_end_due(now=900, last_action_t=100, last_run_end_t=400, settings=RS)   # once per run
    assert not core.run_end_due(now=900, last_action_t=None, last_run_end_t=None, settings=RS)
    assert core.session_summary_due(now=101, last_action_t=100, last_summary_t=None, closing=True, settings=RS)
    assert core.session_summary_due(now=1901, last_action_t=100, last_summary_t=None, closing=False, settings=RS)
    assert not core.session_summary_due(now=500, last_action_t=100, last_summary_t=None, closing=False, settings=RS)
    on = dict(RS, daily_summary=True)
    assert core.daily_summary_due(now_local=dt.datetime(2026, 11, 20, 21, 1), last_daily_date=None, settings=on)
    assert not core.daily_summary_due(now_local=dt.datetime(2026, 11, 20, 20, 59), last_daily_date=None, settings=on)
    assert not core.daily_summary_due(now_local=dt.datetime(2026, 11, 20, 22, 0), last_daily_date="2026-11-20", settings=on)
    assert not core.daily_summary_due(now_local=dt.datetime(2026, 11, 20, 22, 0), last_daily_date=None, settings=RS)


def test_day_item_and_events():
    assert core.day_item_id(dt.date(2026, 11, 20)) == "GATE-261120"
    me = {"id": "github:someone", "owner": True}
    ev = core.day_item_event("GATE-261120", me, dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc))
    assert ev["kind"] == "item.create" and ev["body"]["id"] == "GATE-261120" and "2026-11-20" in ev["body"]["title"]
    rep = core.report_event("GATE-261120", me, "x a" + "@" + "b.co", dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc), "never_block",
                            [{"name": "a.png", "data": "SECRETPIXELS", "local": True}])
    assert rep["item"] == "GATE-261120" and "[email]" in rep["body"]["text"]
    assert rep["body"]["attachments"] == [{"name": "a.png", "local": True}]       # never the pixels
    assert core.item_exists([{"event": ev}], "GATE-261120") and not core.item_exists([], "GATE-261120")


def test_report_has_no_typed_text_or_titles():
    e = core.log_entry(ts=1_700_000_000, tool="Type", args={"text": "my secret plan"}, ctx={"app": "notepad.exe", "title": "diary.txt - Notepad",
                       "element_name": "Text editor", "element_type": "Edit"}, zone="auto", reason="", decision="allowed", by="auto", duration_ms=3, ok=True)
    text = core.build_report({"version": "t"}, [e])
    assert "secret plan" not in text and "diary" not in text and "Text editor" in text and "auto/allowed=1" in text


def test_chunks_fit_hub_limit():
    parts = core.file_chunks(b"x" * (core.CHUNK_BYTES * 2 + 5))
    assert len(parts) == 3 and all(len(p) < 60 * 1024 for p in parts)
    ev = core.chunk_event("a.gif", "ab" * 32, 0, 3, parts[0], dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert len(json.dumps(ev)) < 64 * 1024 and core.file_doc_id("ab" * 32, 1).startswith("gf-")


def test_scrub():
    AT = "@"   # built at run time: no address-shaped text in the repo
    tok = "someone." + "1" * 12 + "." + "a" * 64
    s = core.scrub(f"{tok} x{AT}y.com C:\\Users\\N\\nodach-gate\\shots\\a.png C:\\Users\\N\\Desktop\\b.txt 4111 1111 1111 1111")
    assert "[sign-in value]" in s and "[email]" in s and "nodach-gate/shots/a.png" in s and "[path]" in s and "[number]" in s


# ------------------------------------------------------------------ installer
def test_config_merge_keeps_servers_backup_no_bom(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    old = {"mcpServers": {"filesystem": {"command": "npx", "args": ["x"]},
                          "windows-mcp": {"command": "uvx", "args": ["windows-mcp"]}},
           "preferences": {"menuBarEnabled": True}}
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(old).encode("utf-8"))           # an old file with a BOM
    entry = inst.server_entry("C:\\Python314\\python.exe", "C:\\Users\\N\\nodach-gate\\app\\gate_mcp.py")
    res = inst.write_config(p, entry, dt.datetime(2026, 11, 20, 12, 0, 0))
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    new = json.loads(raw.decode("utf-8"))
    assert new["mcpServers"]["filesystem"] == old["mcpServers"]["filesystem"] and new["preferences"] == old["preferences"]
    assert new["mcpServers"]["nodach-gate"]["command"] == "C:\\Python314\\python.exe"
    assert "windows-mcp" not in new["mcpServers"] and res["removed"] == ["windows-mcp"]
    b = Path(res["backup"])
    assert b.name == "claude_desktop_config.json.bak-20261120-120000" and json.loads(b.read_bytes().decode("utf-8-sig")) == old


def test_config_new_file_and_bad_json(tmp_path):
    p = tmp_path / "c.json"
    res = inst.write_config(p, {"command": "py"})
    assert res["backup"] is None and json.loads(p.read_text())["mcpServers"] == {"nodach-gate": {"command": "py"}}
    p.write_text("{not json")
    with pytest.raises(ValueError):
        inst.write_config(p, {"command": "py"})
    assert p.read_text() == "{not json"                                           # left alone


def test_config_paths_and_python(tmp_path):
    assert len(inst.config_paths(str(tmp_path / "Roaming"), str(tmp_path / "Local"))) == 1
    (tmp_path / "Local" / "Packages" / inst.MSIX_PKG / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    ps = inst.config_paths(str(tmp_path / "Roaming"), str(tmp_path / "Local"))
    assert len(ps) == 2 and "Claude_pzs8sxrjxfjjc" in str(ps[1])
    assert inst.python_exe("C:\\Py\\pythonw.exe").endswith("python.exe")
    assert not inst.is_raw_windows_mcp("nodach-gate", {"command": "python", "args": ["windows_mcp"]})


# ------------------------------------------------------------------ outbox (fake hub)
class FakeHub:
    instances = []
    online, owner, private_ok = True, True, True

    def __init__(self, base, token):
        self.base, self.posts, self.private = base, [], []
        FakeHub.instances.append(self)

    def _check(self):
        if not FakeHub.online:
            raise studio.HubError("couldn't reach the hub (URLError)", 0)

    def me(self):
        self._check()
        return {"login": "someone", "id": "github:someone", "owner": FakeHub.owner}

    def events(self):
        self._check()
        return []

    def post(self, event, did):
        self._check()
        self.posts.append(event)

    def private_put(self, path, did, event):
        if not FakeHub.private_ok:
            raise studio.HubError("hub said 503: the private store isn't set up", 503)
        self.private.append((path, did, len(json.dumps(event))))
        return {"id": did, "stored": True}


import gate_studio as studio  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "HOME", tmp_path)
    monkeypatch.setattr(studio, "Hub", FakeHub)
    FakeHub.instances, FakeHub.online, FakeHub.owner, FakeHub.private_ok = [], True, True, True
    return tmp_path


def test_outbox_queued_when_signed_out_then_sent(home, monkeypatch):
    monkeypatch.delenv("NODACH_GATE_TOKEN", raising=False)
    shot = home / "shots" / "a.png"
    shot.parent.mkdir()
    shot.write_bytes(b"P" * 100_000)
    studio.queue_report("error_or_glitch", "report text", [str(shot)], "staging", now=1_790_000_000)
    r = studio.flush()
    assert r[0]["status"] == "queued" and "not signed in" in r[0]["detail"] and len(studio.pending()) == 1
    studio.save_token(studio.HUBS["staging"], "someone." + "1" * 12 + "." + "a" * 64)
    FakeHub.online = False
    assert studio.flush()[0]["status"] == "queued"
    FakeHub.online = True
    r = studio.flush()
    assert r[0]["status"] == "sent" and not studio.pending()
    h = FakeHub.instances[-1]
    assert [p["kind"] for p in h.posts] == ["item.create", "comment"]
    assert h.posts[0]["body"]["id"].startswith("GATE-")
    att = h.posts[1]["body"]["attachments"][0]
    assert att["private_path"] == "data/users/someone/gate/files" and att["chunks"] == 3 and "data" not in att
    assert all(n < 64 * 1024 for _, _, n in h.private)
    assert (home / "outbox" / "sent").exists()


def test_outbox_owner_only_and_private_missing(home):
    studio.save_token(studio.HUBS["staging"], "someone." + "1" * 12 + "." + "a" * 64)
    f = home / "a.png"
    f.write_bytes(b"x")
    studio.queue_report("manual", "t", [str(f)], "staging")
    FakeHub.owner = False
    r = studio.flush()
    assert r[0]["status"] == "queued" and "owner only" in r[0]["detail"]
    FakeHub.owner, FakeHub.private_ok = True, False
    r = studio.flush()
    assert r[0]["status"] == "sent" and FakeHub.instances[-1].posts[-1]["body"]["attachments"] == [{"name": "a.png", "local": True}]


# ------------------------------------------------------------------ the proxy, end to end
mcp = pytest.importorskip("mcp")
import gate_mcp  # noqa: E402
import gate_win  # noqa: E402


class FakeStudio:
    def __init__(self):
        self.queued = []
        self.pending = lambda: []

    def queue_report(self, kind, text, files, hub, now):
        self.queued.append((kind, text, list(files)))
        return Path(f"{len(self.queued)}.json")

    def flush(self, max_mb):
        return []


def run_gate(tmp_path, ctx, answers, steps):
    popups = []

    def popup(req):
        popups.append(req)
        return answers.pop(0) if answers else "timeout"
    fs = FakeStudio()

    async def go():
        g = gate_mcp.Gate(ctxp=ctx, home=tmp_path, popup=popup, child_cmd=[sys.executable, str(GATE / "tests" / "fake_wmcp.py")],
                          studio_mod=fs, autostart_gui=False)
        out = []
        async with contextlib.AsyncExitStack() as stack:
            await g.connect(stack)
            out.append([t["name"] for t in g.tool_defs()])
            for name, args in steps:
                out.append(await g.call(name, args))
        return g, out
    g, out = asyncio.run(go())
    log = [json.loads(x) for x in (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
    return g, out, log, popups, fs


def _text(r):
    return " ".join(c["text"] if isinstance(c, dict) else c.text for c in r["content"])


def test_proxy_exposes_only_ui_tools_and_logs_hidden(tmp_path):
    g, out, log, _, _ = run_gate(tmp_path, gate_win.NullContext(app="notepad.exe", title="Untitled - Notepad", element_name="Text editor"), [], [])
    names = out[0]
    assert "PowerShell" not in names and "FileSystem" not in names and "BrandNewTool" not in names
    assert {"Click", "Type", "Snapshot", "App", "capture_view", "gate_status"} <= set(names)
    start = log[0]
    assert start["kind"] == "start" and {"PowerShell", "FileSystem", "Registry", "Process", "BrandNewTool"} <= set(start["hidden"])
    assert "BrandNewTool" in start["hidden_by_gate_filter"]
    click = next(t for t in g.tool_defs() if t["name"] == "Click")
    assert "label" not in click["inputSchema"]["properties"]


def test_proxy_flow(tmp_path):
    ctx = gate_win.NullContext(app="notepad.exe", title="Untitled - Notepad", element_name="Text editor", element_type="Edit")
    steps = [("Type", {"loc": [100, 200], "text": "hello"}),
             ("PowerShell", {"command": "whoami"}),
             ("Snapshot", {}),
             ("Click", {"loc": [1, 1], "fail": True}),
             ("Click", {"loc": [1, 1], "fail": True})]
    g, out, log, popups, fs = run_gate(tmp_path, ctx, [], steps)
    assert "did Type" in _text(out[1]) and not out[1]["is_error"]
    assert out[2]["is_error"] and "isn't offered" in _text(out[2])
    snap = _text(out[3])
    assert "Card number" not in snap and "Text editor" in snap and "hid 1 protected" in snap
    acts = [e for e in log if e.get("kind") == "action"]
    assert [a["tool"] for a in acts] == ["Type", "PowerShell", "Snapshot", "Click", "Click"]
    assert acts[0]["args"]["text"] == "hello" and acts[0]["zone"] == "auto" and acts[0]["by"] == "auto"
    assert acts[1]["zone"] == "hidden" and acts[3]["ok"] is False
    assert [k for k, _, _ in fs.queued] == ["error_or_glitch"]      # the repeat came inside the 5-minute gap
    skipped = [e for e in log if e.get("kind") == "report" and e.get("status") == "skipped"]
    assert skipped[0]["trigger"] == "repeat_fail" and "gap" in skipped[0]["detail"]
    assert all("hello" not in t for _, t, _ in fs.queued)


def test_proxy_confirm_and_never(tmp_path):
    ctx = gate_win.NullContext(app="OUTLOOK.EXE", title="Inbox - Outlook", element_name="Send", element_type="Button")
    steps = [("Click", {"loc": [5, 5]}), ("Click", {"loc": [5, 5]}), ("Click", {"loc": [5, 5]}), ("Click", {"loc": [5, 5]})]
    g, out, log, popups, fs = run_gate(tmp_path, ctx, ["block", "once", "app10"], steps)
    assert out[1]["is_error"] and "blocked" in _text(out[1])
    assert not out[2]["is_error"] and not out[3]["is_error"] and not out[4]["is_error"]
    assert len(popups) == 3                                   # the 4th rode the 10-minute grant
    acts = [e for e in log if e.get("kind") == "action"]
    assert [(a["decision"], a["by"]) for a in acts] == [("blocked", "user"), ("allowed", "user"), ("allowed", "user"), ("allowed", "grant")]
    assert acts[0]["shot"] and popups[0]["shot"]              # confirm screenshot, shown in the popup
    assert fs.queued[0][0] == "confirm_timeout" and fs.queued[0][2]   # with its picture

    ctx2 = gate_win.NullContext(app="Bitwarden.exe", title="Bitwarden", element_name="Copy password")
    g, out, log, popups, fs = run_gate(tmp_path / "b", ctx2, [], [("Click", {"loc": [5, 5]}), ("capture_view", {"note": "x"})])
    assert out[1]["is_error"] and "password manager" in _text(out[1]) and not popups
    assert fs.queued[0][0] == "never_block" and fs.queued[0][2] == []   # text only
    assert out[2]["is_error"] and "no picture" in _text(out[2])
    assert not any(e.get("shot") for e in log)


def test_proxy_pause(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps({"paused": True}))
    g, out, log, popups, fs = run_gate(tmp_path, gate_win.NullContext(app="notepad.exe"), [], [("Type", {"loc": [1, 1], "text": "x"}), ("gate_status", {})])
    assert out[1]["is_error"] and "paused" in _text(out[1])
    assert json.loads(_text(out[2]))["paused"] is True
    assert fs.queued == []                                    # pause refusals aren't reported


def test_stdio_server_end_to_end(tmp_path):
    """Claude desktop's view: start gate_mcp.py over stdio, list tools, make one call."""
    import os
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    env = dict(os.environ, NODACH_GATE_HOME=str(tmp_path), NODACH_GATE_QUIET="1",
               NODACH_GATE_CHILD_CMD=json.dumps([sys.executable, str(GATE / "tests" / "fake_wmcp.py")]))

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(GATE / "gate_mcp.py")], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                res = await s.call_tool("Wait", {"duration": 0})
                bad = await s.call_tool("PowerShell", {"command": "x"})
                return names, res, bad
    names, res, bad = asyncio.run(go())
    assert "Click" in names and "PowerShell" not in names and "gate_status" in names
    assert not res.is_error and bad.is_error
    assert (tmp_path / "zones.json").exists() and (tmp_path / "actions.jsonl").exists()


def test_clip_buffer_saves_gif_with_blank_frames(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    buf = gate_win.ClipBuffer(lambda a, t: None, {"clip_fps": 2, "clip_format": "gif"})
    buf.frames.append((1.0, pil.new("RGB", (64, 36), (0, 0, 0)), ""))
    buf.frames.append((1.5, gate_win.blank("password manager: app Bitwarden.exe"), "password manager"))
    buf.frames.append((2.0, pil.new("RGB", (64, 36), (255, 255, 255)), ""))
    path, n, n_blank, t0, t1 = buf.save(tmp_path, "c1")
    assert path.suffix == ".gif" and n == 3 and n_blank == 1 and (t0, t1) == (1.0, 2.0)
    assert pil.open(path).n_frames == 3
    buf.settings["clip_format"] = "frames"
    path, n, *_ = buf.save(tmp_path, "c2")
    assert path.is_dir() and len(list(path.glob("*.png"))) == 3
