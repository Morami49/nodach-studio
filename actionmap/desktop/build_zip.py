"""
Build the Nodach Desktop bundle: nodach-desktop-<ver>.zip (one folder, nodach-desktop/) and its .sha256.

    nodach-desktop/
      install-desktop.bat, install_desktop.py, README.md
      app/VERSION
      app/shell/   shell_mcp.py worker.py desktop_gui.py desktop_core.py updater.py
      app/gate/    from actionmap/gate (no tests)
      app/pointer/ from actionmap/pointer (no tests)
      app/live/    from actionmap/live (no tests)

Used by the desktop-v* release workflow and by the tests:  python actionmap/desktop/build_zip.py --out dist
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTIONMAP = HERE.parent
MODULES = {"gate": ACTIONMAP / "gate", "pointer": ACTIONMAP / "pointer", "live": ACTIONMAP / "live", "shell": HERE / "shell"}
TOP_FILES = ["install-desktop.bat", "install_desktop.py", "README.md"]
# the old per-module installers would add nodach-gate / nodach-pointer back to Claude's config: not shipped
LEFT_OUT = {"install_gate.py", "install-gate.bat", "install-claude-config.ps1", "connect-to-claude.bat"}


def keep(p: Path) -> bool:
    n = p.name
    return not (n.startswith("test_") or n == "__pycache__" or n.endswith(".pyc") or n == "tests" or n in LEFT_OUT)


def version() -> str:
    return (HERE / "VERSION").read_text(encoding="utf-8").strip()


def assemble(dest: Path, ver: str | None = None) -> Path:
    """Write the nodach-desktop/ folder into dest. Returns its path."""
    root = Path(dest) / "nodach-desktop"
    if root.exists():
        shutil.rmtree(root)
    app = root / "app"
    for name, src in MODULES.items():
        out = app / name
        out.mkdir(parents=True)
        for f in sorted(src.iterdir()):
            if f.is_file() and keep(f):
                shutil.copy2(f, out / f.name)
    (app / "VERSION").write_text((ver or version()) + "\n", encoding="utf-8")
    for f in TOP_FILES:
        shutil.copy2(HERE / f, root / f)
    return root


def make_zip(folder: Path, zip_path: Path) -> tuple[Path, Path]:
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(Path(folder).parent).as_posix())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha = zip_path.with_name(zip_path.name + ".sha256")
    sha.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, sha


def build(out: Path, ver: str | None = None) -> tuple[Path, Path]:
    ver = ver or version()
    stage = Path(out) / "stage"
    folder = assemble(stage, ver)
    z, s = make_zip(folder, Path(out) / f"nodach-desktop-{ver}.zip")
    shutil.rmtree(stage)
    return z, s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--version", default=None, help="must match actionmap/desktop/VERSION (checked)")
    a = ap.parse_args(argv)
    if a.version and a.version != version():
        print(f"tag says {a.version} but actionmap/desktop/VERSION is {version()}", file=sys.stderr)
        return 1
    z, s = build(Path(a.out))
    print(z)
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
