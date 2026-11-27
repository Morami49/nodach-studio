"""A stand-in worker for the shell tests: raw MCP over stdio, stdlib only.
Tools: echo (returns this process's pid and the app VERSION), slow (sleeps 1.5 s), crash (exits at once)."""
import json
import os
import sys
import threading
import time
from pathlib import Path

APP = Path(os.environ["NODACH_DESKTOP_APP"])
out_lock = threading.Lock()


def send(m):
    with out_lock:
        sys.stdout.write(json.dumps(m) + "\n")
        sys.stdout.flush()


def handle(m):
    mid, method, p = m.get("id"), m.get("method"), m.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion"), "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "fake", "version": "0"}, "instructions": "fake worker"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in ("echo", "slow", "crash")]}})
    elif method == "tools/call":
        name = p.get("name")
        if name == "crash":
            os._exit(3)
        if name == "slow":
            time.sleep(1.5)
        ver = (APP / "VERSION").read_text().strip()
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({"pid": os.getpid(), "version": ver, "tool": name})}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}})


if os.environ.get("FAKE_WORKER_DIE_AT_START") and Path(os.environ["FAKE_WORKER_DIE_AT_START"]).exists():
    sys.exit(5)
for line in sys.stdin:
    msg = json.loads(line)
    threading.Thread(target=handle, args=(msg,), daemon=True).start()
