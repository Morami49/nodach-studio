#!/usr/bin/env python3
"""Production deploy guard rails (B-deploy-guarded-auto, 23 Nov): roll back a failed deploy, and raise notices.

After every production deploy hub-deploy runs scripts/smoke_hub.py. If it fails, this script redeploys the build the
run recorded as `replaced` (the build /health named before the deploy) and smoke-tests that too; either way an urgent
notice goes to Nowed's My queue. It also writes the urgent "waiting for you" notice when a deploy switched to ask.

    python3 scripts/hub_rollback.py rollback --prev 1a2b3c4 --build 9f8e7d6 --url https://... [--dry-run]
    python3 scripts/hub_rollback.py notice ask --sha <sha> --reasons "..." --run-url <url> --approve-url <url>
    python3 scripts/hub_rollback.py notice rollback --sha <sha> --prev <prev> --result rolled-back|failed --run-url <url>

--dry-run prints every command and runs none (tests use it). Notices need HUB_URL and HUB_TOKEN; they are best effort.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from policy import call_tool  # noqa: E402

WRANGLER = "wrangler@4.158.0"
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
OWNER = "morami49"   # whose My queue the notices go to (the hub owner's login, lower case)


def undo_command(repo: str, sha: str) -> str:
    """The one command that puts `sha` back on production (the same as Actions → hub-deploy → Run workflow)."""
    return f"gh workflow run hub-deploy.yml --repo {repo} --ref main -f sha={sha}"


def plan(prev: str, build: str, workdir: str = "/tmp/hub-rollback") -> tuple[list[list[str]], str]:
    """The commands that redeploy `prev`, or ([], why) when there is nothing safe to roll back to."""
    prev, build = (prev or "").strip().lower(), (build or "").strip().lower()
    if not SHA_RE.match(prev):
        return [], f"no recorded build to roll back to (the hub answered {prev or 'nothing'} before this deploy)"
    if prev[:7] == build[:7]:
        return [], f"the replaced build {prev[:7]} is the one that just failed; rolling back would change nothing"
    return [
        ["git", "fetch", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*"],
        ["git", "rev-parse", "--verify", f"{prev}^{{commit}}"],
        ["git", "worktree", "add", "--detach", workdir, prev],
        # deploy the replaced build's own code and wrangler.toml; secrets stay on the Worker as they are
        ["sh", "-c", f"cd {workdir}/hub && BUILD_SHA={prev[:7]} npx -y {WRANGLER} deploy"],
    ], ""


def rollback(prev: str, build: str, url: str, dry_run: bool = False, run=None, smoke=None, out=print) -> dict:
    """Redeploy `prev` and smoke-test it. Returns {"result": rolled-back | failed | skipped, "why": ...}."""
    cmds, why = plan(prev, build)
    if not cmds:
        out(f"::error title=No rollback::{why}")
        return {"result": "skipped", "why": why}
    run = run or (lambda c: subprocess.run(c, check=True))
    for c in cmds:
        out("$ " + " ".join(c))
        if not dry_run:
            try:
                run(c)
            except Exception as e:
                out(f"::error title=Rollback failed::{' '.join(c[:4])}… {type(e).__name__}")
                return {"result": "failed", "why": f"{c[0]} {c[1] if len(c) > 1 else ''} failed"}
    if dry_run:
        return {"result": "dry-run", "why": f"would redeploy {prev[:7]}"}
    if smoke is None:
        from smoke_hub import run as smoke_run
        smoke = lambda: smoke_run(url, prev[:7])  # noqa: E731
    rc = smoke()
    if rc != 0:
        return {"result": "failed", "why": f"{prev[:7]} was redeployed but its smoke test failed too"}
    return {"result": "rolled-back", "why": f"{prev[:7]} is live again and passed the smoke test"}


def notice(kind: str, sha: str, run_url: str, reasons: str = "", approve_url: str = "", prev: str = "", result: str = "",
           repo: str = "Morami49/nodach-studio") -> dict:
    """The write_event for one urgent notice in Nowed's My queue (pure, so it can be tested)."""
    short = (sha or "")[:7]
    if kind == "ask":
        item = f"DEPLOY-ask-{short}"
        title = f"Deploy {short} to production is waiting for you"
        body = (f"This merge switched hub-deploy from auto to ask: {reasons or 'a guarded change'}. Production still runs the "
                f"build before it. To deploy: open {approve_url or run_url}, click Run workflow, leave the commit blank, click "
                f"the green Run workflow button. To skip it, mark this notice done. This run: {run_url}")
    else:
        item = f"DEPLOY-rollback-{short}"
        if result == "rolled-back":
            title = f"Production deploy {short} failed its smoke test and was rolled back to {prev[:7]}"
        else:
            title = f"Production deploy {short} failed its smoke test; rollback {result or 'failed'}: check now"
        body = (f"After deploying {short}, the smoke test (scripts/smoke_hub.py) failed. Rollback to the recorded replaced build "
                f"{prev[:7] or 'unknown'}: {result or 'not tried'}. The run, with each failing check: {run_url}. "
                f"To put a build back by hand: {undo_command(repo, prev[:7] or '<build>')}.")
    return {"kind": "item.create", "item": None, "idempotency_key": f"deploy-notice/{kind}/{short}",
            "body": {"id": item, "type": "notice", "title": title, "body": body, "status": "open", "refs": ["RUNS-hub-deploy", "D-089"],
                     "meta": {"queue": [OWNER], "urgent": True, "lane": "owner", "run_url": run_url, "deploy": {"kind": kind, "sha": short, "prev": prev[:7] or None}}}}


def send(w: dict) -> None:
    url, token = os.environ.get("HUB_URL"), os.environ.get("HUB_TOKEN")
    if not (url and token):
        print("no hub credentials: notice not written")
        return
    try:
        r = call_tool(url, token, "write_event", w)
        print(json.dumps(r))
        if isinstance(r, dict) and r.get("refused"):
            print(f"::warning title=Notice refused::{r.get('error')}")
    except Exception as e:  # a hub outage must not hide the deploy's own result
        print(f"::warning title=Notice not written::{type(e).__name__}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rollback")
    r.add_argument("--prev", required=True)
    r.add_argument("--build", required=True)
    r.add_argument("--url", required=True)
    r.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("notice")
    n.add_argument("kind", choices=["ask", "rollback"])
    n.add_argument("--sha", required=True)
    n.add_argument("--run-url", required=True)
    n.add_argument("--reasons", default="")
    n.add_argument("--approve-url", default="")
    n.add_argument("--prev", default="")
    n.add_argument("--result", default="")
    a = ap.parse_args(argv)
    if a.cmd == "rollback":
        res = rollback(a.prev, a.build, a.url, a.dry_run)
        print(f"rollback: {res['result']} ({res['why']})")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"result={res['result']}\n")
        return 0 if res["result"] in ("rolled-back", "dry-run") else 1
    send(notice(a.kind, a.sha, a.run_url, a.reasons, a.approve_url, a.prev, a.result, os.environ.get("GITHUB_REPOSITORY", "Morami49/nodach-studio")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
