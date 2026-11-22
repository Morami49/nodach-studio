import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import restore_drill as d  # noqa: E402


def fake_hub(n: int, page_size: int = 3, grow_after_first: int = 0):
    """A /status + /pull stand-in holding n events; optionally gains events while being pulled."""
    state = {"n": n, "calls": 0}

    def get(url: str) -> dict:
        if url.endswith("/status"):
            return {"events": state["n"], "verified": True}
        state["calls"] += 1
        if state["calls"] == 2:
            state["n"] += grow_after_first
        after = int(url.split("after=")[1].split("&")[0])
        end = min(after + page_size, state["n"])
        return {"events": [{"id": f"e{i}"} for i in range(after, end)], "next": end, "total": state["n"]}
    return get


def test_export_pulls_every_page():
    out = d.export("https://h", fake_hub(7))
    assert out["source"] == {"events": 7, "verified": True}
    assert [e["id"] for e in out["events"]] == [f"e{i}" for i in range(7)]


def test_export_keeps_up_with_writes_during_the_pull():
    out = d.export("https://h", fake_hub(4, grow_after_first=2))
    assert len(out["events"]) == 6 and out["source"]["events"] == 4


def test_empty_hub():
    assert d.export("https://h", fake_hub(0))["events"] == []


def test_stuck_cursor_is_an_error():
    def get(url):
        return {"events": 1, "verified": True} if url.endswith("/status") else {"events": [{"id": "x"}], "next": 0, "total": 5}
    try:
        d.export("https://h", get)
    except RuntimeError as e:
        assert "did not move" in str(e)
    else:
        raise AssertionError("expected an error")


def test_summary_has_counts_not_content():
    r = {"ok": False, "source_events": 9, "exported": 8, "rebuilt": 8, "refused": 0, "verified": True,
         "resend_added": 0, "export_set_sha256": "ab" * 32, "problems": ["export has 8 events, the source reported 9"]}
    s = d.summary("staging", r)
    assert "FAILED" in s and "source reported 9" in s and "abababab" in s


def test_no_token_skips():
    saved = os.environ.pop("HUB_TOKEN", None)
    try:
        assert d.main(["x", "staging", "https://h"]) == 2
    finally:
        if saved is not None:
            os.environ["HUB_TOKEN"] = saved


def test_end_to_end_with_the_engine():
    """The real rebuild binary on an empty export: nothing lost, nothing refused (skipped without cargo)."""
    if not shutil.which("cargo"):
        print("skip: no cargo")
        return
    r = d.rebuild({"source": {"events": 0, "verified": True}, "events": []})
    assert r["ok"] and r["rebuilt"] == 0
    json.dumps(r)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
