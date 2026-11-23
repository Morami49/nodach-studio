#!/usr/bin/env python3
"""Write an executor's run record into Studio through the hub (P-13, D-135, Q105 A step 1).

Each executor has a Studio item RUNS-<executor>; each run is one comment on it carrying who, why (trigger and
policy), what (commit, inputs), the outcome and how to undo it. Idempotency keys (F-48) mean a re-run step or a
retried call never doubles a record.

    python3 scripts/run_record.py branch-cleanup --title "Branch cleanup runs" --outcome success \
        --text "Deleted 2 branches" --policy auto --undo "Restore from the shas listed" --field deleted=b-a@abc,b-b@def

Needs HUB_URL and HUB_TOKEN; GitHub's own variables (GITHUB_RUN_ID, GITHUB_ACTOR, ...) fill in the rest.
Recording is best effort: a hub outage never fails the executor's own work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from policy import call_tool  # noqa: E402


def record(executor: str, title: str, outcome: str, text: str, policy: str = "", undo: str = "",
           fields: dict | None = None, env: dict | None = None) -> list[dict]:
    """The write_event calls for one run (pure, so it can be tested)."""
    env = env if env is not None else os.environ
    item = f"RUNS-{executor}"
    run_id, attempt = env.get("GITHUB_RUN_ID", "local"), env.get("GITHUB_RUN_ATTEMPT", "1")
    server, repo = env.get("GITHUB_SERVER_URL", "https://github.com"), env.get("GITHUB_REPOSITORY", "")
    body = {"text": text, "outcome": outcome, "clean": outcome == "success", "executor": executor,
            "trigger": env.get("GITHUB_EVENT_NAME", "manual"), "actor": env.get("GITHUB_ACTOR", ""),
            "sha": (env.get("RUN_SHA") or env.get("GITHUB_SHA", ""))[:7], "policy": policy or None, "undo": undo or None,
            "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo else None, **(fields or {})}
    body = {k: v for k, v in body.items() if v not in (None, "")}
    return [
        {"kind": "item.create", "item": None, "idempotency_key": f"runs-item/{executor}",
         "body": {"id": item, "type": "asset", "title": title, "status": "built", "refs": ["P-13", "D-135"],
                  "body": f"One comment per run of the {executor} executor: who started it, why (trigger and policy), "
                          f"what it changed, the outcome and how to undo it (P-13)."}},
        {"kind": "comment", "item": item, "idempotency_key": f"run/{executor}/{run_id}/{attempt}", "body": body},
    ]


def refusal(result: dict) -> str | None:
    """The hub's reason when it refused a write_event (None when it was written or repeated)."""
    if isinstance(result, dict) and result.get("refused"):
        return str(result.get("error") or "refused")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("executor")
    ap.add_argument("--title", required=True)
    ap.add_argument("--outcome", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--policy", default="")
    ap.add_argument("--undo", default="")
    ap.add_argument("--field", action="append", default=[], help="k=v, repeatable")
    a = ap.parse_args(argv)
    fields = dict(f.split("=", 1) for f in a.field if "=" in f)
    url, token = os.environ.get("HUB_URL"), os.environ.get("HUB_TOKEN")
    if not (url and token):
        print("no hub credentials: run record not written")
        return 0
    try:
        for w in record(a.executor, a.title, a.outcome, a.text, a.policy, a.undo, fields):
            r = call_tool(url, token, "write_event", w)
            print(json.dumps(r))
            if refusal(r):
                # F-ci-token-refused: five days of refused records went unnoticed as plain JSON lines in the log
                print(f"::warning title=Run record refused::RUNS-{a.executor}: {refusal(r)}")
    except Exception as e:
        print(f"::warning title=Run record not written::{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
