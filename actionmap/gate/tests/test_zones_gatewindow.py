import gate_core as core


def _z():
    import json, pathlib
    return json.loads((pathlib.Path(__file__).resolve().parents[1] / "zones.json").read_text(encoding="utf-8"))


def test_gate_window_in_front_allows_reads_and_launches_but_not_input():
    z = _z()
    ctx = {"app": "pythonw.exe", "title": "Nodach Gate", "element_name": ""}
    assert core.decide({"tool": "Snapshot", "args": {}}, ctx, z)[0] == "auto"
    zone, _ = core.decide({"tool": "App", "args": {"mode": "launch", "name": "Notepad"}}, dict(ctx, target_app="notepad", target_title="Notepad"), z)
    assert zone in ("auto", "confirm")
    assert core.decide({"tool": "Click", "args": {"loc": [10, 10]}}, ctx, z)[0] == "never"
    assert core.decide({"tool": "App", "args": {"mode": "switch", "name": "Nodach Gate"}}, ctx, z)[0] == "never"


def test_secret_file_windows_are_never_read():
    z = _z()
    ctx = {"app": "Notepad.exe", "title": "github-recovery-codes.txt - Notepad"}
    assert core.decide({"tool": "Snapshot", "args": {}}, ctx, z)[0] == "never"
    assert core.decide({"tool": "Type", "args": {"loc": [1, 1], "text": "x"}}, {"app": "Notepad.exe", "title": "Untitled - Notepad"}, z)[0] == "auto"


def test_upgrade_zones_adds_new_never_groups_and_keeps_edits():
    user = {"revision": 1, "never": [{"name": "password manager", "processes": ["MyVault"]}], "auto_apps": ["X"], "_changes": ["1: first"]}
    default = {"revision": 2, "never": [{"name": "password manager", "processes": ["Bitwarden"]}, {"name": "secrets in files"}],
               "_changes": ["1: first", {"revision": 2, "what": "secrets"}]}
    z, added = core.upgrade_zones(user, default)
    assert added == ["secrets in files"] and z["revision"] == 2
    assert z["never"][0]["processes"] == ["MyVault"] and z["auto_apps"] == ["X"]
    assert core.upgrade_zones(z, default)[1] == []


def test_secret_element_names_hidden_in_any_window():
    z = _z()
    snap = ('UI Tree:\ndesktop\n├── window "C:\\\\Users\\\\x\\\\Desktop\\\\Nodach - File Explorer"\n'
            '│   ├── (813,257) list item "client_secret_123.apps.googleusercontent.com.json"  [action: click]\n'
            '│   └── (10,10) list item "notes.txt"  [action: click]')
    out, _ = core.redact_snapshot(snap, z)
    assert "client_secret_123" not in out and "notes.txt" in out
    snap2 = 'UI Tree:\ndesktop\n├── window "C:\\\\Users\\\\x\\\\Desktop\\\\Nodach\\\\nodach_sec - File Explorer"\n│   └── (1,1) list item "anything.json"'
    out2, n = core.redact_snapshot(snap2, z)
    assert "anything.json" not in out2 and n == 1


def test_user_control_wait():
    assert core.user_control_wait('{"code": "USER_CONTROL", "status": {"wait_seconds": 9.9}}') == 9.9
    assert core.user_control_wait("Pressed shift+delete.") is None
