# Nodach Gate v0.1.0

Gate lets Claude (the Claude desktop app) use your Windows PC through the open-source
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP) (MIT), but only through a gate that:

- offers Claude **UI tools only**: no PowerShell, files, registry, processes or clipboard;
- **refuses** password managers, banking/payment pages, Windows Security/UAC, Settings > Accounts/Privacy, and Claude's own window;
- **asks you first** (a small popup) before anything that sends, posts, deletes, pays, installs or signs out;
- **logs every action** as process data, with small local pictures where they help, and can send filtered reports to Studio.

It's the early Driver module of Nodach Studio. Windows-MCP reads the screen as UI-tree text (far fewer tokens than
screenshots). Windows-MCP never asks permission on its own: Gate sits in front of it.

## Install (click only)

1. Python 3.14 from <https://www.python.org/downloads/windows/> (tick **Add python.exe to PATH**, keep **tcl/tk** ticked).
2. Unzip `nodach-gate-v0.1.0.zip` anywhere, open the `nodach-gate` folder.
3. Double-click **`install-gate.bat`**. It:
   - installs the packages for your user (`mcp`, `windows-mcp`, `pywin32`, `uiautomation`, `pillow`, `psutil`);
   - copies Gate to `%USERPROFILE%\nodach-gate\app` and puts `zones.json` in `%USERPROFILE%\nodach-gate` (yours is never overwritten);
   - adds **nodach-gate** to Claude desktop's config (`%APPDATA%\Claude\claude_desktop_config.json`, and the Microsoft Store
     copy if you have that one), with the full path of `python.exe`. Every other server stays; a backup of the old file is kept
     next to it as `claude_desktop_config.json.bak-<date-time>`. A raw Windows-MCP entry, if you had one, is taken out
     (it would let Claude skip Gate) and is still in the backup.
4. **Quit Claude from the tray** (right-click the Claude icon by the clock → Quit) and open it again.
5. Check: Claude → **Settings → Developer** shows `nodach-gate` running. The **Nodach Gate** window opens by itself
   (or double-click `start-gate.bat` in `%USERPROFILE%\nodach-gate\app`).
6. Once, to send reports to Studio: in the Gate window pick the hub (**staging** by default), click **Open Studio**, sign in,
   press **F12** → **Application** → **Cookies** → the hub's address → `nodach_session` → copy its **Cookie Value**, close DevTools
   and click **Sign in**. The value is kept in `%USERPROFILE%\nodach-gate\session-<hub>.txt` and cleared from the clipboard.

Try it: ask Claude *"Open Notepad and type hello"*. Then *"Click Send"* in an email draft: the popup should appear.

## What Claude can use

| Offered (Windows-MCP UI tools) | Hidden (never offered, whatever they're called) |
|---|---|
| Snapshot, Screenshot, Click, Type, Scroll, Move (incl. drag), Shortcut, Wait, WaitFor, App (launch by name / switch / resize), ControlStatus | PowerShell, FileSystem, Registry, Process, Clipboard, Scrape, MultiSelect, MultiEdit, Notification, DisplayInventory, and any tool a later Windows-MCP adds |

Plus Gate's own: `capture_view(note)` (Claude asks for a local screenshot, with why) and `gate_status`.
It's an **allowlist**: Windows-MCP is started with `--tools` set to the list above, and anything it still lists is hidden by
Gate as well. The hidden names are in the log's first line each start. Telemetry in Windows-MCP is switched off.
Claude must act by `loc=[x, y]` (the centre the Snapshot shows) so Gate can check the element there; `label` and
`save_path` (which writes files) are refused, and so is `App` with `launch_executable` (it runs any program with arguments).

## What Gate blocks or asks about (zones)

Checked before every action against the window the action lands in (its app and title) and the element's name.

| Zone | What | What happens |
|---|---|---|
| **never** | Password managers (Bitwarden, 1Password, KeePass/KeePassXC, LastPass, Dashlane, …); windows titled with bank, banking, PayPal, Revolut, Monzo, Starling, Barclays, HSBC, Lloyds, NatWest, Santander, Nationwide, Halifax, checkout, payment, card number; fields named card number / CVV / sort code / account number; Windows Security and UAC prompts; Settings > Accounts and Privacy & security pages; launching PowerShell, Command Prompt, Terminal or Registry Editor; Gate's own windows; clicking or typing in Claude's own window | Refused with the reason, no popup. Snapshot/Screenshot are refused while such a window is in front, and its contents are cut out of any Snapshot. No pictures are ever taken of it. |
| **confirm** | Clicks/keys on elements named send, post, publish, submit, delete, remove, uninstall, install, pay, buy, confirm, sign out, format (and a few more); Enter after typing into a message/comment/reply box; Delete or Shift+Delete; launching an app not on your auto list; typing into a password field; input into an open terminal; anything in Windows Settings except scrolling | A popup on your PC: **Allow once**, **Allow for 10 min in this app**, or **Block**. No answer in 60 s = Block. |
| **auto** | Everything else | Done and logged. |

Rules live in `%USERPROFILE%\nodach-gate\zones.json` (comments are the keys starting with `_`). Edit it in Notepad,
bump `revision`, add a line to `_changes`. Gate reloads it on the next action and keeps every version it has used in
`zones-history\`; each log line carries the rules' revision.

## The Gate window

- **Pause all** (big button): everything Claude asks is blocked until you click it again.
- **Screenshots** / **Clips** toggles; **Keep last 10 s** saves the clip buffer now.
- The live list: one row per action, coloured by zone (red never, amber confirm, green auto, blue clips/views, purple reports).
- **Studio**: hub picker, Open Studio, Sign in/out, **Copy report**, **Send report to Studio** (tick **include pictures** to attach
  the latest screenshots/clips), queued-report count and **Send queued now**.
- **Automatic reports**: the master switch **Send automatically** and one checkbox per case (below).

## Pictures and clips (local)

Kept in `%USERPROFILE%\nodach-gate\shots` and `clips`, referenced from the log.
- **Screenshot** (element + 40 px margin, PNG): when the target has no name or is canvas/custom-drawn/web content; before a
  confirm popup (shown in it); after an error or when the window looks unchanged afterwards; when Claude calls `capture_view`.
- **Clip**: the last 10 s of the window in front, 2 frames a second, downscaled, **in memory only**, saved as a GIF when an action
  errors, on a glitch (the screen changed with no action while Claude was driving), or when you click **Keep last 10 s**.
  Logged as `kind: "clip"` with the actions inside it.
- Never while a never-zone window is in front: a blank frame with the reason instead.

## Automatic reports to Studio

Each case is a checkbox (default in brackets): never-zone block [on, text only]; action error or glitch clip [on]; the same
action failing twice in a row [on]; a confirm blocked or timed out [on]; end of a Claude run, 5 min with no action [on];
session summary when Gate closes or after 30 min idle [on]; daily summary at 21:00 [off].
- The text is the same filtered report as **Send report to Studio**: counts, zones, apps and element names. Never typed text,
  window titles or sign-in values.
- It goes as a comment on the day's item **GATE-yymmdd** (created if missing; owner only).
- Pictures never go into the shared record. With **Attach pictures** on, a report's screenshots/clips go to your encrypted
  **Private tier** (`data/users/<you>/gate/files`, in 40 KB pieces, up to 10 MB a report) and the comment carries only the
  file name, size, checksum and private path. If the private store isn't available, they stay on this PC and the comment
  lists their names.
- Limits: 6 an hour, 5 min apart (never-zone blocks skip the gap), 20 a day. Offline, signed out or not the owner: queued in
  `outbox\` and sent later. Each one shows up in the list as a report row (queued / sent / skipped and why).

## Files (`%USERPROFILE%\nodach-gate`)

`actions.jsonl` (the gate.v1 log: ts, tool, args with typed text cut to 80 characters and masked for password/PIN fields,
app, window title cut to 80, element name/type, zone, reason, decision, by auto/user/grant/timeout, duration_ms, ok/error,
shot) · `zones.json` and `zones-history\` · `state.json` (the window's switches) · `shots\` · `clips\` · `outbox\` (and
`outbox\sent\`) · `session-<hub>.txt` (sign-in) · `app\` (the program).

## Undo

Quit Claude, open `%APPDATA%\Claude`, delete `claude_desktop_config.json` and rename the newest
`claude_desktop_config.json.bak-…` back to it. Reopen Claude.

## For developers

`gate_core.py` (pure rules, tested anywhere) · `gate_mcp.py` (the MCP server; `python gate_mcp.py --check` lists what is
offered and hidden) · `gate_win.py` (window/element lookups, pictures, clip buffer) · `gate_popup.py` · `gate_gui.py` ·
`gate_studio.py` (sign-in, outbox, private attachments) · `install_gate.py`. Tests: `python -m pytest -q actionmap/gate/tests`
(a fake Windows-MCP stands in, so they run on Linux).
