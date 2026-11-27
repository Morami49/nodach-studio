"""
Nodach Desktop installer (run by install-desktop.bat; one click).

1. Installs the Python packages for this user (pip --user): mcp, windows-mcp, pywin32, uiautomation, pillow, psutil,
   pynput (Gate + Pointer). Live's voice packages are tried too, but Live works typed-only without them.
2. Copies app\\ (shell, gate, pointer, live) to %USERPROFILE%\\nodach\\app. A copy already there is kept as
   app\\_prev (Roll back in the window). Data stays where it is: %USERPROFILE%\\nodach-gate, \\nodach-pointer,
   \\nodach-live (logs, zones and sign-ins carry over).
3. Edits BOTH Claude desktop configs (%APPDATA%\\Claude and, if Claude is the Microsoft Store/MSIX app,
   %LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude):
   - adds "nodach-desktop" (absolute python.exe path: Claude desktop doesn't see PATH)
   - removes "nodach-gate" and "nodach-pointer" (Nodach Desktop serves both) and raw Windows-MCP entries
   - every other server and setting is kept; backup first (claude_desktop_config.json.bak-<time>); UTF-8, no BOM
4. Puts a "Nodach Desktop" shortcut on the Windows desktop.
The functions above main() are pure and tested on Linux (tests/test_desktop.py).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "app" / "shell"))

SERVER_NAME = "nodach-desktop"
REPLACES = ("nodach-gate", "nodach-pointer")
MSIX_PKG = "Claude_pzs8sxrjxfjjc"
PACKAGES = ["mcp>=2.3,<3", "windows-mcp>=0.8.9,<0.9", "pywin32", "uiautomation", "pillow", "psutil", "pynput"]
VOICE_PACKAGES = ["faster-whisper>=1.1", "sounddevice>=0.5", "numpy>=1.26"]
EXTENSION_NOTE = "Open Claude > Settings > Extensions and turn off Nodach Pointer; Nodach Desktop now serves the pointer."


def config_paths(appdata: str, localappdata: str) -> list[Path]:
    """The normal config, plus the MSIX one when Claude is installed from the Microsoft Store (its package folder exists)."""
    out = [Path(appdata) / "Claude" / "claude_desktop_config.json"]
    pkg = Path(localappdata) / "Packages" / MSIX_PKG
    if pkg.is_dir():
        out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    return out


def python_exe(exe: str) -> str:
    """python.exe, not pythonw.exe: an MCP server talks over stdin/stdout."""
    if exe.lower().endswith("pythonw.exe"):
        return exe[: -len("pythonw.exe")] + "python.exe"
    return exe


def server_entry(python: str, shell_mcp: str) -> dict:
    return {"command": python, "args": [shell_mcp],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "ANONYMIZED_TELEMETRY": "false"}}


def is_raw_windows_mcp(name: str, entry) -> bool:
    if name == SERVER_NAME or not isinstance(entry, dict):
        return False
    blob = " ".join([name, str(entry.get("command", "")), " ".join(map(str, entry.get("args") or []))]).lower()
    return "windows-mcp" in blob or "windows_mcp" in blob or "windowsmcp" in blob


def merge_config(old_text: str | None, entry: dict) -> tuple[str, list[str]]:
    """Config text with mcpServers.nodach-desktop set and the entries it replaces removed. Every other key and server
    is kept. Returns (text, removed names). Raises ValueError if the old file isn't a JSON object (left untouched)."""
    data = {}
    if old_text and old_text.strip():
        data = json.loads(old_text.lstrip("﻿"))
        if not isinstance(data, dict):
            raise ValueError("claude_desktop_config.json isn't a JSON object")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    removed = [n for n, e in servers.items() if n in REPLACES or is_raw_windows_mcp(n, e)]
    for n in removed:
        servers.pop(n)
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", removed


def write_config(path: Path, entry: dict, now: dt.datetime | None = None) -> dict:
    """Backup first, then write UTF-8 without a BOM (Claude desktop can't read a BOM). Returns {path, backup, removed}."""
    path = Path(path)
    old, backup = None, None
    if path.exists():
        raw = path.read_bytes()
        old = raw.decode("utf-8-sig")
        merge_config(old, entry)               # refuse before touching anything if it isn't JSON
        stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + f".bak-{stamp}")
        backup.write_bytes(raw)
    text, removed = merge_config(old, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)
    return {"path": str(path), "backup": str(backup) if backup else None, "removed": removed}


def install_app(src_app: Path, home: Path) -> dict:
    import updater
    home = Path(home)
    for d in ("logs", "run"):
        (home / d).mkdir(parents=True, exist_ok=True)
    r = updater.install_from_folder(src_app, home / "app")
    if not (home / "desktop.json").exists():
        (home / "desktop.json").write_text(json.dumps({"update_url": "", "repo": "Morami49/nodach-studio",
                                                       "autostart_window": True}, indent=1) + "\n", encoding="utf-8")
    for f in ("install-desktop.bat", "install_desktop.py", "README.md"):
        if (HERE / f).exists() and (HERE / f).resolve() != (home / f).resolve():
            (home / f).write_bytes((HERE / f).read_bytes())
    updater.log(home, "install", **r)
    return r


def make_shortcut(home: Path, pythonw: str):
    """A 'Nodach Desktop' shortcut on the desktop (best effort)."""
    target = home / "app" / "shell" / "desktop_gui.py"
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\\Nodach Desktop.lnk');"
          f"$s.TargetPath='{pythonw}';$s.Arguments='\"{target}\"';$s.WorkingDirectory='{home}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], capture_output=True)


def main() -> int:
    print("Nodach Desktop installer\n")
    if sys.version_info < (3, 14):
        print(f"Windows-MCP needs Python 3.14 or newer; this is {sys.version.split()[0]}.")
        print("Install it from https://www.python.org/downloads/windows/ and run install-desktop.bat again.")
        return 1
    if os.name != "nt":
        print("This installer is for Windows.")
        return 1
    print("1/4 Installing packages for your user (this can take a few minutes)...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "--disable-pip-version-check", *PACKAGES])
    if r.returncode != 0:
        print("\npip couldn't install the packages (see above). Nothing else was changed.")
        return 1
    v = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--disable-pip-version-check", *VOICE_PACKAGES])
    if v.returncode != 0:
        print("   (Live's voice packages didn't install; Live still works with typing.)")
    home = Path(os.environ.get("NODACH_DESKTOP_HOME", str(Path.home() / "nodach")))
    print("2/4 Copying Nodach Desktop to", home / "app")
    res = install_app(HERE / "app", home)
    print(f"   version {res['to']}" + (f" (the earlier {res['from']} is kept for Roll back)" if res["from"] else ""))
    print("3/4 Adding nodach-desktop to Claude desktop")
    entry = server_entry(python_exe(sys.executable), str(home / "app" / "shell" / "shell_mcp.py"))
    for p in config_paths(os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
        try:
            w = write_config(p, entry)
        except ValueError as e:
            print(f"   {p}: not changed ({e}). Fix or delete it and run again.")
            continue
        print(f"   {p}: updated" + (f" (backup {Path(w['backup']).name})" if w["backup"] else " (new file)"))
        if w["removed"]:
            print("   took out: " + ", ".join(w["removed"]) + " (Nodach Desktop serves them; they're still in the backup)")
    print("4/4 Adding a 'Nodach Desktop' shortcut to your desktop")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    make_shortcut(home, str(pyw if pyw.exists() else exe))
    print("\nDone. Two things, once:")
    print("  a) " + EXTENSION_NOTE)
    print("  b) Quit Claude from the tray (right-click the Claude icon by the clock > Quit) and reopen it.")
    print("After that, new versions load without restarting Claude (the Update button, or Claude in the linked chat).")
    print("Settings > Developer in Claude should show nodach-desktop as running; the Nodach Desktop window opens with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
