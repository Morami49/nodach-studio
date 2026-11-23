"""Tests for run records (P-13). Run: python3 scripts/test_run_record.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from run_record import record, refusal  # noqa: E402

ENV = {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY": "Morami49/clanstack-studio",
       "GITHUB_EVENT_NAME": "push", "GITHUB_ACTOR": "Morami49", "GITHUB_SHA": "1bf8abdffff"}


def test_record_shape_and_keys():
    item, run = record("branch-cleanup", "Branch cleanup runs", "success", "Deleted 1", "auto", "restore b-x@abc",
                       {"deleted": "b-x@abc"}, ENV)
    assert item["kind"] == "item.create" and item["body"]["id"] == "RUNS-branch-cleanup"
    assert item["idempotency_key"] == "runs-item/branch-cleanup"
    assert run["item"] == "RUNS-branch-cleanup" and run["idempotency_key"] == "run/branch-cleanup/42/2"
    b = run["body"]
    assert b["clean"] is True and b["policy"] == "auto" and b["sha"] == "1bf8abd" and b["deleted"] == "b-x@abc"
    assert b["run_url"] == "https://github.com/Morami49/clanstack-studio/actions/runs/42"


def test_failed_run_is_not_clean_and_drops_empty_fields():
    _, run = record("hub-deploy", "Hub deploys", "failure", "Smoke test failed", env=ENV)
    assert run["body"]["clean"] is False and "policy" not in run["body"] and "undo" not in run["body"]


def test_refusals_are_recognised():
    # the 23 Nov answer from the production hub
    assert refusal({"error": "RUNS-hub-staging isn't in a space you've been given", "refused": True}).startswith("RUNS-hub-staging")
    assert refusal({"written": "abc"}) is None and refusal({"written": "abc", "repeat": True}) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
