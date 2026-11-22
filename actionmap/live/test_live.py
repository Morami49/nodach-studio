"""Tests for the Nodach Live client's pure logic (no microphone, keyboard or network).

Run: python -m pytest actionmap/live/test_live.py   (or python actionmap/live/test_live.py)
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_core as core  # noqa: E402

T = dt.datetime(2026, 11, 19, 12, 30, 45, 123000, tzinfo=dt.timezone.utc)
OWNER = {"login": "Morami49", "id": "github:Morami49", "owner": True}
MEMBER = {"login": "email:0123456789abcdef", "id": "email:email:0123456789abcdef", "owner": False}
STUDIO_HTML = HERE.parent.parent / "studio" / "index.html"


def test_task_lines():
    assert core.parse_task("task: add a count badge") == {"kind": "task", "title": "add a count badge"}
    assert core.parse_task("  Build: the supplier page.") == {"kind": "build", "title": "the supplier page"}
    assert core.parse_task("Task, fix the login") == {"kind": "task", "title": "fix the login"}   # spoken colon written as a comma
    assert core.parse_task("TASK — rename the tab") == {"kind": "task", "title": "rename the tab"}
    assert core.parse_task("task:") is None
    assert core.parse_task("the task: is this") is None
    assert core.parse_task("taskbar is hidden") is None
    assert core.parse_task("building things") is None
    assert core.parse_task(None) is None


def test_task_rule_matches_studio():
    """The client and the Studio panel must agree on what a task line is."""
    if not STUDIO_HTML.exists():   # run from the unzipped nodach-live folder: Studio isn't next to it
        import pytest
        pytest.skip("studio/index.html not here (only in the repository)")
    html = STUDIO_HTML.read_text(encoding="utf-8")
    m = re.search(r"const LIVE_TASK = /(.+?)/i;", html)
    assert m, "Studio's LIVE_TASK not found"
    js = m.group(1).replace("\\u2013", "\u2013").replace("\\u2014", "\u2014").replace("[\\s\\S]", ".")
    py = re.compile(js, re.I | re.S)
    for s in ["task: a", "Build. b", "taskbar", "x task: y", "Task,c", "build —d"]:
        assert bool(py.match(s)) == bool(core.TASK_RE.match(s)), s


def test_clean_text_drops_whisper_noise():
    assert core.clean_text("  [BLANK_AUDIO]  hello   (music) there *coughs* ") == "hello there"
    assert core.clean_text("x" * 5000) == "x" * core.MAX_TEXT
    assert core.clean_text(None) == ""


def test_ids_and_time():
    assert core.iso(T) == "2026-11-19T12:30:45.123Z"
    assert re.fullmatch(r"LV-261119-123045-[0-9a-f]{4}", core.stamp("LV", T))
    d = core.doc_id()
    assert re.fullmatch(r"[a-z0-9]+", d) and not d.startswith("hub-") and len(d) <= 100
    assert core.doc_id() != d


def test_line_event_shape():
    p = {"schema": "pointer.v1", "kind": "pointer.desktop", "id": "PE-1", "app": "EXCEL.EXE", "element": {"type": "ButtonControl", "name": "Sort"}}
    e = core.line_event("LV-261119-123045-abcd", OWNER, " hello  there ", T, "voice", p)
    assert e["kind"] == "live.line" and e["item"] == "LV-261119-123045-abcd" and e["ts"] == "2026-11-19T12:30:45.123Z" and e["branch"] == "main"
    assert e["body"] == {"session": "LV-261119-123045-abcd", "speaker": "Nowed", "text": "hello there", "ts": e["ts"], "via": "voice", "point": p}
    assert e["author"] == {"id": "github:Morami49", "role": "owner", "label": "Nowed"}
    m = core.line_event("LV-x", MEMBER, "hi", T)
    assert m["body"]["speaker"] == "Member" and "point" not in m["body"] and m["author"]["role"] == "member"
    assert "@" not in json.dumps(m)   # never an address


def test_task_and_session_events():
    t = core.task_event("LV-x", "lvabc", {"kind": "build", "title": "a supplier page"}, OWNER, T)
    b = t["body"]
    assert t["kind"] == "item.create" and t["item"] is None
    assert b["type"] == "change" and b["status"] == "draft" and b["refs"] == ["LV-x"] and b["title"] == "Build: a supplier page"
    assert b["meta"] == {"kind": "build", "from_session": "LV-x", "from_line": "lvabc"} and b["id"].startswith("TK-261119-")
    s = core.session_event(OWNER, T)
    assert s["body"]["status"] == "live" and s["body"]["source"] == "live" and s["body"]["live"]["schema"] == "live.v1"
    assert s["body"]["live"]["responder"] is True and core.session_event(MEMBER, T)["body"]["live"]["responder"] is False
    assert s["body"]["id"].startswith("LV-261119-123045-")


def pe(pid, at, mode="point", **over):
    e = {"schema": "pointer.v1", "id": pid, "kind": "pointer.desktop", "at": at.isoformat(), "mode": mode,
         "element": {"type": "ButtonControl", "name": "Send", "automation_id": "x", "class_name": "Button", "rect": {}},
         "window": {"title": "Inbox - private subject", "class_name": "W"}, "process": {"pid": 9, "name": "claude.exe"},
         "screenshot": "C:\\Users\\me\\nodach-pointer\\shots\\x.png", "cursor": {"x": 1, "y": 2}, "note": "make it blue"}
    e.update(over)
    return json.dumps(e)


def test_latest_point_is_fresh_unused_and_a_point():
    lines = [pe("PE-old", T - dt.timedelta(minutes=5)), pe("PE-a", T - dt.timedelta(seconds=30)),
             pe("PE-batch", T - dt.timedelta(seconds=5), mode="batch-item"), "{half-written", pe("PE-b", T - dt.timedelta(seconds=10))]
    assert core.latest_point(lines, T)["id"] == "PE-b"
    assert core.latest_point(lines, T, used={"PE-b"})["id"] == "PE-a"
    assert core.latest_point(lines, T, used={"PE-a", "PE-b"}) is None
    assert core.latest_point([], T) is None


def test_shareable_point_keeps_private_things_on_the_pc():
    p = core.shareable_point(json.loads(pe("PE-a", T)))
    assert p == {"schema": "pointer.v1", "kind": "pointer.desktop", "id": "PE-a", "app": "claude.exe",
                 "element": {"type": "ButtonControl", "name": "Send"}, "note": "make it blue"}
    text = json.dumps(p)
    for leak in ("private subject", "shots", "cursor", "automation_id", "class_name"):
        assert leak not in text
    assert core.shareable_point({"schema": "other"}) is None and core.shareable_point(None) is None


def test_latest_live_session():
    def row(kind, item, body, who="github:Morami49", ts="2026-11-19T10:00:00.000Z"):
        return {"doc_id": "d" + ts + kind, "event": {"kind": kind, "item": item, "body": body, "ts": ts, "author": {"id": who}}}
    docs = [row("item.create", None, {"id": "LV-1", "source": "live", "status": "live"}, ts="2026-11-19T09:00:00.000Z"),
            row("item.create", None, {"id": "LV-2", "source": "live", "status": "live"}, ts="2026-11-19T10:00:00.000Z"),
            row("status", "LV-2", {"status": "open"}, ts="2026-11-19T11:00:00.000Z"),
            row("item.create", None, {"id": "LV-3", "source": "live", "status": "live"}, who="github:someone", ts="2026-11-19T12:00:00.000Z"),
            row("item.create", None, {"id": "SH-1", "source": "show", "status": "live"}, ts="2026-11-19T12:00:00.000Z")]
    assert core.latest_live_session(docs, "github:Morami49") == "LV-1"
    assert core.latest_live_session(docs[:1] + [row("status", "LV-1", {"status": "open"})], "github:Morami49") is None
    assert core.latest_live_session([], "github:Morami49") is None


def test_segments_cut_phrases_on_pauses():
    loud, quiet = 0.1, 0.0
    f = 30
    levels = [quiet] * 5 + [loud] * 20 + [quiet] * 30 + [loud] * 3 + [quiet] * 30 + [loud] * 15
    segs = core.segments(levels, 0.05, f, silence_ms=600, min_ms=300)
    assert segs == [(5, 25), (88, 103)]   # the 3-frame click (90 ms) is dropped; the open phrase at the end is kept
    assert core.segments([quiet] * 50, 0.05, f) == []


def test_client_imports_without_audio_packages():
    import live_client  # noqa: F401  (optional packages are guarded)
    assert live_client.core.VERSION == core.VERSION


def test_client_posts_line_then_draft_to_the_studio_api(tmp_path, monkeypatch, capsys):
    """Against a stand-in hub: the sign-in goes as the session cookie with the Studio header, a task line is posted
    as live.line and then a draft that points at that line's doc id, and the token is never printed."""
    import http.server
    import threading
    import live_client
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            got.append({"path": self.path, "cookie": self.headers.get("cookie"), "csrf": self.headers.get("x-nodach-studio"),
                        "body": json.loads(self.rfile.read(n))})
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b'{"imported":1}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    hub = live_client.Hub(f"http://127.0.0.1:{srv.server_address[1]}", "SECRET-TOKEN-123")
    live = live_client.Live(hub, OWNER, "LV-x")
    live.say("task: add the badge", "voice")
    live.out.join()
    srv.shutdown()
    assert [g["path"] for g in got] == ["/studio/api/events", "/studio/api/events"]
    assert all(g["cookie"] == "nodach_session=SECRET-TOKEN-123" and g["csrf"] == "1" for g in got)
    line, draft = got[0]["body"], got[1]["body"]
    assert line["event"]["kind"] == "live.line" and line["event"]["body"]["text"] == "task: add the badge"
    assert draft["event"]["body"]["meta"]["from_line"] == line["doc_id"] and draft["event"]["body"]["status"] == "draft"
    out = capsys.readouterr().out + (tmp_path / "lines.jsonl").read_text()
    assert "SECRET-TOKEN-123" not in out and "→ draft TK-" in out



# ---------------------------------------------------------------- 0.2.2: never write into a session that has ended
def _sess(sid, started, status="live", ended=False):
    rows = [_ev("item.create", None, {"id": sid, "source": "live", "status": "live", "live": {"started": started}}, ts=started)]
    if ended:
        rows.append(_ev("status", sid, {"status": "open"}, ts=started[:-5] + "9.000Z"))
    return rows


def test_send_target_rules():
    rows = lambda *xs: [{"id": i, "state": st} for i, st in xs]
    assert core.send_target(rows(("LV-a", "live")), "LV-a")["action"] == "send"
    assert core.send_target(rows(("LV-b", "live")), "LV-gone")["action"] == "send"          # not listed: can't tell
    d = core.send_target(rows(("LV-a", "open"), ("LV-b", "live")), "LV-a")
    assert d["action"] == "switch" and d["to"] == "LV-b" and d["state"] == "open"
    d = core.send_target(rows(("LV-a", "open"), ("LV-b", "live"), ("LV-c", "live")), "LV-a")
    assert d["action"] == "ask" and d["to"] is None and d["live"] == ["LV-b", "LV-c"]
    assert core.send_target(rows(("LV-a", "open")), "LV-a")["action"] == "ask"


def test_live_switches_to_the_one_live_session_after_its_own_ended(tmp_path, monkeypatch):
    """F-live-stale-session (20 Nov): LV-...4slr ended at 18:21 but still got lines at 18:35 and 18:37."""
    import live_client
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    docs = _sess("LV-261120-180852-4slr", "2026-11-20T18:08:52.000Z", ended=True) + _sess("LV-261120-183000-newx", "2026-11-20T18:30:00.000Z")
    srv, got, url = _stand_in_hub(200, docs)
    rows, switched = [], []
    live = live_client.Live(live_client.Hub(url, GOOD_TOKEN), OWNER, "LV-261120-180852-4slr",
                            report=lambda *a: rows.append(a), on_switch=lambda o, n: switched.append((o, n)))
    live.say("hello again", "typed")
    live.say("second line", "typed")
    live.out.join(); live.close(); srv.shutdown()
    posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
    assert [p["item"] for p in posts] == ["LV-261120-183000-newx"] * 2
    assert switched == [("LV-261120-180852-4slr", "LV-261120-183000-newx")] and live.session == "LV-261120-183000-newx"
    assert rows[0][0] == "session" and rows[0][1] == "info" and "switched to LV-261120-183000-newx" in rows[0][2]
    assert [r[1] for r in rows if r[0] == "send"] == ["ok", "ok"] and "switched from" in [r for r in rows if r[0] == "send"][0][2]


def test_live_holds_lines_and_asks_when_several_are_live(tmp_path, monkeypatch):
    import live_client
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    docs = (_sess("LV-old", "2026-11-20T18:00:00.000Z", ended=True) + _sess("LV-b", "2026-11-20T18:30:00.000Z")
            + _sess("LV-c", "2026-11-20T18:31:00.000Z"))
    srv, got, url = _stand_in_hub(200, docs)
    rows, asked = [], []
    hub = live_client.Hub(url, GOOD_TOKEN)
    live = live_client.Live(hub, OWNER, "LV-old", report=lambda *a: rows.append(a), on_ask=lambda o, l: asked.append((o, l)))
    live.say("where does this go", "typed")
    live.out.join()
    assert not [g for g in got if g["method"] == "POST"]          # nothing written into the ended session
    assert asked and asked[0][0] == "LV-old" and sorted(asked[0][1]) == ["LV-b", "LV-c"]
    assert rows[-1][0] == "session" and rows[-1][1] == "failed" and "pick one" in rows[-1][2]
    held = live.take_held(); live.close()
    assert len(held) == 1
    nxt = live_client.Live(hub, OWNER, "LV-c", report=lambda *a: rows.append(a))
    nxt.resend(held); nxt.out.join(); nxt.close(); srv.shutdown()
    posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
    assert [(p["item"], p["body"]["text"]) for p in posts] == [("LV-c", "where does this go")]
    assert "lines.jsonl" in [p.name for p in tmp_path.iterdir()] and '"held": true' in (tmp_path / "lines.jsonl").read_text()

if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__code__.co_argcount == 0:
            fn(); n += 1; print("ok", name)
    print(f"{n} passed")


def test_clean_token_accepts_real_shapes_and_rejects_bad_pastes():
    sig = "a" * 64
    good = f"Morami49.1795000000000.{sig}"
    assert core.clean_token(good) == good
    assert core.clean_token(f'  "{good}"  ') == good
    assert core.clean_token(f"nodach_session={good};") == good
    assert core.clean_token("\x16") is None          # Ctrl+V in a Windows console
    assert core.clean_token("") is None
    assert core.clean_token(f"Morami49.17950.{sig}") is None
    assert core.clean_token(None) is None


def test_local_model_dir(tmp_path):
    assert core.local_model_dir([tmp_path], "base") is None
    d = tmp_path / "faster-whisper-base"
    d.mkdir()
    for f in core.MODEL_FILES[:-1]:
        (d / f).write_text("x")
    assert core.local_model_dir([tmp_path], "base") is None   # incomplete folder is ignored
    (d / core.MODEL_FILES[-1]).write_text("x")
    assert core.local_model_dir([tmp_path / "nope", tmp_path], "base") == d


# ---------------------------------------------------------------- 0.2.0: the window's pure logic
GOOD_TOKEN = "Morami49.1795000000000." + "b" * 64


def test_version_and_hotkey_does_not_clash():
    assert core.VERSION == "0.2.2"
    assert core.GUI_HOTKEY == "<ctrl>+<alt>+j" and core.hotkey_clash(core.GUI_HOTKEY) is None
    assert "Claude" in core.hotkey_clash("<ctrl>+<alt>+<space>")
    for k in ("m", "b", "q", "p", "l"):
        assert core.hotkey_clash(f"<ctrl>+<alt>+{k}")
    import live_client
    assert "<ctrl>+<alt>+<space>" not in live_client.HOTKEYS and all(core.hotkey_clash(k) is None or v != "talk" for k, v in live_client.HOTKEYS.items())


def test_activity_rows_and_details():
    r = core.activity_row("send", "ok", core.send_detail(200, 42, point=True, draft="TK-1"), T)
    assert r["step"] == "send" and r["outcome"] == "ok" and r["ts"] == core.iso(T) and len(r["time"]) == 8
    assert r["detail"] == "hub 200 · 42 chars · with pointer point · draft TK-1"
    assert core.activity_row("weird", "maybe", "", T)["step"] == "?weird"
    assert core.activity_row("send", "failed", "x" * 500, T)["detail"] == "x" * 160
    assert core.send_detail(0, 0, error="boom") == "hub unreachable · no text · boom"
    assert core.record_detail(4.24, 0.312) == "4.2 s recorded, peak level 0.31"
    assert core.transcribe_detail(2.0, 17, 0.84) == "2.0 s of audio -> 17 chars in 0.8 s"
    line = core.format_row(r)
    assert line.startswith(r["time"]) and "send" in line and line.endswith("draft TK-1")


def test_level_meter_and_talk_label():
    assert core.level_fraction(0) == 0 and core.level_fraction(None) == 0
    assert core.level_fraction(1.0) == 1.0 and core.level_fraction(5) == 1.0
    assert abs(core.level_fraction(0.001) - 0.0) < 1e-9          # -60 dB
    assert 0.4 < core.level_fraction(0.03) < 0.6                 # about -30 dB, a normal voice
    assert core.talk_label(False) == "● Talk"
    assert core.talk_label(True, 7.9) == "■ Stop and send  0:07"
    assert core.talk_label(True, 75, always=True) == "■ Stop listening  1:15"


def test_input_devices_like_windows():
    hostapis = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
    devs = [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
        {"name": "Microphone (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0},
        {"name": "Headset (Jabra)  ", "max_input_channels": 1, "hostapi": 0},
        {"name": "Speakers (Realtek(R) Audio)", "max_input_channels": 0, "hostapi": 0},
        {"name": "Microphone (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 1},
        {"name": "Microphone (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 2},
    ]
    out = core.input_devices(devs, 1, hostapis)
    assert [d["label"] for d in out] == ["Microphone (Realtek(R) Audio)", "Headset (Jabra)"]
    assert out[0] == {"index": 1, "label": "Microphone (Realtek(R) Audio)", "default": True, "kind": "mic"}
    # no default known: every microphone, sound system named when they differ, duplicates dropped
    out2 = core.input_devices(devs, -1, hostapis)
    assert len(out2) == 2 and not any(d["default"] for d in out2) and "(MME)" in out2[0]["label"]
    assert core.input_devices([], 0) == [] and core.input_devices([{"name": "Out", "max_input_channels": 0}], 0) == []


def _ev(kind, item, body, who="github:Morami49", ts="2026-11-19T10:00:00.000Z"):
    return {"doc_id": "d" + ts + kind, "event": {"kind": kind, "item": item, "body": body, "ts": ts, "author": {"id": who}}}


def test_session_list_live_first_newest_first():
    docs = [_ev("item.create", None, {"id": "LV-a", "source": "live", "status": "live", "live": {"started": "2026-11-18T09:00:00.000Z"}}, ts="2026-11-18T09:00:00.000Z"),
            _ev("item.create", None, {"id": "LV-b", "source": "live", "status": "live", "live": {"started": "2026-11-19T09:00:00.000Z"}}, ts="2026-11-19T09:00:00.000Z"),
            _ev("item.create", None, {"id": "LV-c", "source": "live", "status": "live", "live": {"started": "2026-11-20T09:00:00.000Z"}}, ts="2026-11-20T09:00:00.000Z"),
            _ev("status", "LV-c", {"status": "open"}, ts="2026-11-20T10:00:00.000Z"),
            _ev("item.create", None, {"id": "LV-other", "source": "live", "status": "live", "live": {"audience": ["github:x"]}}, who="github:x", ts="2026-11-20T11:00:00.000Z"),
            _ev("item.create", None, {"id": "SH-1", "source": "show", "status": "live"})]
    rows = core.session_list(docs, "github:Morami49", owner=True)
    assert [(r["id"], r["state"]) for r in rows] == [("LV-other", "live"), ("LV-b", "live"), ("LV-a", "live"), ("LV-c", "open")]
    assert rows[1]["label"] == "LV-b · live"
    member = core.session_list(docs, "github:Morami49", owner=False)
    assert "LV-other" not in [r["id"] for r in member]
    assert core.session_list([], "x") == []


def test_scrub_keeps_secrets_and_outside_paths_out():
    s = core.scrub(f"token {GOOD_TOKEN} nodach_session=x a.person@example.com "
                   r"C:\Users\Al\Downloads\nodach-live\models\faster-whisper-base and C:\Users\Al\Documents\cv.docx "
                   "/home/al/nodach-live/lines.jsonl " + "f" * 64)
    assert GOOD_TOKEN not in s and "b" * 64 not in s and "example.com" not in s and "nodach_session" not in s
    assert "nodach-live/models/faster-whisper-base" in s and "Documents" not in s and "Users" not in s and "[path]" in s
    assert "nodach-live/lines.jsonl" in s and "f" * 64 not in s


def test_report_has_the_facts_and_nothing_private():
    rows = [core.activity_row("record", "ok", f"row {i}", T) for i in range(40)]
    rows.append(core.activity_row("send", "failed", f"hub 401 · {GOOD_TOKEN} · C:\\Users\\Al\\secret\\x.txt", T))
    info = {"version": "0.2.0", "os": "Windows-11-10.0.22631-SP0", "python": "3.12.4", "hub": "staging (https://staging.nodach.com)",
            "role": "owner", "session": "LV-261120-101010-abcd", "mic": "Microphone (Realtek(R) Audio)",
            "model": r"base · local folder C:\Users\Al\Downloads\nodach-live\models\faster-whisper-base", "model_status": "ready",
            "settings": {"keep_listening": False, "attach_point": True, "speech_level": 0.015}}
    rep = core.build_report(info, rows)
    for want in ("client 0.2.0", "Windows-11", "staging", "signed in: owner", "LV-261120-101010-abcd", "Realtek",
                 "nodach-live/models/faster-whisper-base", "ready", "keep_listening=off", "attach_point=on", "speech_level=0.015",
                 "last 30 rows, 1 failed"):
        assert want in rep, want
    assert rep.count("\n") == 10 + 30 and "row 10" not in rep.split("detail\n", 1)[1].split("\n")[0] and "row 11" in rep
    assert GOOD_TOKEN not in rep and "secret" not in rep and "Users" not in rep and "@" not in rep
    empty = core.build_report({}, [])
    assert "not signed in" in empty and "(nothing yet)" in empty


def test_report_event_is_a_comment_on_the_session():
    e = core.report_event("LV-x", OWNER, "report text " + GOOD_TOKEN, T)
    assert e["kind"] == "comment" and e["item"] == "LV-x" and e["ts"] == core.iso(T)
    assert e["body"] == {"text": "report text [sign-in value]", "report": True}
    assert e["author"]["role"] == "owner" and "@" not in json.dumps(e)


# ---------------------------------------------------------------- 0.2.0: shared client pieces (stand-in hub)
def _stand_in_hub(status=200, sessions=()):
    import http.server
    import threading
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_GET(self):
            got.append({"method": "GET", "path": self.path, "cookie": self.headers.get("cookie")})
            if status != 200:
                return self._send(status, {"error": "no"})
            if self.path.startswith("/studio/api/me"):
                return self._send(200, {"id": "github:Morami49", "login": "Morami49", "owner": True})
            ev = list(sessions)
            return self._send(200, {"events": ev, "next": len(ev), "total": len(ev)})

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            got.append({"method": "POST", "path": self.path, "cookie": self.headers.get("cookie"), "body": json.loads(self.rfile.read(n))})
            self._send(status, {"imported": 1} if status == 200 else {"error": "signed out"})

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got, f"http://127.0.0.1:{srv.server_address[1]}"


def test_live_reports_each_send_with_the_hub_status(tmp_path, monkeypatch):
    import live_client
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    for status, outcome in ((200, "ok"), (401, "failed")):
        srv, got, url = _stand_in_hub(status)
        rows = []
        hub = live_client.Hub(url, GOOD_TOKEN)
        assert GOOD_TOKEN not in repr(hub)
        live = live_client.Live(hub, OWNER, "LV-x", report=lambda *a: rows.append(a))
        live.say("hello there", "typed")
        live.out.join()
        live.close()
        srv.shutdown()
        assert rows and rows[0][0] == "send" and rows[0][1] == outcome and f"hub {status}" in rows[0][2], rows
        assert GOOD_TOKEN not in json.dumps(rows)
    try:
        live_client.Hub("http://127.0.0.1:9", GOOD_TOKEN).me()
    except live_client.HubError as e:
        assert e.code == 0 and GOOD_TOKEN not in str(e)
    else:
        raise AssertionError("expected HubError")


def test_saved_sign_in_round_trip(tmp_path, monkeypatch):
    import live_client
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.delenv("NODACH_LIVE_TOKEN", raising=False)
    url = "https://staging.nodach.com"
    assert live_client.stored_token(url) is None
    live_client.save_token(url, GOOD_TOKEN)
    assert live_client.stored_token(url) == GOOD_TOKEN
    assert [p.name for p in tmp_path.iterdir()] == ["session-staging.nodach.com.txt"]
    live_client.forget_token(url)
    assert live_client.stored_token(url) is None
    monkeypatch.setenv("NODACH_LIVE_TOKEN", f'"{GOOD_TOKEN}"')
    assert live_client.stored_token(url) == GOOD_TOKEN


# ---------------------------------------------------------------- 0.2.0: the window itself (only where a display exists)
def test_gui_end_to_end_against_a_stand_in_hub(tmp_path, monkeypatch):
    """Skipped without tkinter or a display (CI); runs under xvfb-run locally. Signs in from a saved value, lists
    sessions, picks the live one, sends a typed line, copies a report and sends it as a comment."""
    import os
    import time as _t
    import pytest
    tk = pytest.importorskip("tkinter")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("no display")
    import live_client
    import live_gui
    sessions = [_ev("item.create", None, {"id": "LV-261120-090000-aaaa", "source": "live", "status": "live",
                                          "live": {"started": "2026-11-20T09:00:00.000Z"}}, ts="2026-11-20T09:00:00.000Z")]
    srv, got, url = _stand_in_hub(200, sessions)
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    monkeypatch.setitem(core.HUBS, "staging", url)
    monkeypatch.setenv("NODACH_LIVE_TOKEN", GOOD_TOKEN)
    root = tk.Tk()
    app = live_gui.App(root, "staging", "base")

    def pump(cond, secs=10):
        end = _t.monotonic() + secs
        while _t.monotonic() < end:
            root.update()
            if cond():
                return True
            _t.sleep(0.02)
        return False
    try:
        assert pump(lambda: app.live is not None), [r["detail"] for r in app.rows]
        assert app.who_var.get() == "Signed in as owner" and app.session_var.get() == "LV-261120-090000-aaaa · live"
        assert str(app.end_btn["state"]) != "disabled"
        app.text_var.set("hello from the window")
        app.send_typed()
        assert pump(lambda: any(r["step"] == "send" for r in app.rows))
        sent = [r for r in app.rows if r["step"] == "send"][-1]
        assert sent["outcome"] == "ok" and "hub 200" in sent["detail"]
        app.toggle_talk()   # no speech model here: a clear failed row, not silence
        assert app.rows[-1]["step"] == "record" and app.rows[-1]["outcome"] == "failed"
        # 0.2.2: that failure sends an automatic report by itself (on by default)
        assert pump(lambda: any(r["step"] == "report" and "auto-report sent (failure)" in r["detail"] for r in app.rows))
        app.copy_report()
        rep = root.clipboard_get()
        assert f"client {core.VERSION}" in rep and "LV-261120-090000-aaaa" in rep and "signed in: owner" in rep and GOOD_TOKEN not in rep
        app.send_report()
        assert pump(lambda: any(r["step"] == "report" and "comment" in r["detail"] for r in app.rows))
        posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
        assert [p["kind"] for p in posts] == ["live.line", "comment", "comment"]
        assert posts[1]["body"]["auto"] == "failure" and posts[1]["body"]["text"].startswith("Automatic report (failure): record failed")
        assert posts[2]["body"]["report"] is True and "auto" not in posts[2]["body"] and posts[2]["item"] == "LV-261120-090000-aaaa"
        assert GOOD_TOKEN not in json.dumps(posts)
        assert GOOD_TOKEN not in (tmp_path / "activity.jsonl").read_text()
        assert os.listdir(tmp_path) and "session-" not in " ".join(os.listdir(tmp_path))   # env sign-in isn't written to disk
    finally:
        app._close()
        try:
            pump(lambda: False, 0.5)
        except tk.TclError:
            pass
        srv.shutdown()


def test_mic_ranking_and_find_my_mic():
    devs = [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
        {"name": "Microphone (DroidCam Audio)", "max_input_channels": 1, "hostapi": 0},
        {"name": "Stereo Mix (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0},
        {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0},
    ]
    out = core.input_devices(devs, default_index=1)
    assert [d["kind"] for d in out] == ["mic", "virtual", "loopback"]
    assert out[0]["label"].startswith("Microphone Array")
    assert "not your voice" in out[2]["label"]
    assert core.pick_loudest([(out[2], 0.5), (out[1], 0.0), (out[0], 0.1)]) is out[0]
    assert core.pick_loudest([(out[2], 0.5)]) is None


def test_gui_highlights_the_session_list_and_sends_held_lines_after_a_pick(tmp_path, monkeypatch):
    """0.2.2, the window: its session ended and two are live, so a typed line is held, an activity row says why, the
    session list turns yellow with a note under it; picking a live session sends the held line there."""
    import time as _t
    import pytest
    tk = pytest.importorskip("tkinter")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("no display")
    import live_client
    import live_gui
    docs = (_sess("LV-261120-180000-oldx", "2026-11-20T18:00:00.000Z", ended=True) + _sess("LV-261120-183000-bbbb", "2026-11-20T18:30:00.000Z")
            + _sess("LV-261120-183100-cccc", "2026-11-20T18:31:00.000Z"))
    srv, got, url = _stand_in_hub(200, docs)
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    monkeypatch.setitem(core.HUBS, "staging", url)
    monkeypatch.setenv("NODACH_LIVE_TOKEN", GOOD_TOKEN)
    root = tk.Tk()
    app = live_gui.App(root, "staging", "base")

    def pump(cond, secs=10):
        end = _t.monotonic() + secs
        while _t.monotonic() < end:
            root.update()
            if cond():
                return True
            _t.sleep(0.02)
        return False
    try:
        assert pump(lambda: app.live is not None)
        app.auto_var.set(False)                           # this test counts posts; automatic reports have their own
        app._set_live("LV-261120-180000-oldx")          # as if it was picked before it ended
        app.text_var.set("is this still going through")
        app.send_typed()
        assert pump(lambda: bool(app.pick_var.get())), [r["detail"] for r in app.rows]
        assert str(app.session_box.cget("style")) == "Pick.TCombobox" and "has ended" in app.pick_var.get()
        assert any(r["step"] == "session" and r["outcome"] == "failed" and "pick one" in r["detail"] for r in app.rows)
        assert not [g for g in got if g["method"] == "POST"]
        app.session_var.set("LV-261120-183100-cccc · live")
        app._session_picked()
        assert pump(lambda: any(g["method"] == "POST" for g in got))
        posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
        assert [(p["item"], p["body"]["text"]) for p in posts] == [("LV-261120-183100-cccc", "is this still going through")]
        assert app.pick_var.get() == "" and str(app.session_box.cget("style")) == "TCombobox"
    finally:
        app._close()
        try:
            pump(lambda: False, 0.5)
        except tk.TclError:
            pass
        srv.shutdown()


def test_auto_report_rules():
    now = T
    fail = {"step": "send", "outcome": "failed", "detail": "hub 401"}
    assert core.auto_report_due(fail, None, now)
    assert not core.auto_report_due(fail, now - dt.timedelta(seconds=299), now)          # at most once per 5 minutes
    assert core.auto_report_due(fail, now - dt.timedelta(seconds=300), now)
    for step in ("record", "transcribe", "sign-in", "session"):
        assert core.auto_report_due({"step": step, "outcome": "failed"}, None, now)
    assert not core.auto_report_due({"step": "send", "outcome": "ok"}, None, now)
    assert not core.auto_report_due({"step": "report", "outcome": "failed"}, None, now)  # its own failures never loop
    assert not core.auto_report_due({"step": "mic", "outcome": "failed"}, None, now)
    assert not core.auto_report_due({"step": "send", "outcome": "empty speech"}, None, now)
    txt = core.auto_report_text("REPORT BODY\n", "failure", {"step": "send", "outcome": "failed", "detail": f"x {GOOD_TOKEN} a@b.co"})
    assert txt.startswith("Automatic report (failure): send failed") and txt.endswith("REPORT BODY\n")
    assert GOOD_TOKEN not in txt and "a@b.co" not in txt
    assert not core.retry_due([], None, now) and core.retry_due([1], None, now)
    assert not core.retry_due([1], now - dt.timedelta(seconds=30), now) and core.retry_due([1], now - dt.timedelta(seconds=60), now)
    e = core.report_event("LV-x", OWNER, "t", T, auto="session ended")
    assert e["body"] == {"text": "t", "report": True, "auto": "session ended"}
    assert "auto" not in core.report_event("LV-x", OWNER, "t", T)["body"]


def test_gui_auto_report_queues_when_the_send_fails_and_retries(tmp_path, monkeypatch):
    import time as _t
    import pytest
    tk = pytest.importorskip("tkinter")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("no display")
    import live_client
    import live_gui
    sessions = _sess("LV-261120-090000-aaaa", "2026-11-20T09:00:00.000Z")
    srv, got, url = _stand_in_hub(200, sessions)
    monkeypatch.setattr(live_client, "HOME", tmp_path)
    monkeypatch.setattr(live_client, "POINTER_EVENTS", tmp_path / "none.jsonl")
    monkeypatch.setitem(core.HUBS, "staging", url)
    monkeypatch.setenv("NODACH_LIVE_TOKEN", GOOD_TOKEN)
    root = tk.Tk()
    app = live_gui.App(root, "staging", "base")

    def pump(cond, secs=10):
        end = _t.monotonic() + secs
        while _t.monotonic() < end:
            root.update()
            if cond():
                return True
            _t.sleep(0.02)
        return False
    try:
        assert pump(lambda: app.live is not None)
        real = app.hub.post
        calls = {"n": 0}

        def flaky(ev, did=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise live_client.HubError("hub answered 503", 503)
            return real(ev, did)
        app.hub.post = flaky
        app.row("send", "failed", "hub 503 · test")
        assert pump(lambda: any("auto-report queued (failure)" in r["detail"] for r in app.rows))
        app.row("send", "failed", "again within 5 minutes")          # rate limit: no second report
        app.auto_try = core.utcnow() - dt.timedelta(seconds=core.AUTO_RETRY_S)
        assert pump(lambda: any("auto-report sent (failure)" in r["detail"] for r in app.rows))
        pump(lambda: False, 0.4)
        posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
        assert len(posts) == 1 and posts[0]["body"]["auto"] == "failure" and "hub 503 · test" in posts[0]["body"]["text"]
        log = (tmp_path / "auto-reports.jsonl").read_text()
        assert '"what": "queued"' in log and '"what": "sent"' in log and GOOD_TOKEN not in log
        # ending the session sends a summary
        monkeypatch.setattr(live_gui.messagebox, "askyesno", lambda *a, **k: True)
        app.end_session()
        assert pump(lambda: any("auto-report sent (session ended)" in r["detail"] for r in app.rows))
        posts = [g["body"]["event"] for g in got if g["method"] == "POST"]
        assert posts[-1]["kind"] == "comment" and posts[-1]["body"]["auto"] == "session ended"
    finally:
        app._close()
        try:
            pump(lambda: False, 0.5)
        except tk.TclError:
            pass
        srv.shutdown()
