"""Tests for policy resolution (Q101 A). Run: python3 scripts/test_policy.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from policy import deploy_mode, drift_writes, fold, guarded_reasons, merged_change, resolve  # noqa: E402
import json  # noqa: E402

K = "SET-policy-branch-cleanup"


def d(doc, ts, kind, item=None, **body):
    return {"doc_id": doc, "event": {"ts": ts, "kind": kind, "item": item, "body": body}}


def test_fold_create_edit_and_duplicate():
    docs = [d("b", "2026-11-08T10:01:00Z", "item.edit", K, value="off"),
            d("a", "2026-11-08T10:00:00Z", "item.create", None, id=K, value="ask"),
            d("c", "2026-11-08T10:02:00Z", "item.create", None, id=K, value="auto"),   # duplicate create: ignored
            d("e", "2026-11-08T10:03:00Z", "comment", K, text="x")]
    assert fold(docs, K) == "off"
    assert fold(docs + [d("f", "2026-11-08T10:04:00Z", "item.edit", K, value="auto")], K) == "auto"
    assert fold([], K) is None


def test_resolve_studio_wins_and_flags_drift():
    assert resolve("auto", "auto", "ask") == {"effective": "auto", "studio": "auto", "platform": "auto", "drift": "no"}
    assert resolve("off", "auto", "ask")["effective"] == "off" and resolve("off", "auto", "ask")["drift"] == "yes"
    assert resolve(None, "auto", "ask") == {"effective": "auto", "studio": "unknown", "platform": "auto", "drift": "no"}
    assert resolve(None, "", "ask")["effective"] == "ask"
    assert resolve("auto", "", "ask")["drift"] == "yes"


def test_drift_writes_once_per_drift():
    first = drift_writes(K, "auto", "ask", set())
    assert [w["kind"] for w in first] == ["item.create", "comment"]
    assert first[0]["body"]["id"] == "DRIFT-" + K and first[0]["body"]["type"] == "finding"
    later = drift_writes(K, "auto", "ask", {"DRIFT-" + K})
    assert [w["kind"] for w in later] == ["comment"] and later[0]["idempotency_key"] == first[1]["idempotency_key"]
    assert drift_writes(K, "off", "ask", {"DRIFT-" + K})[0]["idempotency_key"] != first[1]["idempotency_key"]


# B-deploy-guarded-auto: auto stays the default; guarded changes switch that deploy to ask
ORDINARY = ["studio/index.html", "studio/tests/scenarios.py", "scripts/smoke_hub.py", "hub/src/lib.rs"]


def test_ordinary_merge_deploys_automatically():
    d = deploy_mode("auto", "workflow_run", ["enhancement"], ORDINARY)
    assert d == {"run": True, "mode": "auto", "reasons": ["an ordinary merge: auto"]}


def test_high_risk_label_asks():
    d = deploy_mode("auto", "workflow_run", ["HIGH RISK"], ORDINARY)
    assert not d["run"] and d["mode"] == "ask" and d["reasons"] == ["labelled HIGH RISK"]
    assert deploy_mode("auto", "workflow_run", ["high risk "], ORDINARY)["mode"] == "ask"   # the label, whatever its case


def test_guarded_paths_ask():
    cases = {
        "engine/crates/hub-core/src/money.rs": "money",
        "studio/tests/money_flow.py": "money",
        "hub/src/stripe_webhook.rs": "money",
        "engine/crates/hub-core/src/access.rs": "access, authorship or teamspaces",
        "engine/crates/hub-core/src/authorship.rs": "access, authorship or teamspaces",
        "engine/crates/hub-core/tests/teamspaces.rs": "access, authorship or teamspaces",
        "design/BRAND.md": "design/",
        "design/logos/mark.svg": "design/",
        ".github/workflows/hub-deploy.yml": "workflows",
    }
    for path, area in cases.items():
        d = deploy_mode("auto", "workflow_run", [], ORDINARY + [path])
        assert not d["run"] and d["mode"] == "ask", path
        assert any(r.startswith(f"touches {area}: ") and path in r for r in d["reasons"]), (path, d)
    assert not guarded_reasons([], ["site/designer.html", "studio/index.html"]), "design/ means the folder, not the word"


def test_every_reason_is_listed():
    d = deploy_mode("auto", "workflow_run", ["HIGH RISK"], ["engine/crates/hub-core/src/money.rs", ".github/workflows/ci.yml"])
    assert d["reasons"][0] == "labelled HIGH RISK" and len(d["reasons"]) == 3


def test_manual_run_is_the_approval_and_policy_still_rules():
    assert deploy_mode("auto", "workflow_dispatch", ["HIGH RISK"], [".github/workflows/x.yml"])["run"] is True
    assert deploy_mode("ask", "workflow_dispatch", [], [])["mode"] == "manual"
    assert deploy_mode("off", "workflow_dispatch", [], [])["run"] is False, "off means never, even by hand"
    assert deploy_mode("ask", "workflow_run", [], ORDINARY) == {"run": False, "mode": "ask", "reasons": ["the deploy policy is ask"]}
    assert deploy_mode("off", "workflow_run", [], ORDINARY)["mode"] == "off"


def test_unreadable_change_asks():
    d = deploy_mode("auto", "workflow_run", [], [], known=False)
    assert not d["run"] and d["mode"] == "ask"


def test_merged_change_reads_the_merged_pr():
    calls = []

    def gh(*a):
        calls.append(a)
        if a[0].endswith("/pulls") and "commits" in a[0]:
            return json.dumps([{"number": 7, "merged_at": None, "base": {"ref": "main"}, "labels": []},
                               {"number": 9, "merged_at": "2026-11-23T10:00:00Z", "base": {"ref": "main"}, "labels": [{"name": "HIGH RISK"}]}])
        if a[0] == "repos/o/r/pulls/9/files":
            return "engine/crates/hub-core/src/money.rs\nstudio/index.html\n"
        raise AssertionError(a)
    labels, files, pr, known = merged_change("o/r", "abc", gh)
    assert (labels, pr, known) == (["HIGH RISK"], 9, True) and files == ["engine/crates/hub-core/src/money.rs", "studio/index.html"]
    # a direct push (no PR): the commit's own files
    labels, files, pr, known = merged_change("o/r", "abc", lambda *a: "[]" if a[0].endswith("/pulls") else "design/x.svg\n")
    assert (labels, files, pr, known) == ([], ["design/x.svg"], None, True)
    # GitHub unreadable: unknown, so the caller asks

    def broken(*a):
        raise RuntimeError("502")
    assert merged_change("o/r", "abc", broken)[3] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
