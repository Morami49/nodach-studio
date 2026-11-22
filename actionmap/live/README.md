# Nodach Live (v0.2.2)

Talk into a Nodach Studio **live session** from your Windows PC while Claude and builders reply in the same
stream (Studio → **◉ Live**). Speech is turned into text **on your PC** (faster-whisper on the CPU); the audio
never leaves it. Only the text of each line goes to the hub, signed in as you.

**New in 0.2.2:**
- **No more lines into an ended session.** Before each line is sent, the app re-reads the session list. If the
  session you picked has ended and exactly one session is live, it switches to that one and says so in Activity
  (*session · info · … switched to LV-…*). If none or several are live, it holds the line, adds a red *session*
  row, turns the session list yellow with a note under it, and sends the held lines once you pick (or start) one.
- **Automatic reports** (tick box next to *Send report to Claude*, on by default): when a *record, transcribe,
  send, sign-in* or *session* step fails, the same filtered report is posted to the current session by itself, at
  most once every 5 minutes; a summary is posted when you end a session. If posting fails it's queued and tried
  again every minute. Each one leaves a row: *report · auto-report sent / queued*. `auto-reports.jsonl` in
  `%USERPROFILE%\nodach-live\` logs them (no report text).

From 0.2.0 it is a **window you click**: no command line and no keyboard shortcuts needed. (0.1.x used
Ctrl+Alt+Space, which is also the Claude desktop app's quick-entry shortcut, so it opened Claude's box instead.)

## The window, top to bottom

| # | Part | What it does |
|---|---|---|
| 1 | **Hub and sign-in** | `staging` or `production`; shows *Signed in as owner/member* or *Not signed in*. **Sign in** reads the `nodach_session` value from your clipboard (never shown). **Open Studio** opens the hub in your browser. |
| 2 | **Session** | Your sessions, live ones first (`LV-… · live`). **Refresh**, **Start new session**, **End session** (owners). |
| 3 | **Microphone** | Pick the microphone; the bar moves while it listens. Shows the speech model: *loading*, *ready*, or *not available → typed only*. If voice packages are missing, **Install voice packages** installs them (then close and reopen). |
| 4 | **● Talk** | Click, speak, click **■ Stop and send** (it shows a timer). The line is transcribed and sent. Tick **Keep listening** to have each pause send a line until you click **■ Stop listening**. **Enable keyboard shortcut** turns on **Ctrl+Alt+J** (same as clicking Talk); it's off by default and doesn't clash with the pointer (Ctrl+Alt+M/B/Q), ActionMap (Ctrl+Alt+P) or Claude (Ctrl+Alt+Space). |
| 5 | **Type a line** | Type and press **Enter** or **Send**. **Attach my latest pointer point** (on by default) adds the pointer's latest point to your next line; **Send latest point** sends it on its own. |
| 6 | **Activity** | Every step as a row: time, step (record / transcribe / send / sign-in / session / …), outcome (ok / failed / empty speech) and a short detail (seconds recorded, peak level, transcript length, hub status code). Failed rows are red. |
| 7 | **Copy report** / **Send report to Claude** | A plain-text report (client version, Windows version, hub, signed-in role, session, microphone, speech model, settings, the last 30 activity rows). *Copy* puts it on the clipboard to paste into a chat; *Send* posts it as a comment on the session so Claude can read it through the Studio connector. It never holds the sign-in value, audio, what you said, window titles, or file paths outside the `nodach-live` folder. |

- A line starting **`task:`** or **`build:`** also creates a **draft** change item linked to the session (status
  `draft`; nothing is built until an owner approves it). Speech often writes the colon as a comma or full stop;
  those count too.
- A pointer point (Nodach Desktop Pointer, **Ctrl+Alt+M**, in the last 2 minutes) carries app name, element type
  and name, and your note. Window titles, screenshots and file paths stay on the PC.

## First-time setup (Windows)

1. Python on PATH: <https://www.python.org/downloads/windows/> (tick **Add python.exe to PATH**; keep *tcl/tk* ticked).
2. Unzip the Nodach Live zip (it holds the `nodach-live` folder). Updating from 0.2.x: unzip `nodach-live-v0.2.2-update.zip`
   over your existing `nodach-live` folder (it has no `models` folder, so your speech model stays).
3. Double-click **`start-live.bat`**. The window opens. If section 3 says voice packages aren't installed, click
   **Install voice packages**, wait for the *installed* row in Activity, then close and reopen the window.
4. Sign in (once every 30 days): click **Open Studio**, sign in, press **F12** → **Application** tab → **Cookies** →
   the hub's address → click `nodach_session` → copy its **Cookie Value**. Close DevTools and click **Sign in** in
   the window. The value is kept in `%USERPROFILE%\nodach-live\session-<hub>.txt` and removed from the clipboard.
5. Pick a live session, or click **Start new session**. Click **● Talk**.

If something goes wrong, click **Send report to Claude** (or **Copy report** and paste it into the chat).

The command-line client is still there: **`start-live-cli.bat`** (push-to-talk is now **Ctrl+Alt+J**,
always-on **Ctrl+Alt+A**, latest point **Ctrl+Alt+L**; type `/point`, `/always`, `/end`, `/quit`). Its options:
`--hub https://studio.nodach.com`, `--session LV-…`, `--start`, `--typed`, `--model tiny|base|small`, `--no-point`.
The window takes `--hub staging|production` and `--model`. `NODACH_LIVE_LEVEL` (default `0.015`) sets how loud
counts as speech.

## Files and data

| File | What it is |
|---|---|
| `live_gui.py` | the window (tkinter, ships with Python) |
| `live_client.py` | hub, session sender, speech model; also the command-line client |
| `live_core.py` | pure logic: task lines, event shapes, pointer point, phrase cutting, session list, microphone list, activity rows, the report |
| `test_live.py` | tests that run anywhere, no microphone or screen: `python -m pytest actionmap/live/test_live.py` |
| `start-live.bat` | opens the window |
| `start-live-cli.bat` | opens the command-line client |

In `%USERPROFILE%\nodach-live\`: `lines.jsonl` is an append-only log of every line sent or not sent (text only);
`activity.jsonl` is an append-only copy of the window's activity rows (no text you said); `gui-error.txt` appears
only if the window couldn't start; `auto-reports.jsonl` lists automatic reports sent or queued. Events go to `POST /studio/api/events` with your `nodach_session` cookie and the
`x-nodach-studio` header, exactly like Studio's own writes, so the hub's access rules apply: owners can write
anywhere, members only into a session they were given.
