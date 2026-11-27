"""F-staging-one-branch: build the staging "train", so parallel chats stop overwriting each other on staging.

The train starts from main and merges in, oldest first, every open non-draft PR from this repository (never a fork)
whose required checks passed on its head SHA, plus the branch that triggered the deploy if its checks passed. A draft
PR labelled "try" is included too (F-staging-train-drafts, 20 Nov: a draft Nowed was trying fell off staging). A
branch that conflicts is left out, and the PR gets one comment naming what it conflicts with. Merges are made with
`git merge-tree --write-tree` and `git commit-tree`, so no working tree is touched until the caller checks out the
result; nothing is pushed anywhere.

    python3 scripts/staging_train.py --repo R --base origin/main --prs prs.json --checks checks.json \
        --trigger-branch B --trigger-sha S --out train.json [--single] [--prev previous-train.json]

prs.json: the GitHub "list pull requests" array. checks.json: {sha: [{"name", "conclusion"}, ...]}.
train.json: {"mode", "base", "head", "included": [{pr, branch, sha}], "left_out": [{pr, branch, sha, why,
conflicts_with, comment}], "dropped_out": [{pr, branch, why, comment}]}. The caller deploys "head".
dropped_out lists PRs that the previous deploy (--prev, its train.json) included but this one doesn't, while still
open, so the caller can comment on each one instead of a PR silently vanishing from staging.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from should_automerge import REQUIRED, passed_checks  # noqa: E402

IDENTITY = {"GIT_AUTHOR_NAME": "staging-train", "GIT_AUTHOR_EMAIL": "staging-train@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "staging-train", "GIT_COMMITTER_EMAIL": "staging-train@users.noreply.github.com"}


def git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **IDENTITY})


TRY_LABEL = "try"


def _labels(p: dict) -> set[str]:
    return {(x.get("name") if isinstance(x, dict) else x) for x in (p.get("labels") or [])}


def conflict_comment(conflicts_with: list[str]) -> str:
    return "left out of the staging train: conflicts with " + ", ".join(conflicts_with)


def candidates(prs: list[dict], checks: dict, repo: str, trigger_branch: str = "", trigger_sha: str = "",
               base_branch: str = "main") -> tuple[list[dict], list[dict]]:
    """Ordered train candidates (oldest PR first, a trigger branch without a PR last) and the ones skipped."""
    out, skipped, seen = [], [], set()
    for p in sorted(prs, key=lambda p: (p.get("created_at") or "", p.get("number") or 0)):
        head = p.get("head") or {}
        branch, sha = head.get("ref", ""), head.get("sha", "")
        entry = {"pr": p.get("number"), "branch": branch, "sha": sha}
        if p.get("state", "open") != "open" or (p.get("base") or {}).get("ref") != base_branch:
            continue
        if ((head.get("repo") or {}).get("full_name")) != repo:
            skipped.append({**entry, "why": "from a fork"})
            continue
        is_trigger = branch == trigger_branch
        if p.get("draft") and not is_trigger and TRY_LABEL not in _labels(p):
            skipped.append({**entry, "why": "draft"})
            continue
        missing = [c for c in REQUIRED if c not in passed_checks(checks.get(sha, []))]
        if missing:
            skipped.append({**entry, "why": "required checks not passed: " + ", ".join(missing)})
            continue
        seen.add(branch)
        out.append(entry)
    if trigger_branch and trigger_branch != base_branch and trigger_branch not in seen and trigger_sha:
        if not any(s["branch"] == trigger_branch for s in skipped):
            missing = [c for c in REQUIRED if c not in passed_checks(checks.get(trigger_sha, []))]
            entry = {"pr": None, "branch": trigger_branch, "sha": trigger_sha}
            if missing:
                skipped.append({**entry, "why": "required checks not passed: " + ", ".join(missing)})
            else:
                out.append(entry)
    return out, skipped


def _merge(cwd: str, onto: str, sha: str, message: str) -> str | None:
    """Commit merging sha into onto, or None on conflict."""
    if git(cwd, "merge-base", "--is-ancestor", sha, onto, check=False).returncode == 0:
        return onto  # already contained (e.g. a branch main has caught up with)
    r = git(cwd, "merge-tree", "--write-tree", "--no-messages", onto, sha, check=False)
    if r.returncode == 1:
        return None
    if r.returncode != 0:
        raise RuntimeError(f"git merge-tree failed: {r.stderr.strip()}")
    tree = r.stdout.split()[0]
    return git(cwd, "commit-tree", tree, "-p", onto, "-p", sha, "-m", message).stdout.strip()


def _culprits(cwd: str, base: str, base_name: str, included: list[dict], sha: str) -> list[str]:
    if _merge(cwd, base, sha, "probe") is None:
        return [base_name]
    hits = []
    for e in included:
        with_e = _merge(cwd, base, e["sha"], "probe")
        if with_e is not None and _merge(cwd, with_e, sha, "probe") is None:
            hits.append(e["branch"])
    return hits or [e["branch"] for e in included]  # only the combination conflicts


def build(cwd: str, base: str, cands: list[dict], base_name: str = "main") -> dict:
    base_sha = git(cwd, "rev-parse", base).stdout.strip()
    cur, included, left_out = base_sha, [], []
    for c in cands:
        if git(cwd, "cat-file", "-e", c["sha"] + "^{commit}", check=False).returncode != 0:
            left_out.append({**c, "why": "commit not fetched", "conflicts_with": [], "comment": None})
            continue
        label = f"{c['branch']} (#{c['pr']})" if c.get("pr") else c["branch"]
        nxt = _merge(cwd, cur, c["sha"], f"staging train: merge {label}")
        if nxt is None:
            who = _culprits(cwd, base_sha, base_name, included, c["sha"])
            left_out.append({**c, "why": "conflict", "conflicts_with": who, "comment": conflict_comment(who)})
            continue
        cur = nxt
        included.append(c)
    return {"mode": "train", "base": base_sha, "head": cur, "included": included, "left_out": left_out}


def dropped_out(prev: dict | None, train: dict, prs: list[dict]) -> list[dict]:
    """PRs the previous deploy included that this one doesn't, while they are still open (one comment each)."""
    if not prev or prev.get("mode") != "train" or train.get("mode") != "train":
        return []
    now = {e.get("pr") for e in train.get("included", []) if e.get("pr")}
    open_prs = {p.get("number") for p in prs if p.get("state", "open") == "open"}
    why = {}
    for e in train.get("left_out", []) + train.get("not_candidates", []):
        if e.get("pr"):
            w = e.get("why", "")
            if e.get("conflicts_with"):
                w += " with " + ", ".join(e["conflicts_with"])
            why[e["pr"]] = w
    head = (train.get("head") or "")[:7]
    prev_head = (prev.get("head") or "")[:7]
    out = []
    for e in prev.get("included", []):
        n = e.get("pr")
        if not n or n in now or n not in open_prs:
            continue
        w = why.get(n, "not offered to the train")
        out.append({"pr": n, "branch": e.get("branch"), "why": w,
                    "comment": f"dropped out of the staging train: staging {head} no longer includes this PR "
                               f"(it was in {prev_head}). Why: {w}. To get it back: fix that, or mark the PR ready "
                               f"for review (a draft needs the '{TRY_LABEL}' label)."})
    return out


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--repo", required=True)
    a.add_argument("--base", default="origin/main")
    a.add_argument("--prs", required=True)
    a.add_argument("--checks", required=True)
    a.add_argument("--trigger-branch", default="")
    a.add_argument("--trigger-sha", default="")
    a.add_argument("--single", action="store_true", help="old behaviour: deploy the trigger SHA alone")
    a.add_argument("--prev", default="", help="the previous deploy's train.json, to list PRs that dropped out")
    a.add_argument("--out", required=True)
    a.add_argument("--cwd", default=".")
    o = a.parse_args()
    prs = json.load(open(o.prs))
    if o.single:
        pr = next((p.get("number") for p in prs if (p.get("head") or {}).get("sha") == o.trigger_sha), None)
        train = {"mode": "single", "base": None, "head": o.trigger_sha,
                 "included": [{"pr": pr, "branch": o.trigger_branch, "sha": o.trigger_sha}], "left_out": []}
    else:
        cands, skipped = candidates(prs, json.load(open(o.checks)), o.repo, o.trigger_branch, o.trigger_sha)
        train = build(o.cwd, o.base, cands)
        train["not_candidates"] = skipped
        prev = None
        if o.prev and os.path.exists(o.prev):
            try:
                prev = json.load(open(o.prev))
            except ValueError:
                prev = None
        train["dropped_out"] = dropped_out(prev, train, prs)
    json.dump(train, open(o.out, "w"), indent=2)
    print(json.dumps(train, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
