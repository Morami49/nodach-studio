import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from main_guard import decide, reached_main_at  # noqa: E402

BASE, HEAD, SHA, OLD = "b" * 40, "h" * 40, "m" * 40, "o" * 40
GREEN_PR = {"id": 7, "name": "ci", "event": "pull_request", "head_branch": "b-x", "conclusion": "success", "created_at": "2026-11-22T19:33:00Z"}
GREEN_PUSH = {"id": 8, "name": "ci", "event": "push", "head_branch": "b-x", "conclusion": "success", "created_at": "2026-11-22T19:32:00Z"}
MAIN_AT_BASE = {"name": "ci", "event": "push", "head_branch": "main", "conclusion": "success", "created_at": "2026-11-22T19:08:00Z"}


def run(runs, trees, merged="T-merge", parents=(BASE, HEAD), heads=(HEAD,), main_tree="T-merge"):
    return decide(SHA, main_tree, list(parents), list(heads), lambda s: runs.get(s, []), lambda s: trees.get(s), lambda a, b: merged)


def test_green_branch_run_on_the_commit_itself():
    assert run({SHA: [GREEN_PUSH]}, {})[0]


def test_same_tree_as_tested_head_q58():
    assert run({HEAD: [GREEN_PUSH]}, {HEAD: "T-merge"})[0]


def test_clean_merge_tested_after_main_reached_base():
    ok, why = run({HEAD: [GREEN_PR, GREEN_PUSH], BASE: [MAIN_AT_BASE]}, {HEAD: "T-head"})
    assert ok and "clean merge" in why


def test_pr_run_older_than_base_still_alarms():
    old = dict(GREEN_PR, created_at="2026-11-21T21:44:30Z")   # #61: tested a day before main moved on
    assert not run({HEAD: [old, GREEN_PUSH], BASE: [MAIN_AT_BASE]}, {HEAD: "T-head"})[0]


def test_merge_with_extra_changes_alarms():
    assert not run({HEAD: [GREEN_PR], BASE: [MAIN_AT_BASE]}, {HEAD: "T-head"}, merged="T-other")[0]


def test_conflicted_merge_alarms():
    assert not run({HEAD: [GREEN_PR], BASE: [MAIN_AT_BASE]}, {HEAD: "T-head"}, merged=None)[0]


def test_unknown_base_time_alarms():
    assert not run({HEAD: [GREEN_PR]}, {HEAD: "T-head"})[0]


def test_red_or_main_only_runs_dont_count():
    red = dict(GREEN_PR, conclusion="failure")
    on_main = dict(GREEN_PUSH, head_branch="main")
    assert not run({HEAD: [red, on_main], SHA: [on_main], BASE: [MAIN_AT_BASE]}, {HEAD: "T-merge"})[0]


def test_head_not_second_parent_alarms():
    assert not run({HEAD: [GREEN_PR], BASE: [MAIN_AT_BASE]}, {HEAD: "T-head"}, parents=(BASE, OLD))[0]


def test_no_merged_pr_alarms():
    assert not run({}, {}, heads=())[0]


def test_reached_main_at_is_earliest_push_to_main():
    runs = [MAIN_AT_BASE, dict(MAIN_AT_BASE, created_at="2026-11-22T19:01:00Z"), dict(MAIN_AT_BASE, event="workflow_run", created_at="2026-11-22T18:00:00Z")]
    assert reached_main_at(runs) == "2026-11-22T19:01:00Z"
    assert reached_main_at([]) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
