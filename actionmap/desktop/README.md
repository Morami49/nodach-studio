# Nodach Desktop 0.1.0 (the first Driver shell)

One install for Nowed's Windows desktop tools: Gate (Claude's hands, behind Nowed's rules), Pointer (point at
something, Claude reads it), Live (talk into a Studio live session). It ends the download, extract, relaunch-Claude
cycle: after the first install, new versions load without restarting Claude.

## Install (once)

1. Double-click `install-desktop.bat` (Python 3.14 needed: same as Gate).
2. Open Claude > Settings > Extensions and turn off **Nodach Pointer**. Nodach Desktop now serves the pointer.
3. Quit Claude from the tray (right-click the Claude icon by the clock > Quit) and reopen it.

The installer copies the code to `%USERPROFILE%\nodach\app` (`shell`, `gate`, `pointer`, `live`), leaves the data where
it already was (`%USERPROFILE%\nodach-gate`, `\nodach-pointer`, `\nodach-live`: logs, zones and sign-ins carry
over), and in both Claude configs (normal and Microsoft Store) adds `nodach-desktop` and takes out `nodach-gate`
and `nodach-pointer`. A backup of each config is kept next to it.

## How reloading works

```
Claude desktop --stdio--> app\shell\shell_mcp.py  (stable, standard library only)
                              --stdio--> app\shell\worker.py  (Gate's tools + the Pointer's 3 tools)
```

- The shell watches `app\VERSION` and the files under `app\`. When they change, it lets running calls finish,
  restarts the worker and tells Claude the tool list changed (`notifications/tools/list_changed`).
- Claude can also call `desktop_reload` after writing files, and `desktop_status` to see versions, worker uptime,
  last reload and errors.
- If the worker crashes it restarts with backoff (1, 2, 5, 10, 30, 60 s). Meanwhile calls get a clear error.
- If `shell_mcp.py` itself changes, `desktop_status` and the window say: restart Claude once.
- Logs are append-only: `%USERPROFILE%\nodach\logs\shell.jsonl`, `updates.jsonl`, `worker-stderr.log`.

## The window

Opens when Claude starts Nodach Desktop (or from the Desktop shortcut). Mouse only:

- **STOP CLAUDE NOW**: pauses Gate (same as Pause all). It can always be clicked. The border and banner turn
  orange with the tool name while Claude is acting through Gate. Since Gate 0.1.5 this replaces Windows-MCP's own
  overlay.
- Gate on/paused (Pause / Resume, Gate log). Pointer on/off (Start / Stop the capture app). Live (Open Live).
- Versions, **Update** (newest `desktop-v*` GitHub Release: zip and sha256 are checked, the new files go to
  `app\_next` and are swapped in, the old ones are kept in `app\_prev`), **Roll back**, Reload tools, **Copy report**.

Updates come from `desktop.json` `update_url` if set (a manifest `{"version","zip","sha256"}`). If not, they come
from this repo's GitHub Releases while the repo is public. If neither is available, the window says updates come
from Claude in the linked chat.

## Releases

Push a tag `desktop-v<version>` that matches `actionmap/desktop/VERSION`. The `desktop-release` workflow then builds
`nodach-desktop-<version>.zip` and `.zip.sha256` (`python actionmap/desktop/build_zip.py`) and attaches them to the
release.

Tests (Linux): `python -m pytest -q actionmap/desktop/tests`
