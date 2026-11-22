"""P1 c (INC-261118-seed, AUD-secrets-state action 2): restore drill for a hub's shared record.

Reads the hub's /status, pulls every /pull page, rebuilds a fresh hub from that export with the engine's own code
(engine/crates/hub-core/examples/restore_drill.rs) and checks the rebuild matches: same events, chain verifies,
re-sending adds nothing. Read-only: it never writes to any hub. The export is kept only in a temp file that is
deleted at the end; the printed report has counts and checksums, never content.

Usage: HUB_TOKEN=... python3 scripts/restore_drill.py <env> <hub url>   (exit 0 ok, 1 drift, 2 couldn't run)
Not covered yet (later slices): the Private tier (export with a test owner, reimport, compare checksums) and a
Durable Object point-in-time restore to a bookmark.
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = 2000  # the hub's /pull maximum


def http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {token}", "user-agent": "nodach-restore-drill"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def export(base: str, get) -> dict:
    """{source, events}: the hub's own status, then every event from cursor 0 until caught up."""
    st = get(f"{base}/status")
    events, after = [], 0
    while True:
        page = get(f"{base}/pull?after={after}&limit={PAGE}")
        events.extend(page.get("events") or [])
        nxt = int(page.get("next", after))
        if nxt <= after and page.get("events"):
            raise RuntimeError(f"/pull cursor did not move past {after}")
        after = nxt
        if after >= int(page.get("total", after)) or not page.get("events"):
            break
    return {"source": {"events": int(st.get("events", 0)), "verified": bool(st.get("verified"))}, "events": events}


def rebuild(data: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    try:
        p = subprocess.run(
            ["cargo", "run", "-q", "--release", "-p", "hub-core", "--example", "restore_drill", "--", path],
            cwd=ROOT / "engine", capture_output=True, text=True)
    finally:
        os.unlink(path)
    if p.returncode not in (0, 1):
        raise RuntimeError(f"rebuild could not run: {p.stderr.strip()[-500:]}")
    return json.loads(p.stdout)


def summary(env: str, r: dict) -> str:
    head = (f"{env}: restore drill {'ok' if r.get('ok') else 'FAILED'}: source {r.get('source_events')} events, "
            f"exported {r.get('exported')}, rebuilt {r.get('rebuilt')}, refused {r.get('refused')}, "
            f"chain {'verifies' if r.get('verified') else 'does not verify'}, re-send added {r.get('resend_added')}; "
            f"event-set sha256 {str(r.get('export_set_sha256', ''))[:16]}")
    return "\n".join([head] + [f"- {p}" for p in r.get("problems") or []])


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    env, base = argv[1], argv[2].rstrip("/")
    token = os.environ.get("HUB_TOKEN", "")
    if not token:
        print(f"{env}: no token; restore drill skipped", file=sys.stderr)
        return 2
    try:
        report = rebuild(export(base, lambda u: http_get(u, token)))
    except Exception as e:  # network, auth or build trouble: say so, never pretend it passed
        print(f"{env}: restore drill could not run: {e}")
        return 2
    print(summary(env, report))
    Path("drill-report.json").write_text(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
