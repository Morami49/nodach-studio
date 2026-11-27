"""
Nodach Desktop worker: the MCP server the shell (shell_mcp.py) runs as its child. This is the part that changes;
the shell restarts it when files under app\\ change, so Claude never needs a restart for a new Gate or Pointer.

Serves, in one tool list:
- Gate's tools (gate_mcp.Gate: the same zones, popup, logging and reports as nodach-gate; data stays in
  %USERPROFILE%\\nodach-gate). Gate's own log window is not auto-opened: the Desktop window is the entry point.
- The Pointer's three tools (get_pointed_elements, get_pointer_screenshot, mark_pointed), from pointer_mcp.py
  (data stays in %USERPROFILE%\\nodach-pointer).
If Windows-MCP can't start, the Pointer tools and Gate's own two tools still work and the error is in
%USERPROFILE%\\nodach\\run\\worker-status.json (desktop_status shows it).
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import sys
import time
import traceback
from pathlib import Path

WORKER_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("NODACH_DESKTOP_APP") or HERE.parent)
ROOT = Path(os.environ.get("NODACH_DESKTOP_HOME") or (Path.home() / "nodach"))
for sub in ("gate", "pointer"):
    sys.path.insert(0, str(APP / sub))

POINTER_TOOL_DEFS = [
    {"name": "get_pointed_elements", "description": "Nodach Pointer: latest elements Nowed pointed at on his Windows desktop "
     "(newest first). status: new | seen | done | deferred | any. Each event has the app, window, element type/name/"
     "AutomationId, ancestor path, screen rectangle, his note, batch refs and a screenshot path (fetch it with "
     "get_pointer_screenshot).",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
         "status": {"type": "string", "default": "new", "enum": ["new", "seen", "done", "deferred", "any"]}}}},
    {"name": "get_pointer_screenshot", "description": "Nodach Pointer: the cropped screenshot saved for one pointer event.",
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}},
    {"name": "mark_pointed", "description": "Nodach Pointer: record a status for pointer events: new | seen | done | deferred "
     "(append-only, earlier states stay visible).",
     "inputSchema": {"type": "object", "properties": {
         "event_ids": {"type": "array", "items": {"type": "string"}},
         "status": {"type": "string", "enum": ["new", "seen", "done", "deferred"]}}, "required": ["event_ids", "status"]}},
]
POINTER_NAMES = {t["name"] for t in POINTER_TOOL_DEFS}
INSTRUCTIONS_POINTER = ("Nodach Pointer: when Nowed says he pointed at something (Ctrl+Alt+M on his PC), call "
                        "get_pointed_elements, then mark_pointed with seen/done once handled.")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Worker:
    def __init__(self):
        self.gate = None
        self.gate_error = None
        self.pointer = None
        self.pointer_error = None
        self.started = now_iso()
        try:
            import gate_mcp
            self.gate_mod = gate_mcp
            gate_mcp.ensure_home()
            self.gate = gate_mcp.Gate(autostart_gui=False)
        except Exception as e:
            self.gate_mod, self.gate_error = None, f"Gate didn't load: {type(e).__name__}: {e}"
        try:
            import pointer_mcp
            self.pointer = pointer_mcp
        except Exception as e:
            self.pointer_error = f"Pointer tools didn't load: {type(e).__name__}: {e}"
        self.gate_connected = False

    async def connect(self, stack):
        if not self.gate:
            return
        try:
            async with asyncio.timeout(float(os.environ.get("NODACH_DESKTOP_WMCP_TIMEOUT", "30"))):
                await self.gate.connect(stack)
            self.gate_connected = True
        except BaseException as e:   # noqa: BLE001 - a missing Windows-MCP must not take the Pointer down
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self.gate_error = f"Windows-MCP didn't start through Gate: {type(e).__name__}: {e}"
        with contextlib.suppress(Exception):
            self.gate.start_threads()

    def tool_defs(self):
        gate_defs = self.gate.tool_defs() if self.gate else []
        return gate_defs + (POINTER_TOOL_DEFS if self.pointer else [])

    def versions(self):
        out = {"worker": WORKER_VERSION}
        with contextlib.suppress(Exception):
            import gate_core
            out["gate"] = gate_core.VERSION
        with contextlib.suppress(Exception):
            import pointer_core
            out["pointer"] = pointer_core.VERSION
        return out

    def write_status(self):
        body = {"pid": os.getpid(), "started": self.started, "versions": self.versions(),
                "gate": {"loaded": bool(self.gate), "windows_mcp_connected": self.gate_connected, "error": self.gate_error,
                         "hidden_tools": getattr(self.gate, "hidden", None)},
                "pointer": {"loaded": bool(self.pointer), "error": self.pointer_error},
                "tools": [t["name"] for t in self.tool_defs()]}
        p = ROOT / "run" / "worker-status.json"
        with contextlib.suppress(Exception):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.with_suffix(".tmp").write_text(json.dumps(body, indent=1), encoding="utf-8")
            os.replace(p.with_suffix(".tmp"), p)

    async def call(self, name: str, args: dict) -> dict:
        if name in POINTER_NAMES:
            if not self.pointer:
                return _err(self.pointer_error or "Pointer tools aren't available.")
            try:
                return await asyncio.to_thread(self._pointer_call, name, args)
            except Exception as e:
                return _err(f"Nodach Pointer: {e}")
        if not self.gate:
            return _err(self.gate_error or "Gate isn't available.")
        if not self.gate_connected and name not in ("gate_status", "capture_view"):
            return _err(f"Nodach Gate can't reach Windows-MCP: {self.gate_error}. Ask Nowed to check Nodach Desktop's window.")
        if name == "gate_status":
            return await self.gate.call(name, args)
        self.acting(name, True)
        try:
            return await self.gate.call(name, args)
        finally:
            self.acting(name, False)

    def acting(self, name: str, on: bool):
        """run\\acting.json: what the Desktop window shows as 'Claude is acting' (Windows-MCP's own overlay is off since
        Gate 0.1.5, so this is the on-screen sign that Claude is driving the PC)."""
        self.active = getattr(self, "active", 0) + (1 if on else -1)
        body = {"active": self.active > 0, "tool": name, "at": now_iso(), "t": time.time(), "pid": os.getpid()}
        p = ROOT / "run" / "acting.json"
        with contextlib.suppress(Exception):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.with_suffix(".tmp").write_text(json.dumps(body), encoding="utf-8")
            os.replace(p.with_suffix(".tmp"), p)

    def _pointer_call(self, name, args):
        p = self.pointer
        if name == "get_pointed_elements":
            text = p.get_pointed_elements(int(args.get("limit", 5)), str(args.get("status", "new")))
            return {"content": [{"type": "text", "text": text}], "is_error": False}
        if name == "mark_pointed":
            text = p.mark_pointed(list(args.get("event_ids") or []), str(args.get("status") or ""))
            return {"content": [{"type": "text", "text": text}], "is_error": False}
        img = p.get_pointer_screenshot(str(args.get("event_id") or ""))
        return {"content": [img.to_image_content()], "is_error": False}

    def close(self):
        with contextlib.suppress(Exception):
            if getattr(self, "active", 0) > 0:
                self.active = 1
                self.acting("", False)
        if self.gate:
            with contextlib.suppress(Exception):
                self.gate.close()


def _err(text):
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_server(w: Worker):
    import mcp.types as types
    from mcp.server.lowlevel import Server

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(d) for d in w.tool_defs()])

    async def call_tool(ctx, params):
        try:
            r = await w.call(params.name, params.arguments or {})
        except Exception as e:
            traceback.print_exc()
            r = _err(f"Nodach Desktop worker error in {params.name}: {type(e).__name__}: {e}")
        content = [types.TextContent(type="text", text=c["text"]) if isinstance(c, dict) else c for c in r["content"]]
        return types.CallToolResult(content=content, is_error=r["is_error"])

    instructions = "\n\n".join(x for x in ((w.gate_mod.INSTRUCTIONS if w.gate_mod else ""), INSTRUCTIONS_POINTER if w.pointer else "") if x)
    return Server("nodach-desktop-worker", version=WORKER_VERSION, instructions=instructions,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve():
    from mcp.server.stdio import stdio_server
    w = Worker()
    async with contextlib.AsyncExitStack() as stack:
        await w.connect(stack)
        w.write_status()
        server = build_server(w)
        try:
            async with stdio_server() as (r, wr):
                await server.run(r, wr, server.create_initialization_options())
        finally:
            w.close()


if __name__ == "__main__":
    asyncio.run(serve())
