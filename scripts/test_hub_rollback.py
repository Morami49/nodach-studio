"""Dry-run tests for the production rollback path and its notices (B-deploy-guarded-auto).
Run: python3 scripts/test_hub_rollback.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from hub_rollback import main, notice, plan, rollback, undo_command  # noqa: E402


def test_plan_redeploys_the_replaced_build():
    cmds, why = plan("1a2b3c4", "9f8e7d6")
    assert why == "" and cmds[2] == ["git", "worktree", "add", "--detach", "/tmp/hub-rollback", "1a2b3c4"]
    assert cmds[-1][:2] == ["sh", "-c"] and "BUILD_SHA=1a2b3c4" in cmds[-1][2] and "wrangler@" in cmds[-1][2] and " deploy" in cmds[-1][2]


def test_nothing_safe_to_roll_back_to():
    for prev in ("", "unknown", "nothing", "1a2b; rm -rf /", "XYZ1234"):
        cmds, why = plan(prev, "9f8e7d6")
        assert cmds == [] and "no recorded build" in why, prev
    cmds, why = plan("9f8e7d6", "9f8e7d6")
    assert cmds == [] and "just failed" in why


def test_dry_run_runs_nothing():
    ran, lines = [], []
    r = rollback("1a2b3c4", "9f8e7d6", "https://hub.example", dry_run=True, run=ran.append, smoke=lambda: 1 / 0, out=lines.append)
    assert r["result"] == "dry-run" and ran == [] and sum(l.startswith("$ ") for l in lines) == 4


def test_rollback_then_smoke():
    ran = []
    ok = rollback("1a2b3c4", "9f8e7d6", "u", run=ran.append, smoke=lambda: 0, out=lambda s: None)
    assert ok["result"] == "rolled-back" and len(ran) == 4
    bad = rollback("1a2b3c4", "9f8e7d6", "u", run=lambda c: None, smoke=lambda: 1, out=lambda s: None)
    assert bad["result"] == "failed" and "smoke test failed too" in bad["why"]

    def boom(c):
        if c[0] == "sh":
            raise RuntimeError("wrangler")
    assert rollback("1a2b3c4", "9f8e7d6", "u", run=boom, smoke=lambda: 0, out=lambda s: None)["result"] == "failed"
    assert rollback("", "9f8e7d6", "u", run=boom, smoke=lambda: 0, out=lambda s: None)["result"] == "skipped"


def test_cli_dry_run_exit_code():
    assert main(["rollback", "--prev", "1a2b3c4", "--build", "9f8e7d6", "--url", "u", "--dry-run"]) == 0
    assert main(["rollback", "--prev", "unknown", "--build", "9f8e7d6", "--url", "u", "--dry-run"]) == 1


def test_notices_are_urgent_and_in_nowed_queue():
    a = notice("ask", "abcdef1234", "https://gh/run/1", reasons="labelled HIGH RISK", approve_url="https://gh/wf")
    b = a["body"]
    assert a["kind"] == "item.create" and b["type"] == "notice" and b["meta"]["urgent"] is True and b["meta"]["queue"] == ["morami49"]
    assert b["id"] == "DEPLOY-ask-abcdef1" and "https://gh/wf" in b["body"] and "Run workflow" in b["body"] and "https://gh/run/1" in b["body"]
    r = notice("rollback", "abcdef1234", "https://gh/run/2", prev="1a2b3c4", result="rolled-back")["body"]
    assert "rolled back to 1a2b3c4" in r["title"] and r["meta"]["urgent"] and "gh workflow run hub-deploy.yml" in r["body"]
    f = notice("rollback", "abcdef1234", "https://gh/run/2", prev="", result="skipped")["body"]
    assert "rollback skipped" in f["title"]


def test_undo_command():
    assert undo_command("o/r", "1a2b3c4") == "gh workflow run hub-deploy.yml --repo o/r --ref main -f sha=1a2b3c4"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
