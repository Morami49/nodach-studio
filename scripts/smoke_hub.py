#!/usr/bin/env python3
"""Smoke-test a deployed hub and say WHY it failed (F-staging-smoke-silent, 23 Nov).

hub-staging run 45222430169 failed its smoke step with exit 1 and no output: the step was a chain of bare
`test "$(curl ...)" = 302` lines under `set -e`, so the failing check, the code it got and the body were never shown.
The staging hub's own log shows the /studio request never reached the hub's handler: it landed while the Worker was
still rolling out the versions created by the secret puts just before it (the Stripe webhook secret went live during
the smoke step). This script:
  - waits for /health to name the expected build,
  - retries each check for a short settle window (secret puts create new Worker versions; the first requests after
    them can be answered by the platform, not the hub),
  - on failure prints each check's URL, expected code, last code, and the start of the last body, then exits 1.

    python3 scripts/smoke_hub.py --url https://clanstack-hub-staging.ndab-utube.workers.dev --build 160126d

Only the standard library; never prints a token.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

# (name, method, path, expected status, body or None)
CHECKS = [
    ("Studio sends signed-out visitors to sign-in", "GET", "/studio", 302, None),
    ("Studio's API refuses anyone not signed in", "GET", "/studio/api/me", 401, None),
    ("Studio's API refuses unsigned writes", "POST", "/studio/api/events", 401, b"{}"),
    ("the Claude connector starts sign-in", "POST", "/mcp", 401, b"{}"),
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 302 is an answer here, not something to follow
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, method: str = "GET", body: bytes | None = None, timeout: float = 15) -> tuple[int, str]:
    """(status, first 300 chars of the body); status 0 when the hub could not be reached at all."""
    req = urllib.request.Request(url, data=body, method=method, headers={"user-agent": "nodach-smoke/1 (+github actions)"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(300).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read(300).decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, text
    except Exception as e:  # DNS, TLS, reset, timeout
        return 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}"


def run(url: str, build: str, health_tries: int = 18, check_tries: int = 6, wait: float = 10,
        fetch=fetch, sleep=time.sleep, out=print) -> int:
    url = url.rstrip("/")
    want = f"ok {build}"
    code, text = 0, ""
    for i in range(1, health_tries + 1):
        code, text = fetch(url + "/health")
        if code == 200 and text.strip().startswith(want):
            break
        out(f"not live yet ({i}/{health_tries}): /health answered {code} {text.strip()[:80]!r}")
        if i < health_tries:
            sleep(wait)
    else:
        out(f"::error title=Smoke test failed::/health never named build {build}: last answer {code} {text.strip()[:200]!r}")
        return 1
    failed = []
    for name, method, path, expect, body in CHECKS:
        for i in range(1, check_tries + 1):
            code, text = fetch(url + path, method, body)
            if code == expect:
                out(f"ok: {name} ({method} {path} -> {code})")
                break
            out(f"retry {i}/{check_tries}: {method} {path} answered {code}, expected {expect}: {text.strip()[:120]!r}")
            if i < check_tries:
                sleep(wait)
        else:
            failed.append((name, method, path, expect, code, text))
    for name, method, path, expect, code, text in failed:
        out(f"::error title=Smoke test failed::{name}: {method} {url}{path} answered {code}, expected {expect}. "
            f"Body: {text.strip()[:200]!r}")
    if failed:
        out(f"smoke test FAILED for {build}: {len(failed)} of {len(CHECKS)} checks (see the errors above)")
        return 1
    out(f"smoke test passed for {build}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--build", required=True, help="the 7-char build /health must report")
    ap.add_argument("--health-tries", type=int, default=18)
    ap.add_argument("--check-tries", type=int, default=6)
    ap.add_argument("--wait", type=float, default=10)
    a = ap.parse_args(argv)
    return run(a.url, a.build, a.health_tries, a.check_tries, a.wait)


if __name__ == "__main__":
    sys.exit(main())
