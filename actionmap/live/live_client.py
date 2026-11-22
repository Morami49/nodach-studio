"""
Nodach Live client (B-live-session), command-line version
=========================================================
(The click-only window is live_gui.py, started by start-live.bat; this file is start-live-cli.bat. Both share the
Hub, Live and speech-model code below.)
Talk into a Nodach Studio live session from your Windows PC. Speech is transcribed HERE, on your own CPU
(faster-whisper); the audio never leaves the PC. Only the text of each line is sent, to the hub, signed in as you.

  Ctrl+Alt+J      push to talk: press to start, press again to send what you said
                  (was Ctrl+Alt+Space, which the Claude desktop app uses for its quick-entry box)
  Ctrl+Alt+A      always-on: toggle listening; each phrase (a pause ends it) becomes a line
  Ctrl+Alt+L      send the pointer's latest point on its own (from the Nodach Desktop Pointer, Ctrl+Alt+M)
  or type in this window: a line + Enter sends it · /point · /always · /end ends the session · /quit

A line starting "task:" or "build:" also makes a draft change item linked to the session (owners only; drafts are
never built until approved). A fresh pointer point (made in the last 2 minutes, not used yet) rides on the next line.

Sign-in: the client uses your Studio session (the nodach_session cookie). The first run asks for it once and keeps
it in %USERPROFILE%\\nodach-live\\ (one file per hub); it is never printed. Or set NODACH_LIVE_TOKEN.
No microphone, no faster-whisper, or --typed: typed lines only.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_core as core  # noqa: E402

HOME = Path(os.environ.get("NODACH_LIVE_HOME", str(Path.home() / "nodach-live")))
POINTER_EVENTS = Path(os.environ.get("NODACH_POINTER_HOME", str(Path.home() / "nodach-pointer"))) / "events.jsonl"
DEFAULT_HUB = os.environ.get("NODACH_HUB", "https://staging.nodach.com")
RATE = 16000
FRAME_MS = 30
HOTKEYS = {core.GUI_HOTKEY: "talk", "<ctrl>+<alt>+a": "always", "<ctrl>+<alt>+l": "point"}

# Optional packages, guarded so this module imports anywhere (tests, typed-only use).
try:
    import numpy as np
    import sounddevice as sd
except Exception as _e:  # ImportError, or PortAudio missing
    np = sd = None
    AUDIO_MISSING = str(_e)
else:
    AUDIO_MISSING = ""
try:
    from pynput import keyboard
except Exception:
    keyboard = None


# ---------------------------------------------------------------- hub
class HubError(RuntimeError):
    """A hub answer that wasn't a success; .code is the HTTP status (0 when the hub couldn't be reached)."""
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class Hub:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._cookie = "nodach_session=" + token   # sent only to this hub, never printed
        self.last_status = 0

    def __repr__(self):   # never shows the sign-in value
        return f"Hub({self.base!r})"

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Cookie": self._cookie, "Accept": "application/json", "User-Agent": f"nodach-live/{core.VERSION}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "x-nodach-studio": "1"})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                self.last_status = r.status
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            self.last_status = e.code
            try:
                msg = json.loads(e.read()).get("error", "")
            except Exception:
                msg = ""
            raise HubError(f"hub said {e.code}{': ' + msg if msg else ''}", e.code) from None
        except (urllib.error.URLError, OSError) as e:
            self.last_status = 0
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

    def post(self, event: dict, did: str | None = None) -> str:
        did = did or core.doc_id()
        self._req("POST", "/studio/api/events", {"doc_id": did, "event": event})
        return did


def _clipboard() -> str:
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        v = r.clipboard_get()
        r.destroy()
        return v
    except Exception:
        return ""


def token_file(hub_url: str) -> Path:
    host = urllib.parse.urlparse(hub_url).hostname or "hub"
    return HOME / f"session-{host}.txt"


def save_token(hub_url: str, t: str) -> None:
    """Keep the sign-in value in the one session file for this hub (the only place it is ever written)."""
    f = token_file(hub_url)
    HOME.mkdir(parents=True, exist_ok=True)
    f.write_text(t, encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def forget_token(hub_url: str) -> None:
    try:
        token_file(hub_url).unlink()
    except OSError:
        pass


def stored_token(hub_url: str):
    """The sign-in from NODACH_LIVE_TOKEN or the saved file, without asking; None if there isn't a good one."""
    t = core.clean_token(os.environ.get("NODACH_LIVE_TOKEN", ""))
    if t:
        return t
    f = token_file(hub_url)
    if f.exists():
        try:
            return core.clean_token(f.read_text(encoding="utf-8"))
        except OSError:
            return None
    return None


def ask_token(hub_url: str) -> str:
    host = urllib.parse.urlparse(hub_url).hostname or "hub"
    print(f"Sign-in for {host}: copy the nodach_session Cookie Value in your browser (see README),")
    print("then just press Enter here: it's read from your clipboard and never shown.")
    for _ in range(3):
        typed = getpass.getpass("Press Enter (or right-click to paste, then Enter): ")
        t = core.clean_token(typed) or core.clean_token(_clipboard())
        if t:
            save_token(hub_url, t)
            print("  Sign-in value read.")
            return t
        print("  That isn't a nodach_session value (it looks like name.numbers.long-code). Copy it again and retry.")
    raise SystemExit("No sign-in value; run start-live.bat again when you have it copied.")


def load_token(hub_url: str) -> str:
    return stored_token(hub_url) or ask_token(hub_url)


# ---------------------------------------------------------------- the session
class Live:
    def __init__(self, hub: Hub, me: dict, session: str, attach_points: bool = True, report=None,
                 check: bool = True, on_switch=None, on_ask=None):
        """report(step, outcome, detail, message): called after every send; default prints like the CLI always did.
        check: before each send, re-read the session list and don't write into a session that has ended
        (core.send_target). on_switch(old, new) / on_ask(old, live_ids): told when it moved on or needs a pick;
        lines it couldn't place wait in self.held (take_held() hands them to the next session)."""
        self.hub, self.me, self.session, self.attach = hub, me, session, attach_points
        self.report = report or (lambda step, outcome, detail, message: print("  " + message))
        self.check, self.on_switch, self.on_ask = check, on_switch, on_ask
        self.held: list = []
        self.used_points: set = set()
        self.out: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._sender, daemon=True).start()

    def close(self) -> None:
        """Let queued lines land, then stop this session's sender."""
        self.out.put(None)

    def _log(self, row: dict) -> None:   # append-only local record of what was sent (or not)
        HOME.mkdir(parents=True, exist_ok=True)
        with (HOME / "lines.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _point(self):
        if not self.attach or not POINTER_EVENTS.exists():
            return None
        try:
            lines = POINTER_EVENTS.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            return None
        p = core.latest_point(lines, core.utcnow(), self.used_points)
        if p:
            self.used_points.add(p.get("id"))
        return core.shareable_point(p)

    def say(self, text: str, via: str) -> None:
        text = core.clean_text(text)
        if text:
            self.out.put(("line", text, via, self._point()))

    def point_only(self) -> bool:
        p = self._point()
        if p:
            self.out.put(("line", "", "pointer", p))
            return True
        self.report("point", "failed", "no fresh pointer point in the last 2 minutes",
                    "No fresh pointer point (point with Ctrl+Alt+M in the Nodach pointer first).")
        return False

    def _sender(self) -> None:
        while True:
            job = self.out.get()
            if job is None:
                self.out.task_done()
                return
            _, text, via, point = job
            note = ""
            if self.check:
                go, note = self._target()
                if not go:
                    self.held.append(job)
                    self._log({"ts": core.iso(core.utcnow()), "session": self.session, "text": text, "via": via, "sent": False, "held": True})
                    self.out.task_done()
                    continue
            t = core.utcnow()
            ev = core.line_event(self.session, self.me, text, t, via, point)
            try:
                did = self.hub.post(ev)
                status = self.hub.last_status
                task = core.parse_task(text)
                drafted = draft_id = ""
                if task and self.me.get("owner"):
                    tev = core.task_event(self.session, did, task, self.me, core.utcnow())
                    self.hub.post(tev)
                    draft_id = tev["body"]["id"]
                    drafted = f"  → draft {draft_id}"
                self._log({"ts": ev["ts"], "session": self.session, "doc_id": did, "text": text, "via": via, "sent": True})
                self.report("send", "ok", core.send_detail(status, len(text), bool(point), draft_id) + (f" · {note}" if note else ""),
                            f"sent{' + point' if point else ''}: {text or '(point)'}{drafted}")
            except Exception as e:
                self._log({"ts": ev["ts"], "session": self.session, "text": text, "via": via, "sent": False, "error": str(e)[:200]})
                self.report("send", "failed", core.send_detail(getattr(e, "code", 0), len(text), bool(point), error=str(e)[:80]),
                            f"NOT sent ({e}). Kept in {HOME / 'lines.jsonl'}.")
            finally:
                self.out.task_done()

    def _target(self):
        """(send?, note). Re-reads the session list; switches to the one live session if this one ended, or holds
        the line and asks when it can't tell which. A failed read doesn't stop the line (noted on its send row)."""
        try:
            rows = core.session_list(self.hub.events(), self.me["id"], bool(self.me.get("owner")))
        except Exception as e:
            return True, f"session not re-checked (hub {getattr(e, 'code', 0) or 'unreachable'})"
        d = core.send_target(rows, self.session)
        if d["action"] == "send":
            return True, ""
        old = self.session
        if d["action"] == "switch":
            self.session = d["to"]
            self.report("session", "info", f"{old} is {d['state']}, not live: switched to {d['to']} (the only live session)",
                        f"{old} has ended ({d['state']}). Switched to {d['to']}, the only live session.")
            if self.on_switch:
                self.on_switch(old, d["to"])
            return True, f"switched from {old}"
        n = len(d["live"])
        self.report("session", "failed",
                    f"{old} is {d['state']}, not live; {n} live sessions: pick one (line held, sent once you pick)" if n
                    else f"{old} is {d['state']}, not live; no live session: start one (line held)",
                    f"{old} has ended. Line held: pick or start a live session.")
        if self.on_ask:
            self.on_ask(old, d["live"])
        return False, ""

    def take_held(self) -> list:
        jobs, self.held = self.held, []
        return jobs

    def resend(self, jobs) -> None:
        for j in jobs:
            self.out.put(j)

    def end(self) -> bool:
        self.out.join()   # every line lands before the session closes
        t = core.iso(core.utcnow())
        if not self.me.get("owner"):
            self.report("session", "failed", "only an owner can end a session", "Only an owner can end a session.")
            return False
        au = core.author(self.me)
        self.hub.post({"ts": t, "kind": "item.edit", "item": self.session, "branch": "main", "author": au,
                       "body": {"live": {"ended": t}, "body": f"Live session ended {t[:16].replace('T', ' ')} UTC from the desktop client."}})
        self.hub.post({"ts": t, "kind": "status", "item": self.session, "branch": "main", "author": au, "body": {"status": "open"}})
        self.report("session", "ok", f"ended {self.session} · hub {self.hub.last_status}", f"Ended {self.session}.")
        return True


# ---------------------------------------------------------------- voice (optional)
def model_roots():
    return [Path(__file__).resolve().parent / "models", HOME / "models"]


def load_model(model: str, say=print):
    """(WhisperModel, where-it-came-from). A local faster-whisper-<model> folder wins; else it downloads once."""
    from faster_whisper import WhisperModel   # optional; ImportError → typed only
    local = core.local_model_dir(model_roots(), model)
    if local:
        say(f"Loading the {model} speech model from {local}…")
        return WhisperModel(str(local), device="cpu", compute_type="int8"), f"local folder {local}"
    say(f"Loading the {model} speech model on this PC (first run downloads it once)…")
    try:
        return WhisperModel(model, device="cpu", compute_type="int8"), "downloaded"
    except Exception as e:
        raise RuntimeError(f"couldn't download the {model} model ({type(e).__name__}). Put the folder "
                           f"faster-whisper-{model} (from the Nodach model zip) into {HOME / 'models'} and rerun") from None


def transcribe(model, audio) -> str:
    segs, _ = model.transcribe(audio, language="en", vad_filter=True, beam_size=1)
    return core.clean_text(" ".join(s.text for s in segs))


class Voice:
    def __init__(self, live: Live, model: str):
        self.model, _ = load_model(model)
        self.live = live
        self._last_toggle = 0.0
        self.talking = False
        self.always = False
        self.buf: list = []
        self.lock = threading.Lock()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=RATE * FRAME_MS // 1000, callback=self._cb)
        self.stream.start()
        threading.Thread(target=self._always_loop, daemon=True).start()

    def _cb(self, data, frames, t, status):
        if self.talking or self.always:
            with self.lock:
                self.buf.append(data[:, 0].copy())

    def _transcribe(self, audio, via: str) -> None:
        secs = 0 if audio is None else len(audio) / RATE
        if secs < 0.3:
            if via == "voice":   # say so instead of dropping it silently (0.1.2 looked like it ignored the key)
                print(f"  Nothing to send: only {secs:.1f} s was recorded. Press once to start, speak, press again.")
            return
        text = transcribe(self.model, audio)
        if text:
            self.live.say(text, via)
        elif via == "voice":
            print(f"  No speech heard in {secs:.1f} s (check the microphone, or speak closer).")

    def _take(self, upto=None):
        with self.lock:
            frames, self.buf = (self.buf, []) if upto is None else (self.buf[:upto], self.buf[upto:])
        return np.concatenate(frames) if frames else None

    def toggle_talk(self) -> None:
        now = time.monotonic()
        if now - self._last_toggle < 0.4:   # one physical press can arrive twice; ignore the echo
            return
        self._last_toggle = now
        if self.always:
            print("  Always-on is listening already (Ctrl+Alt+A turns it off).")
            return
        if not self.talking:
            with self.lock:
                self.buf = []
            self.talking = True
            print("  ● listening… (Ctrl+Alt+J again to send)")
        else:
            self.talking = False
            audio = self._take()
            threading.Thread(target=self._transcribe, args=(audio, "voice"), daemon=True).start()

    def toggle_always(self) -> None:
        self.talking = False
        with self.lock:
            self.buf = []
        self.always = not self.always
        print("  ● always-on: every phrase becomes a line" if self.always else "  always-on off")

    def _always_loop(self) -> None:
        threshold = float(os.environ.get("NODACH_LIVE_LEVEL", "0.015"))
        quiet = 800 // FRAME_MS
        while True:
            time.sleep(0.25)
            if not self.always:
                continue
            with self.lock:
                levels = [float(np.sqrt(np.mean(f * f))) for f in self.buf]
            if len(levels) * FRAME_MS > 30000:   # 30 s without a pause: send what there is
                self._transcribe(self._take(), "voice-always")
                continue
            done = [s for s in core.segments(levels, threshold, FRAME_MS) if len(levels) - s[1] >= quiet]
            if done:
                end = done[-1][1]
                audio = self._take(end + quiet // 2)
                self._transcribe(audio, "voice-always")
            elif levels and max(levels) < threshold and len(levels) > 2 * quiet:
                self._take(len(levels) - quiet)   # drop old silence


# ---------------------------------------------------------------- main
def pick_session(hub: Hub, me: dict, want: str | None, start: bool) -> str | None:
    if want:
        return want
    if not start:
        sid = core.latest_live_session(hub.events(), me["id"])
        if sid:
            return sid
        if not me.get("owner"):
            print("No live session of yours. Ask an owner for the session id and run with --session LV-…")
            return None
        if input("No live session of yours is open. Start one? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return None
    ev = core.session_event(me, core.utcnow())
    hub.post(ev)
    return ev["body"]["id"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nodach Live: talk into a Studio live session (audio stays on this PC).")
    ap.add_argument("--hub", default=DEFAULT_HUB, help="hub address (default: staging, %(default)s)")
    ap.add_argument("--session", help="session id (LV-…); default: your newest live session")
    ap.add_argument("--start", action="store_true", help="start a new session")
    ap.add_argument("--typed", action="store_true", help="no microphone: typed lines only")
    ap.add_argument("--model", default=os.environ.get("NODACH_LIVE_MODEL", "base"), help="faster-whisper model: tiny, base or small")
    ap.add_argument("--no-point", action="store_true", help="never attach pointer points")
    a = ap.parse_args(argv)
    hub = Hub(a.hub, load_token(a.hub))
    try:
        me = hub.me()
    except Exception as e:
        if "401" not in str(e):
            print(f"Couldn't reach {a.hub} ({e}).")
            return 1
        # the stored value expired or was wrong: forget it and ask once more
        print("The saved sign-in wasn't accepted (expired, or from a different sign-in).")
        forget_token(a.hub)
        hub = Hub(a.hub, ask_token(a.hub))
        try:
            me = hub.me()
        except Exception as e2:
            print(f"Still couldn't sign in ({e2}). In the browser, open {a.hub}/studio, sign in with GitHub, "
                  "reload, copy nodach_session again, and rerun.")
            return 1
    sid = pick_session(hub, me, a.session, a.start)
    if not sid:
        return 1
    live = Live(hub, me, sid, attach_points=not a.no_point)
    print(f"Nodach Live {core.VERSION} · {a.hub} · session {sid} · signed in as {'an owner' if me.get('owner') else 'a member'}")
    voice = None
    if not a.typed:
        if AUDIO_MISSING:
            print(f"No microphone support ({AUDIO_MISSING}); typed lines only. Run: pip install -r requirements.txt")
        else:
            try:
                voice = Voice(live, a.model)
            except Exception as e:
                print(f"Voice is off ({type(e).__name__}: {e}); typed lines only.")
    if voice and keyboard is not None:
        jobs = {"talk": voice.toggle_talk, "always": voice.toggle_always, "point": live.point_only}
        keyboard.GlobalHotKeys({k: jobs[j] for k, j in HOTKEYS.items()}).start()
        print("Ctrl+Alt+J talk/send · Ctrl+Alt+A always-on · Ctrl+Alt+L send latest point · or type below")
    else:
        print("Type a line and press Enter. /point sends the pointer's latest point · /end · /quit")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        cmd = line.strip().lower()
        if cmd in ("/quit", "/q"):
            break
        if cmd == "/end":
            live.end()
            break
        if cmd == "/point":
            live.point_only()
        elif cmd == "/always" and voice:
            voice.toggle_always()
        elif cmd == "/help":
            print(__doc__)
        elif line.strip():
            live.say(line, "typed")
    live.out.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
