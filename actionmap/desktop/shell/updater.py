"""
Nodach Desktop updates: find the newest release, download nodach-desktop-<ver>.zip and its .sha256, verify, unpack
to app\\_next, swap it in (the old files go to app\\_prev for one-click Roll back). Standard library only.

Where updates come from (desktop.json in %USERPROFILE%\\nodach):
- "update_url": a manifest URL ({"version", "zip", "sha256"}; zip/sha256 may be relative to the manifest). Wins
  when set (e.g. a hub endpoint, for a private repo).
- otherwise GitHub Releases of "repo" (default Morami49/nodach-studio), tags desktop-v<ver>, if the repo is public.
- otherwise nothing: the window says updates come from Claude in the linked chat for now.

The swap is done entry by entry inside app\\ (gate, pointer, live, shell, VERSION) while app\\.swapping exists, so the
shell waits; if any move fails, every move already made is undone. VERSION moves last.
Every check, update and rollback is appended to %USERPROFILE%\\nodach\\logs\\updates.jsonl.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = "Morami49/nodach-studio"
NOT_LINKED = "Updates come from Claude in the linked chat for now"
LOCK = ".swapping"
RESERVED = {"_next", "_prev", "_rollback", LOCK, "__pycache__", "_old"}
UA = {"User-Agent": "nodach-desktop-updater", "Accept": "application/vnd.github+json"}


class UpdateError(Exception):
    pass


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4]) or (0,)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(root: Path, kind: str, **fields):
    (Path(root) / "logs").mkdir(parents=True, exist_ok=True)
    with open(Path(root) / "logs" / "updates.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"schema": "desktop.update.v1", "ts": now_iso(), "kind": kind, **fields}, ensure_ascii=False) + "\n")


def load_config(root: Path) -> dict:
    try:
        cfg = json.loads((Path(root) / "desktop.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    return {"update_url": cfg.get("update_url") or "", "repo": cfg.get("repo") or DEFAULT_REPO,
            "autostart_window": cfg.get("autostart_window", True)}


def http_get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str, getter=http_get):
    return json.loads(getter(url).decode("utf-8"))


# ---------------------------------------------------------------- finding the newest release
def pick_release(releases: list) -> dict | None:
    """Newest non-draft desktop-v* release that has nodach-desktop-<ver>.zip and .zip.sha256 (or .sha256)."""
    best = None
    for r in releases or []:
        tag = r.get("tag_name") or ""
        if r.get("draft") or not tag.startswith("desktop-v"):
            continue
        ver = tag[len("desktop-v"):]
        assets = {a.get("name"): a.get("browser_download_url") for a in r.get("assets") or []}
        z = assets.get(f"nodach-desktop-{ver}.zip")
        s = assets.get(f"nodach-desktop-{ver}.zip.sha256") or assets.get(f"nodach-desktop-{ver}.sha256")
        if not (z and s):
            continue
        if best is None or vtuple(ver) > vtuple(best["version"]):
            best = {"version": ver, "zip": z, "sha256": s, "source": f"GitHub release {tag}"}
    return best


def find_update(root: Path, getter=http_get) -> dict:
    """{"available": bool, "version", "zip", "sha256", "source"} or {"available": False, "message"}."""
    cfg = load_config(root)
    if cfg["update_url"]:
        m = get_json(cfg["update_url"], getter)
        base = cfg["update_url"]
        return {"available": True, "version": str(m["version"]), "zip": urllib.parse.urljoin(base, m["zip"]),
                "sha256": urllib.parse.urljoin(base, m["sha256"]), "source": "update_url"}
    try:
        repo = get_json(f"https://api.github.com/repos/{cfg['repo']}", getter)
    except urllib.error.HTTPError as e:
        if e.code == 404:          # private (or gone): GitHub answers 404 without a sign-in
            return {"available": False, "message": NOT_LINKED}
        raise
    if repo.get("private"):
        return {"available": False, "message": NOT_LINKED}
    rel = pick_release(get_json(f"https://api.github.com/repos/{cfg['repo']}/releases?per_page=50", getter))
    if not rel:
        return {"available": False, "message": "No Nodach Desktop release has been published yet."}
    return dict(rel, available=True)


# ---------------------------------------------------------------- verify and unpack
def parse_sha(text: str) -> str:
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    if not m:
        raise UpdateError("the .sha256 file has no checksum in it")
    return m.group(1).lower()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(zip_path: Path, sha_text: str) -> str:
    want, got = parse_sha(sha_text), sha256_of(zip_path)
    if want != got:
        raise UpdateError(f"checksum doesn't match (expected {want[:12]}..., got {got[:12]}...): not installed")
    return got


def unpack_to_next(zip_path: Path, app: Path) -> Path:
    """Extract the zip's nodach-desktop/app/ into app/_next (replacing an old _next). Refuses unsafe paths."""
    app = Path(app)
    nxt = app / "_next"
    if nxt.exists():
        shutil.rmtree(nxt)
    tmp = Path(tempfile.mkdtemp(prefix="nodach-next-", dir=str(app)))
    try:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or ":" in name:
                    raise UpdateError(f"unsafe path in the zip: {name}")
            z.extractall(tmp)
        src = next((p for p in (tmp / "nodach-desktop" / "app", tmp / "app") if (p / "VERSION").exists()), None)
        if not src:
            raise UpdateError("the zip has no nodach-desktop/app/VERSION")
        os.replace(src, nxt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return nxt


# ---------------------------------------------------------------- swap and roll back
def entries(folder: Path) -> list[str]:
    """What gets swapped: every top-level entry except _next/_prev/locks, VERSION last."""
    if not folder.exists():
        return []
    names = sorted(p.name for p in folder.iterdir() if p.name not in RESERVED and not p.name.startswith("nodach-next-"))
    return [n for n in names if n != "VERSION"] + (["VERSION"] if "VERSION" in names else [])


def _move_set(moves: list[tuple[Path, Path]], mover=os.replace):
    done = []
    try:
        for a, b in moves:
            if a.exists():
                mover(a, b)
                done.append((a, b))
    except OSError:
        for a, b in reversed(done):
            try:
                os.replace(b, a)
            except OSError:
                pass
        raise


def swap(app: Path, source: str = "_next", keep: str = "_prev", mover=os.replace) -> dict:
    """Move app/* -> app/<keep>/ and app/<source>/* -> app/. On failure nothing is left half-done."""
    app = Path(app)
    src, kept = app / source, app / keep
    if not (src / "VERSION").exists():
        raise UpdateError(f"nothing to swap in: {source} has no VERSION")
    lock = app / LOCK
    lock.write_text(now_iso(), encoding="utf-8")
    hold = app / "_old"
    try:
        if hold.exists():
            shutil.rmtree(hold)
        hold.mkdir()
        old_names, new_names = entries(app), entries(src)
        out_moves = [(app / n, hold / n) for n in old_names]
        in_moves = [(src / n, app / n) for n in new_names]
        _move_set(out_moves, mover)
        try:
            _move_set(in_moves, mover)
        except OSError:
            for a, b in reversed(out_moves):
                if b.exists():
                    os.replace(b, a)
            raise
        # the swap is done: the old files become <keep>, the emptied source is removed
        if kept.exists():
            shutil.rmtree(kept, ignore_errors=True)
        os.replace(hold, kept)
        shutil.rmtree(src, ignore_errors=True)
        return {"from": _ver(kept), "to": _ver(app), "kept_as": keep}
    except OSError as e:
        raise UpdateError(f"couldn't swap the files ({type(e).__name__}: {e}). Close any window showing the "
                          "nodach\\app folder and try again; nothing was changed.") from e
    finally:
        if hold.exists() and not any(hold.iterdir()):
            hold.rmdir()
        lock.unlink(missing_ok=True)


def rollback(app: Path, mover=os.replace) -> dict:
    """Swap the current files and app/_prev (so Roll back can be undone the same way)."""
    app = Path(app)
    prev = app / "_prev"
    if not (prev / "VERSION").exists():
        raise UpdateError("there is no earlier version to roll back to")
    tmp = app / "_rollback"
    if tmp.exists():
        shutil.rmtree(tmp)
    os.replace(prev, tmp)
    try:
        r = swap(app, source="_rollback", keep="_prev", mover=mover)
    except UpdateError:
        if tmp.exists() and not prev.exists():
            os.replace(tmp, prev)
        raise
    return r


def _ver(folder: Path) -> str:
    try:
        return (folder / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "?"


def current_version(app: Path) -> str:
    return _ver(Path(app))


def update(root: Path, app: Path, getter=http_get, force: bool = False, progress=lambda s: None) -> dict:
    """The Update button: find, download, verify, unpack, swap. Returns {"status", "message", ...}."""
    root, app = Path(root), Path(app)
    cur = current_version(app)
    try:
        progress("Checking for updates...")
        info = find_update(root, getter)
        if not info.get("available"):
            log(root, "check", current=cur, result="none", message=info.get("message"))
            return {"status": "none", "message": info.get("message")}
        if vtuple(info["version"]) <= vtuple(cur) and not force:
            log(root, "check", current=cur, latest=info["version"], result="up_to_date", source=info["source"])
            return {"status": "up_to_date", "message": f"Up to date ({cur}).", "latest": info["version"]}
        progress(f"Downloading {info['version']}...")
        with tempfile.TemporaryDirectory(prefix="nodach-dl-") as d:
            zp = Path(d) / f"nodach-desktop-{info['version']}.zip"
            zp.write_bytes(getter(info["zip"]))
            sha = verify(zp, getter(info["sha256"]).decode("utf-8", "replace"))
            progress("Verified. Unpacking...")
            unpack_to_next(zp, app)
        got = _ver(app / "_next")
        if got != info["version"]:
            shutil.rmtree(app / "_next", ignore_errors=True)
            raise UpdateError(f"the zip says {got}, the release says {info['version']}: not installed")
        progress("Swapping in the new files...")
        r = swap(app)
        log(root, "update", **r, sha256=sha, source=info["source"])
        return {"status": "updated", "message": f"Updated {r['from']} -> {r['to']}. Claude picks it up in a few seconds "
                "(no restart). Roll back returns to " + r["from"] + ".", **r}
    except (UpdateError, OSError, ValueError, KeyError) as e:
        msg = str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}"
        log(root, "update_failed", current=cur, error=msg)
        return {"status": "failed", "message": "Update failed: " + msg}


def do_rollback(root: Path, app: Path) -> dict:
    try:
        r = rollback(Path(app))
        log(root, "rollback", **r)
        return {"status": "rolled_back", "message": f"Rolled back {r['from']} -> {r['to']}. Roll back again to return to {r['from']}.", **r}
    except (UpdateError, OSError) as e:
        log(root, "rollback_failed", error=str(e))
        return {"status": "failed", "message": "Roll back failed: " + str(e)}


def install_from_folder(src_app: Path, app: Path) -> dict:
    """Installer: put a fresh copy of src_app in place, through the same _next -> swap path (old copy kept)."""
    app = Path(app)
    app.mkdir(parents=True, exist_ok=True)
    nxt = app / "_next"
    if nxt.exists():
        shutil.rmtree(nxt)
    shutil.copytree(src_app, nxt, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_next", "_prev"))
    if not entries(app):           # first install: nothing to keep
        for n in entries(nxt):
            os.replace(nxt / n, app / n)
        shutil.rmtree(nxt, ignore_errors=True)
        return {"from": None, "to": _ver(app), "kept_as": None}
    return swap(app)
