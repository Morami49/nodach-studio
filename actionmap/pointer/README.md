# Nodach Desktop Pointer (v0.1.8)

Point at anything in any Windows app, type a note, and paste the result into any AI chat.
Each point is also saved to a local log that Claude can read through MCP, and sent to ActionMap
when ActionMap is running.

## Daily use

1. Double-click `start-pointer.bat` (leave the black window open).
2. Hover over the thing you mean and press **Ctrl+Alt+M**. Type a note, press **Enter**.
3. Paste (**Ctrl+V**) into any chat.

Other keys: **Ctrl+Alt+B** adds the element under the mouse to a batch (the next Ctrl+Alt+M note
covers all of them); **Ctrl+Alt+Q** quits. In the note box, **Shift+Enter** is a new line and **Esc** discards.

### Web-view apps (the Claude app, the Copilot app, browsers) — new in v0.1.4

Windows can't see inside apps drawn as web pages; it only reports one big unnamed pane. For those the
pointer now takes a screenshot of the area around your mouse (about 900x600) and puts **the image and the
text together on the clipboard**. When you paste:

- into a chat that accepts images (Claude, ChatGPT, Copilot), you get the screenshot attached;
- into a plain text box (Notepad, a terminal), you get the text block.

The console window says `IMAGE + text copied to clipboard` when this happens. Normal named buttons and
fields keep the text-only behaviour. If a chat only shows the image and not your note, type the note
again in the chat, or paste once more into a text-only place to see the block.

## First-time setup (Windows)

Python must be on PATH: install it from <https://www.python.org/downloads/windows/> and tick
**Add python.exe to PATH**.

1. The zip holds one folder, `nodach-pointer`. Right-click `nodach-pointer-v0.1.8.zip` > **Extract All**, and
   extract to `C:\Users\akabd`, so that `C:\Users\akabd\nodach-pointer\pointer.py` exists (not
   `nodach-pointer\nodach-pointer\pointer.py`; if that happens, move the inner files up one level).
2. Open that folder in File Explorer, right-click an empty spot, choose **Open in Terminal**, and run:
   ```
   pip install -r requirements.txt
   ```
   This installs pywin32 too (new in v0.1.4, needed for the image clipboard).
3. Double-click `start-pointer.bat`. It should print `Nodach Desktop Pointer 0.1.8`.
4. Test: hover over a button, press **Ctrl+Alt+M**, type `test`, press Enter, and paste into Notepad.
   Then hover over the middle of a Claude app chat, do the same, and paste into a chat box: the image
   should attach.

### Optional: let Claude Desktop read your points directly

Double-click `connect-to-claude.bat`. It adds the `nodach-pointer` MCP server to every Claude Desktop
config file it finds (normal and Microsoft Store installs), keeps a dated backup of each, and leaves your
other servers alone. Then quit Claude fully (right-click its icon by the clock > **Quit**) and reopen it.
Ask Claude: *"Call get_pointed_elements and tell me what I pointed at."*

For Claude Code instead:
```
claude mcp add nodach-pointer -- python C:\Users\akabd\nodach-pointer\pointer_mcp.py
```

MCP tools: `get_pointed_elements(limit, status)`, `get_pointer_screenshot(event_id)`,
`mark_pointed(event_ids, status)` with statuses `new | seen | done | deferred`.

## Updating

- From a zip: close the pointer window (Ctrl+Alt+Q), extract the new zip to `C:\Users\akabd` so it lands on
  `C:\Users\akabd\nodach-pointer` (choose **Replace the files**), run `pip install -r requirements.txt` again, and start it.
  Your `events.jsonl`, `status.jsonl` and `shots\` live in the same folder by default and are not in the zip,
  so they are kept.
- From git (once you have a clone of `Morami49/nodach-studio`): `git pull`, then run from
  `actionmap\pointer` (set `NODACH_POINTER_HOME=C:\Users\akabd\nodach-pointer` if you want to keep using the
  old data folder).

## Files and data

| File | What it is |
|---|---|
| `pointer.py` | the hotkey tool (Windows only) |
| `pointer_core.py` | pure logic: web-view detection, crop boxes, pasted text, ActionMap event |
| `pointer_mcp.py` | MCP server so Claude can read your points (works with mcp 1.x and 2.x) |
| `test_pointer.py` | tests that run on any OS: `python actionmap/pointer/test_pointer.py` |
| `start-pointer.bat` | starts the pointer |
| `connect-to-claude.bat`, `install-claude-config.ps1` | add the MCP server to Claude Desktop |

Data goes to `%USERPROFILE%\nodach-pointer` (override with `NODACH_POINTER_HOME`):
`events.jsonl` (append-only, schema `pointer.v1`, ids `PE-yymmdd-hhmmss-xxxx`), `shots\<id>.png`, and
`status.jsonl` (append-only status changes). Nothing is ever rewritten, so earlier states stay visible.
v0.1.4 adds two fields to each event: `opaque` (true for web views) and `shot_box` (the crop rectangle).

ActionMap: if ActionMap runs at `ACTIONMAP_URL` (default `http://127.0.0.1:8765`), each point is POSTed to
`/events` as `kind: annotation, action: point`, actor `human:<ACTIONMAP_HUMAN_ID>` (default `local`), with
`ACTIONMAP_TOKEN` sent as `X-ActionMap-Token` when set.

Privacy: the pointer never reads field values (typed passwords are not captured), but screenshots show
whatever is on screen.

## Versions

- 0.1.4: web views (F-pointer-webview): cursor-centred 900x600 crop, image + text on the clipboard, console hint; pywin32; pure logic split into `pointer_core.py` with tests.
- 0.1.5: big named containers (e.g. the Claude app's "Primary pane") also take the picture path; small named elements stay text-only.
- 0.1.6: unnamed containers (page sections with no label) take the picture path at any size; unnamed buttons and other small controls stay text-only.
- 0.1.7: the pasted picture carries the text block as a caption band underneath, because chat apps paste only the picture when both are on the clipboard.
- 0.1.8: every point copies the captioned picture (not only web views), so a paste always looks the same; the caption font is Segoe UI so dashes no longer show as boxes. From the Claude desktop app, Claude can also read points directly (no paste needed).
- 0.1.3: hotkey Ctrl+Alt+M (ActionMap uses Ctrl+Alt+P); MCP server works with mcp 1.x and 2.x; points sent to ActionMap.
- 0.1.1: `connect-to-claude.bat` config installer.
- 0.1.0: first version.
