"""D-087 main guard: main may only move to content that already passed CI on a branch.

Accepted (exit 0), in order:
  1. the commit itself has a green `ci` run on a branch;
  2. Q58 A: it came from a merged PR whose tested head has the SAME git tree (a rebase or fast-forward);
  3. F-main-guard-merge (23 Nov 2026): it is a GitHub merge commit (parents: main before, PR head) whose tree is
     exactly the clean merge of those two parents, and the PR's green `pull_request` ci run started after main had
     reached that first parent. A pull_request run tests refs/pull/N/merge, i.e. this same merge of the PR head into
     main as it was then, so the content is identical to what was tested. Before this rule every merge of a PR that
     wasn't rebased onto main raised a false "main moved without green CI" issue (#68, #69, #70, #73).
Right after a merge GitHub can still answer "no PRs" for the new commit (that is what raised #73 for c0656ed, whose
content was identical to the tested PR head). So a failing check is asked again a few times, 20 s apart, before it
alarms. Anything else still fails and opens the issue, exactly as before. Fails closed when an API call or git step fails.

Usage (in the guards workflow, with the repo fetched in full): python3 scripts/main_guard.py <sha>
Env: REPO (owner/name). Uses `gh api` (REST only) and local `git`.
"""
import json
import os
import subprocess
import sys
import time


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def gh(path: str):
    return json.loads(sh("gh", "api", path))


def green_branch_ci(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r.get("name") == "ci" and r.get("head_branch") != "main" and r.get("conclusion") == "success"]


def reached_main_at(runs: list[dict]) -> str | None:
    """When main first pointed at a commit: the earliest run a push to main started for it."""
    t = sorted(r["created_at"] for r in runs if r.get("head_branch") == "main" and r.get("event") == "push")
    return t[0] if t else None


def decide(sha: str, tree: str, parents: list[str], pr_heads: list[str], runs_for, tree_of, merge_tree) -> tuple[bool, str]:
    """runs_for(sha) -> workflow runs for that head sha; tree_of(sha) -> tree sha; merge_tree(a, b) -> clean merge
    tree sha or None on conflict. Pure apart from those three, so it can be tested without GitHub."""
    if green_branch_ci(runs_for(sha)):
        return True, f"{sha[:7]} has a green branch ci run"
    for head in pr_heads:
        green = green_branch_ci(runs_for(head))
        if not green:
            continue
        if tree_of(head) == tree:
            return True, f"same content as tested PR head {head[:7]} (Q58 A)"
        if len(parents) == 2 and parents[1] == head:
            base = parents[0]
            since = reached_main_at(runs_for(base))
            prs = [r for r in green if r.get("event") == "pull_request" and since and r["created_at"] >= since]
            if prs and merge_tree(base, head) == tree:
                return True, (f"clean merge of PR head {head[:7]} into {base[:7]}, the exact content its pull_request ci "
                              f"run {prs[0].get('id')} tested (main reached {base[:7]} at {since})")
    return False, f"{sha[:7]}: no green ci run on a branch for this content"


def main(sha: str, tries: int = 4, wait: float = 20.0) -> int:
    for n in range(1, tries + 1):
        ok, why = check_once(sha)
        print(f"try {n}: " + ("ok: " if ok else "not yet: ") + why)
        if ok:
            return 0
        if n < tries:
            time.sleep(wait)
    print("ALARM: " + why)
    return 1


def check_once(sha: str) -> tuple[bool, str]:
    repo = os.environ["REPO"]
    cache: dict[str, list] = {}

    def runs_for(s: str) -> list[dict]:
        if s not in cache:
            cache[s] = gh(f"repos/{repo}/actions/runs?head_sha={s}&per_page=100").get("workflow_runs", [])
        return cache[s]

    def tree_of(s: str) -> str:
        return gh(f"repos/{repo}/git/commits/{s}")["tree"]["sha"]

    def merge_tree(a: str, b: str) -> str | None:
        r = subprocess.run(["git", "merge-tree", "--write-tree", a, b], capture_output=True, text=True)
        return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None

    c = gh(f"repos/{repo}/git/commits/{sha}")
    parents = [p["sha"] for p in c.get("parents", [])]
    heads = [p["head"]["sha"] for p in gh(f"repos/{repo}/commits/{sha}/pulls") if p.get("merged_at")]
    return decide(sha, c["tree"]["sha"], parents, heads, runs_for, tree_of, merge_tree)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
