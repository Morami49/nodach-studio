"""Tests for the hub smoke test (F-staging-smoke-silent). Run: python3 scripts/test_smoke_hub.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from smoke_hub import CHECKS, run  # noqa: E402

GOOD = {"/health": (200, "ok abc1234\n"), "/studio": (302, ""), "/studio/api/me": (401, '{"error":"sign in"}'),
        "/studio/api/events": (401, '{"error":"sign in"}'), "/mcp": (401, '{"error":"invalid_token"}')}


def fake(answers):
    """answers: path -> list of (code, body) served in turn (the last one repeats)."""
    seen = {}

    def f(url, method="GET", body=None):
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        q = answers[path]
        i = seen.get(path, 0)
        seen[path] = i + 1
        return q[min(i, len(q) - 1)]
    return f


def go(answers, **k):
    lines = []
    rc = run("https://hub.example", "abc1234", fetch=fake(answers), sleep=lambda s: None, out=lines.append, **k)
    return rc, "\n".join(lines)


def test_passes_when_every_check_answers():
    rc, log = go({p: [v] for p, v in GOOD.items()})
    assert rc == 0 and "smoke test passed for abc1234" in log, log


def test_the_23_nov_failure_now_names_the_check_the_code_and_the_body():
    # run 45222430169: /health was right, then a check got an answer that wasn't the hub's, and nothing was printed
    a = {p: [v] for p, v in GOOD.items()}
    a["/studio"] = [(503, "worker is restarting")]
    rc, log = go(a, check_tries=3)
    assert rc == 1, log
    assert "::error title=Smoke test failed::Studio sends signed-out visitors to sign-in" in log, log
    assert "GET https://hub.example/studio answered 503, expected 302" in log, log
    assert "worker is restarting" in log, log
    assert "1 of 4 checks" in log, log


def test_a_settling_worker_is_retried_not_failed():
    a = {p: [v] for p, v in GOOD.items()}
    a["/studio"] = [(0, "ConnectionResetError"), (500, "Durable Object reset"), (302, "")]
    rc, log = go(a)
    assert rc == 0, log
    assert "retry 1/6: GET /studio answered 0" in log and "retry 2/6: GET /studio answered 500" in log, log


def test_wrong_build_says_what_health_answered():
    a = {p: [v] for p, v in GOOD.items()}
    a["/health"] = [(200, "ok 0000000\n")]
    rc, log = go(a, health_tries=2)
    assert rc == 1 and "never named build abc1234" in log and "ok 0000000" in log, log


def test_checks_match_the_old_inline_step():
    assert [(m, p, c) for _, m, p, c, _ in CHECKS] == [("GET", "/studio", 302), ("GET", "/studio/api/me", 401),
                                                        ("POST", "/studio/api/events", 401), ("POST", "/mcp", 401)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
