"""
Nodach Gate: Studio side. Sign-in (the same nodach_session cookie approach as actionmap/live, minimal code copied,
not imported), the outbox of reports waiting to be sent, and attachments to the encrypted Private tier.

Where things go:
  report text      -> a comment on the day's item GATE-yymmdd in the shared record (filtered: core.build_report)
  pictures / zips  -> the owner's Private tier (POST /studio/api/private, encrypted with the owner's key), split into
                      40 KB chunks at data/users/<login>/gate/files; the comment carries only {name, bytes, sha256,
                      private_path, chunks}. Too big, signed out, not owner, or the private store not set up: the
                      file stays on this PC and the comment lists its name with local: true.
Nothing here prints or logs the sign-in value. Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import gate_core as core

HOME = Path(os.environ.get("NODACH_GATE_HOME", str(Path.home() / "nodach-gate")))
HUBS = {"staging": "https://staging.nodach.com", "production": "https://studio.nodach.com"}


def hub_url(name: str) -> str:
    return HUBS.get(name, name if str(name).startswith("https://") else HUBS["staging"])


class HubError(RuntimeError):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class Hub:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._cookie = "nodach_session=" + token   # sent only to this hub, never printed

    def __repr__(self):
        return f"Hub({self.base!r})"

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Cookie": self._cookie, "Accept": "application/json", "User-Agent": f"nodach-gate/{core.VERSION}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "x-nodach-studio": "1"})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except Exception:
                msg = ""
            raise HubError(f"hub said {e.code}{': ' + msg if msg else ''}", e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise HubError(f"couldn't reach the hub ({type(getattr(e, 'reason', e)).__name__})", 0) from None

    def me(self) -> dict:
        return self._req("GET", "/studio/api/me")

    def events(self):
        out, after = [], 0
        while True:
            r = self._req("GET", f"/studio/api/events?after={after}&limit=2000")
            out += r.get("events") or []
            nxt = r.get("next", after)
            if nxt <= after or nxt >= r.get("total", 0):
                return out
            after = nxt

    def post(self, event: dict, did: str) -> None:
        self._req("POST", "/studio/api/events", {"doc_id": did, "event": event})

    def private_put(self, path: str, did: str, event: dict) -> dict:
        return self._req("POST", "/studio/api/private", {"path": path, "doc_id": did, "event": event})


# ---------------------------------------------------------------- sign-in (as actionmap/live)
def token_file(url: str) -> Path:
    return HOME / f"session-{urllib.parse.urlparse(url).hostname or 'hub'}.txt"


def save_token(url: str, t: str) -> None:
    f = token_file(url)
    HOME.mkdir(parents=True, exist_ok=True)
    f.write_text(t, encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def forget_token(url: str) -> None:
    try:
        token_file(url).unlink()
    except OSError:
        pass


def stored_token(url: str):
    t = core.clean_token(os.environ.get("NODACH_GATE_TOKEN", ""))
    if t:
        return t
    f = token_file(url)
    if f.exists():
        try:
            return core.clean_token(f.read_text(encoding="utf-8"))
        except OSError:
            return None
    return None


# ---------------------------------------------------------------- outbox
def outbox() -> Path:
    d = HOME / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_report(kind: str, text: str, files=(), hub: str = "staging", now: float | None = None) -> Path:
    """Put a report in the outbox (sent by flush, now or later). files: local paths (pictures, clip folders)."""
    now = now or time.time()
    item = core.day_item_id(dt.datetime.fromtimestamp(now).date())
    rec = {"kind": kind, "text": text, "files": [str(f) for f in files or []], "hub": hub, "item": item,
           "queued": core.iso(now), "tries": 0}
    p = outbox() / core.outbox_name(now, kind)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return p


def pending() -> list[Path]:
    return sorted(outbox().glob("*.json"))


def _file_bytes(p: Path, max_bytes: int):
    """(name, bytes) for a file, or a folder zipped in memory; None if missing or over max_bytes."""
    if p.is_dir():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(p).as_posix())
                    if buf.tell() > max_bytes:
                        return None
        data, name = buf.getvalue(), p.name + ".zip"
    elif p.is_file():
        if p.stat().st_size > max_bytes:
            return None
        data, name = p.read_bytes(), p.name
    else:
        return None
    return (name, data) if len(data) <= max_bytes else None


def upload_private(hub: Hub, login: str, p: Path, max_bytes: int) -> dict:
    """One attachment into the Private tier; returns its reference (or a local-only entry)."""
    got = _file_bytes(p, max_bytes)
    if not got:
        return {"name": p.name, "local": True}
    name, data = got
    sha = hashlib.sha256(data).hexdigest()
    path = core.private_path(login)
    parts = core.file_chunks(data)
    t = dt.datetime.now(dt.timezone.utc)
    for i, b64 in enumerate(parts):
        hub.private_put(path, core.file_doc_id(sha, i), core.chunk_event(name, sha, i, len(parts), b64, t))
    return {"name": name, "bytes": len(data), "sha256": sha, "private_path": path, "chunks": len(parts)}


def ensure_day_item(hub: Hub, me: dict, item: str, cache: dict) -> None:
    if cache.get(item):
        return
    if not core.item_exists(hub.events(), item):
        hub.post(core.day_item_event(item, me, dt.datetime.now(dt.timezone.utc)), "gt" + hashlib.sha256(item.encode()).hexdigest()[:20])
    cache[item] = True


def flush(max_mb: float = 10, log=None) -> list[dict]:
    """Send every queued report it can. Returns [{file, kind, status: sent|queued, detail}] (detail never holds
    secrets). A report stays queued when the PC is offline, nobody is signed in, or the account isn't the owner."""
    results, hubs, cache, dead = [], {}, {}, set()
    for p in pending():
        try:
            if hub_url(json.loads(p.read_text(encoding="utf-8")).get("hub", "staging")) in dead:
                continue
        except (OSError, ValueError):
            continue
        claim = p.with_suffix(".sending")
        try:
            os.replace(p, claim)          # another Gate process may be flushing too
        except OSError:
            continue
        rec = json.loads(claim.read_text(encoding="utf-8"))
        url = hub_url(rec.get("hub", "staging"))
        status, detail = "queued", ""
        try:
            if url not in hubs:
                tok = stored_token(url)
                if not tok:
                    raise HubError("not signed in (Sign in in the Gate window)", 401)
                h = Hub(url, tok)
                hubs[url] = (h, h.me())
            h, me = hubs[url]
            if not me.get("owner"):
                raise HubError("owner only: this sign-in isn't the owner", 403)
            ensure_day_item(h, me, rec["item"], cache)
            atts = []
            for f in rec.get("files") or []:
                try:
                    atts.append(upload_private(h, me.get("login", ""), Path(f), int(max_mb * 1024 * 1024)))
                except HubError as e:
                    atts.append({"name": Path(f).name, "local": True})
                    detail = f"attachment kept on this PC ({e})"
            h.post(core.report_event(rec["item"], me, rec["text"], dt.datetime.now(dt.timezone.utc), rec["kind"], atts),
                   "gr" + hashlib.sha256((claim.name + rec["queued"]).encode()).hexdigest()[:20])
            status = "sent"
            sent = outbox() / "sent"
            sent.mkdir(exist_ok=True)
            rec["sent"], rec["attachments"] = core.iso(time.time()), atts
            (sent / p.name).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            claim.unlink()
        except (HubError, KeyError, ValueError) as e:
            detail = str(e)
            rec["tries"] = rec.get("tries", 0) + 1
            rec["last_error"] = detail
            claim.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            os.replace(claim, p)
            if isinstance(e, HubError) and e.code in (0, 401, 403):
                dead.add(url)
        results.append({"file": p.name, "kind": rec.get("kind"), "item": rec.get("item"), "status": status, "detail": core.scrub(detail)})
    return results
