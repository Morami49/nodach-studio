"""
Nodach Gate installer (run by install-gate.bat; one click).

1. Installs the Python packages for this user (pip --user): mcp, windows-mcp, pywin32, uiautomation, pillow, psutil.
2. Copies Gate to %USERPROFILE%\\nodach-gate\\app (a fixed place, so it keeps working if the zip is deleted) and puts
   zones.json in %USERPROFILE%\\nodach-gate (only if you don't have one yet: your edits are never overwritten).
3. Adds "nodach-gate" to Claude desktop's config, at %APPDATA%\\Claude\\claude_desktop_config.json and, if that
   folder exists, the Microsoft Store (MSIX) copy under %LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\...
   - the command is the absolute path of python.exe (Claude desktop doesn't see PATH)
   - every other server is kept; a raw Windows-MCP entry is taken out (it would let Claude round Gate) and the
     old file is kept as claude_desktop_config.json.bak-<timestamp>
   - written as UTF-8 without a BOM (Claude desktop can't read a BOM)
The functions above main() are pure and tested on any OS (test_gate.py).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_NAME = "nodach-gate"
MSIX_PKG = "Claude_pzs8sxrjxfjjc"
PACKAGES = ["mcp>=2.3,<3", "windows-mcp>=0.8.9,<0.9", "pywin32", "uiautomation", "pillow", "psutil"]
APP_FILES = ["gate_core.py", "gate_mcp.py", "gate_win.py", "gate_popup.py", "gate_gui.py", "gate_studio.py",
             "zones.json", "start-gate.bat", "README.md", "requirements.txt", "install_gate.py", "install-gate.bat"]


def config_paths(appdata: str, localappdata: str) -> list[Path]:
    """Where Claude desktop keeps its config: the normal install, and the MSIX one if that folder exists."""
    out = [Path(appdata) / "Claude" / "claude_desktop_config.json"]
    msix = Path(localappdata) / "Packages" / MSIX_PKG / "LocalCache" / "Roaming" / "Claude"
    if msix.is_dir():
        out.append(msix / "claude_desktop_config.json")
    return out


def python_exe(exe: str) -> str:
    """python.exe, not pythonw.exe (an MCP server talks over stdin/stdout)."""
    if exe.lower().endswith("pythonw.exe"):
        return exe[: -len("pythonw.exe")] + "python.exe"
    return exe


def server_entry(python: str, gate_mcp: str) -> dict:
    return {"command": python, "args": [gate_mcp],
            "env": {"PYTHONUTF8": "1", "ANONYMIZED_TELEMETRY": "false", "PYTHONIOENCODING": "utf-8"}}


def is_raw_windows_mcp(name: str, entry) -> bool:
    if name == SERVER_NAME or not isinstance(entry, dict):
        return False
    blob = " ".join([name, str(entry.get("command", "")), " ".join(map(str, entry.get("args") or []))]).lower()
    return "windows-mcp" in blob or "windows_mcp" in blob or "windowsmcp" in blob


def merge_config(old_text: str | None, entry: dict) -> tuple[str, list[str]]:
    """New config text with mcpServers.nodach-gate set; every other key and server kept, raw Windows-MCP entries
    removed. Returns (text, removed server names). Raises ValueError if the old file isn't JSON (left untouched)."""
    data = {}
    if old_text and old_text.strip():
        data = json.loads(old_text.lstrip("﻿"))
        if not isinstance(data, dict):
            raise ValueError("claude_desktop_config.json isn't a JSON object")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    removed = [n for n, e in servers.items() if is_raw_windows_mcp(n, e)]
    for n in removed:
        servers.pop(n)
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", removed


def write_config(path: Path, entry: dict, now: dt.datetime | None = None) -> dict:
    """Merge into one config file: backup first, UTF-8 without BOM. Returns {path, backup, removed}."""
    path = Path(path)
    old = None
    backup = None
    if path.exists():
        raw = path.read_bytes()
        old = raw.decode("utf-8-sig")
        stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + f".bak-{stamp}")
        backup.write_bytes(raw)
    text, removed = merge_config(old, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))         # no BOM
    os.replace(tmp, path)
    return {"path": str(path), "backup": str(backup) if backup else None, "removed": removed}


def copy_app(src: Path, home: Path) -> Path:
    app = home / "app"
    app.mkdir(parents=True, exist_ok=True)
    for f in APP_FILES:
        if (src / f).exists() and (src / f).resolve() != (app / f).resolve():
            shutil.copy2(src / f, app / f)
    if not (home / "zones.json").exists():
        shutil.copy2(src / "zones.json", home / "zones.json")
    return app


def main() -> int:
    print("Nodach Gate installer\n")
    if sys.version_info < (3, 14):
        print(f"Windows-MCP needs Python 3.14 or newer; this is {sys.version.split()[0]}.")
        print("Install it from https://www.python.org/downloads/windows/ and run install-gate.bat again.")
        return 1
    if os.name != "nt":
        print("This installer is for Windows.")
        return 1
    print("1/3 Installing packages for your user (this can take a few minutes)...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "--disable-pip-version-check", *PACKAGES])
    if r.returncode != 0:
        print("\npip couldn't install the packages (see above). Nothing else was changed.")
        return 1
    home = Path(os.environ.get("NODACH_GATE_HOME", str(Path.home() / "nodach-gate")))
    print("2/3 Copying Gate to", home / "app")
    app = copy_app(HERE, home)
    print("3/3 Adding nodach-gate to Claude desktop")
    entry = server_entry(python_exe(sys.executable), str(app / "gate_mcp.py"))
    for p in config_paths(os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
        try:
            res = write_config(p, entry)
        except ValueError as e:
            print(f"   {p}: not changed ({e}). Fix or delete it and run again.")
            continue
        print(f"   {p}: updated" + (f" (backup {Path(res['backup']).name})" if res["backup"] else " (new file)"))
        if res["removed"]:
            print("   took out raw Windows-MCP entr" + ("y" if len(res["removed"]) == 1 else "ies") + ": " + ", ".join(res["removed"])
                  + " (they let Claude skip Gate; they're still in the backup)")
    print("\nDone. Quit Claude from the tray (right-click the Claude icon by the clock > Quit) and reopen it.")
    print("Then in Claude: Settings > Developer should show nodach-gate as running. The Gate window opens with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
