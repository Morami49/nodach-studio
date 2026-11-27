import json
import os
import subprocess
import sys
import tempfile

from should_automerge import REQUIRED
from staging_train import build, candidates, conflict_comment, dropped_out, git

GREEN = [{"name": c, "conclusion": "success"} for c in REQUIRED]
REPO = "Morami49/nodach-studio"


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def _commit(d, msg):
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", msg)
    return git(d, "rev-parse", "HEAD").stdout.strip()


def make_repo():
    """main plus three branches: b-a adds a.txt, b-b adds b.txt, b-c rewrites the line b-a also rewrites."""
    d = tempfile.mkdtemp(prefix="train-")
    git(d, "init", "-q", "-b", "main")
    _write(d, "shared.txt", "one\ntwo\nthree\n")
    base = _commit(d, "base")
    shas = {}
    for br, fname, text in [("b-a", "shared.txt", "ONE (from a)\ntwo\nthree\n"),
                            ("b-b", "b.txt", "bee\n"),
                            ("b-c", "shared.txt", "ONE (from c)\ntwo\nthree\n")]:
        git(d, "checkout", "-q", "-b", br, base)
        _write(d, fname, text)
        shas[br] = _commit(d, f"work on {br}")
    git(d, "checkout", "-q", "main")
    return d, base, shas


def pr(n, branch, sha, created, draft=False, repo=REPO, labels=()):
    return {"number": n, "state": "open", "draft": draft, "created_at": created, "base": {"ref": "main"},
            "labels": [{"name": x} for x in labels],
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo} if repo else None}}


def test_train_merges_in_order_and_leaves_out_the_conflict():
    d, base, s = make_repo()
    cands = [{"pr": 1, "branch": "b-a", "sha": s["b-a"]}, {"pr": 2, "branch": "b-b", "sha": s["b-b"]},
             {"pr": 3, "branch": "b-c", "sha": s["b-c"]}]
    t = build(d, "main", cands)
    assert t["base"] == base
    assert [e["branch"] for e in t["included"]] == ["b-a", "b-b"]
    assert len(t["left_out"]) == 1
    lo = t["left_out"][0]
    assert lo["branch"] == "b-c" and lo["conflicts_with"] == ["b-a"]
    assert lo["comment"] == "left out of the staging train: conflicts with b-a"
    # the train head contains both included branches' work and main is untouched
    for sha in (s["b-a"], s["b-b"]):
        assert git(d, "merge-base", "--is-ancestor", sha, t["head"], check=False).returncode == 0
    assert git(d, "merge-base", "--is-ancestor", s["b-c"], t["head"], check=False).returncode != 0
    assert git(d, "show", f"{t['head']}:shared.txt").stdout.startswith("ONE (from a)")
    assert git(d, "show", f"{t['head']}:b.txt").stdout == "bee\n"
    assert git(d, "rev-parse", "main").stdout.strip() == base
    assert git(d, "status", "--porcelain").stdout == ""


def test_oldest_first_decides_who_wins():
    d, base, s = make_repo()
    t = build(d, "main", [{"pr": 3, "branch": "b-c", "sha": s["b-c"]}, {"pr": 1, "branch": "b-a", "sha": s["b-a"]}])
    assert [e["branch"] for e in t["included"]] == ["b-c"] and t["left_out"][0]["conflicts_with"] == ["b-c"]


def test_conflict_with_main_names_main():
    d, base, s = make_repo()
    git(d, "checkout", "-q", "main")
    _write(d, "shared.txt", "ONE (main moved)\ntwo\nthree\n")
    _commit(d, "main moves")
    t = build(d, "main", [{"pr": 1, "branch": "b-a", "sha": s["b-a"]}, {"pr": 2, "branch": "b-b", "sha": s["b-b"]}])
    assert [e["branch"] for e in t["included"]] == ["b-b"]
    assert t["left_out"][0]["conflicts_with"] == ["main"]


def test_empty_train_is_main():
    d, base, s = make_repo()
    t = build(d, "main", [])
    assert t["head"] == base and t["included"] == []


def test_candidates_filter_and_order():
    s = {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40, "d": "d" * 40, "e": "e" * 40, "t": "f" * 40}
    prs = [pr(5, "b-late", s["b"], "2026-11-19T10:00:00Z"),
           pr(4, "b-early", s["a"], "2026-11-18T10:00:00Z"),
           pr(6, "b-draft", s["c"], "2026-11-17T10:00:00Z", draft=True),
           pr(7, "b-fork", s["d"], "2026-11-16T10:00:00Z", repo="someone/fork"),
           pr(8, "b-red", s["e"], "2026-11-15T10:00:00Z")]
    checks = {s["a"]: GREEN, s["b"]: GREEN, s["c"]: GREEN, s["d"]: GREEN, s["e"]: GREEN[:2], s["t"]: GREEN}
    out, skipped = candidates(prs, checks, REPO, "b-nopr", s["t"])
    assert [c["branch"] for c in out] == ["b-early", "b-late", "b-nopr"]
    why = {x["branch"]: x["why"] for x in skipped}
    assert why["b-draft"] == "draft" and why["b-fork"] == "from a fork" and "required checks" in why["b-red"]


def test_trigger_branch_needs_green_checks_and_is_not_doubled():
    s = "a" * 40
    out, skipped = candidates([pr(4, "b-x", s, "2026-11-18T10:00:00Z")], {s: GREEN}, REPO, "b-x", s)
    assert [c["branch"] for c in out] == ["b-x"]
    out, skipped = candidates([], {}, REPO, "b-y", "b" * 40)
    assert out == [] and skipped[0]["branch"] == "b-y"
    out, _ = candidates([], {}, REPO, "main", "c" * 40)
    assert out == []
    # a draft PR is still staged when it is the branch just pushed (the old one-branch behaviour)
    out, _ = candidates([pr(4, "b-x", s, "2026-11-18T10:00:00Z", draft=True)], {s: GREEN}, REPO, "b-x", s)
    assert [c["branch"] for c in out] == ["b-x"]


def test_draft_plus_try_included():
    """F-staging-train-drafts: a draft labelled 'try' rides the train even when another branch triggered the deploy."""
    s = {"a": "a" * 40, "b": "b" * 40, "t": "f" * 40}
    prs = [pr(51, "b-live", s["a"], "2026-11-19T23:00:00Z", draft=True, labels=("hold", "try")),
           pr(52, "b-plain-draft", s["b"], "2026-11-19T23:10:00Z", draft=True, labels=("hold",))]
    out, skipped = candidates(prs, {s["a"]: GREEN, s["b"]: GREEN, s["t"]: GREEN}, REPO, "b-other", s["t"])
    assert [c["branch"] for c in out] == ["b-live", "b-other"]
    assert {x["branch"]: x["why"] for x in skipped} == {"b-plain-draft": "draft"}
    # labels may also arrive as bare strings
    p = pr(53, "b-s", s["a"], "2026-11-19T23:00:00Z", draft=True)
    p["labels"] = ["try"]
    out, _ = candidates([p], {s["a"]: GREEN}, REPO)
    assert [c["branch"] for c in out] == ["b-s"]
    # a red 'try' draft is still left out, with the reason
    out, skipped = candidates([pr(54, "b-red", s["b"], "2026-11-19T23:00:00Z", draft=True, labels=("try",))],
                              {s["b"]: GREEN[:1]}, REPO)
    assert out == [] and "required checks" in skipped[0]["why"]


def test_dropped_out_names_each_pr_once_with_why():
    prev = {"mode": "train", "head": "1" * 40, "included": [
        {"pr": 48, "branch": "b-show", "sha": "a" * 40}, {"pr": 51, "branch": "b-live", "sha": "b" * 40},
        {"pr": 40, "branch": "b-merged", "sha": "c" * 40}, {"pr": None, "branch": "b-nopr", "sha": "d" * 40}]}
    train = {"mode": "train", "head": "2" * 40, "included": [{"pr": 48, "branch": "b-show", "sha": "a" * 40}],
             "left_out": [], "not_candidates": [{"pr": 51, "branch": "b-live", "sha": "b" * 40, "why": "draft"}]}
    prs = [pr(48, "b-show", "a" * 40, "x"), pr(51, "b-live", "b" * 40, "x", draft=True)]  # #40 merged: not open
    d = dropped_out(prev, train, prs)
    assert [(x["pr"], x["why"]) for x in d] == [(51, "draft")]
    assert "2222222" in d[0]["comment"] and "1111111" in d[0]["comment"] and "'try' label" in d[0]["comment"]
    # a conflict names the culprit
    train["not_candidates"] = []
    train["left_out"] = [{"pr": 51, "branch": "b-live", "sha": "b" * 40, "why": "conflict",
                          "conflicts_with": ["b-show"], "comment": "x"}]
    assert dropped_out(prev, train, prs)[0]["why"] == "conflict with b-show"
    # nothing to compare: first deploy, single mode, or nothing dropped
    assert dropped_out(None, train, prs) == []
    assert dropped_out({**prev, "mode": "single"}, train, prs) == []
    assert dropped_out(prev, {**train, "mode": "single"}, prs) == []
    assert dropped_out({"mode": "train", "included": train["included"]}, train, prs) == []


def test_comment_text():
    assert conflict_comment(["b-a", "b-b"]) == "left out of the staging train: conflicts with b-a, b-b"


def test_cli_single_and_train():
    d, base, s = make_repo()
    here = os.path.dirname(os.path.abspath(__file__))
    prs = [pr(1, "b-a", s["b-a"], "2026-11-17T00:00:00Z"), pr(3, "b-c", s["b-c"], "2026-11-18T00:00:00Z")]
    checks = {s["b-a"]: GREEN, s["b-c"]: GREEN, s["b-b"]: GREEN}
    pj, cj, out = (os.path.join(d, ".git", n) for n in ("prs.json", "checks.json", "train.json"))
    json.dump(prs, open(pj, "w"))
    json.dump(checks, open(cj, "w"))
    common = [sys.executable, os.path.join(here, "staging_train.py"), "--repo", REPO, "--base", "main",
              "--prs", pj, "--checks", cj, "--out", out, "--cwd", d, "--trigger-branch", "b-b",
              "--trigger-sha", s["b-b"]]
    subprocess.run(common, check=True, capture_output=True)
    t = json.load(open(out))
    assert [e["branch"] for e in t["included"]] == ["b-a", "b-b"] and t["left_out"][0]["pr"] == 3
    assert t["dropped_out"] == []
    # --prev: #3 was on staging last time, now conflicts, so it is reported as dropped out
    prev = os.path.join(d, ".git", "prev.json")
    json.dump({"mode": "train", "head": base, "included": [{"pr": 3, "branch": "b-c", "sha": s["b-c"]}]}, open(prev, "w"))
    subprocess.run(common + ["--prev", prev], check=True, capture_output=True)
    t = json.load(open(out))
    assert [(x["pr"], x["why"]) for x in t["dropped_out"]] == [(3, "conflict with b-a")]
    # a missing or broken --prev file is ignored, never fatal
    open(prev, "w").write("not json")
    subprocess.run(common + ["--prev", prev], check=True, capture_output=True)
    assert json.load(open(out))["dropped_out"] == []
    subprocess.run(common + ["--single"], check=True, capture_output=True)
    t = json.load(open(out))
    assert t["mode"] == "single" and t["head"] == s["b-b"] and t["included"][0]["sha"] == s["b-b"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
