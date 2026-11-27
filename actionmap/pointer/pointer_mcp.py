"""
Nodach Pointer MCP server — v0.1.3 (works with mcp 1.x and 2.x)
Gives Claude Desktop / Claude Code read access to what you pointed at with pointer.py.

Tools
  get_pointed_elements(limit, status)  latest pointer events (default: status "new")
  get_pointer_screenshot(event_id)     the cropped screenshot for one event
  mark_pointed(event_ids, status)      new | seen | done | deferred

Status changes go to a separate append-only file (status.jsonl); events.jsonl is never
rewritten, so every earlier state stays visible (versioned, no silent overwrites).
"""
import datetime as dt
import json
import os
from pathlib import Path

try:  # mcp 2.x (the version ActionMap uses)
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

HOME = Path(os.environ.get("NODACH_POINTER_HOME", str(Path.home() / "nodach-pointer")))
EVENTS = HOME / "events.jsonl"
STATUS = HOME / "status.jsonl"
STATUSES = {"new", "seen", "done", "deferred"}

mcp = FastMCP("nodach-pointer")


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a half-written line rather than fail
    return out


def current_status() -> dict:
    s = {}
    for row in read_jsonl(STATUS):
        s[row.get("id")] = row.get("status")
    return s


@mcp.tool()
def get_pointed_elements(limit: int = 5, status: str = "new") -> str:
    """Latest elements the user pointed at on their Windows desktop (newest first).
    status: new | seen | done | deferred | any. Each event has the app, window, element
    type/name/AutomationId, ancestor path, screen rectangle, the user's note, batch refs
    and a screenshot path (fetch it with get_pointer_screenshot)."""
    st = current_status()
    events = [e for e in read_jsonl(EVENTS) if e.get("mode") != "batch-item" or status == "any"]
    for e in events:
        e["status"] = st.get(e["id"], "new")
    if status != "any":
        events = [e for e in events if e["status"] == status]
    by_id = {e["id"]: e for e in read_jsonl(EVENTS)}
    picked = list(reversed(events))[: max(1, min(limit, 50))]
    for e in picked:  # inline batched items so one point carries the whole selection
        e["batch_items"] = [by_id[r] for r in e.get("batch_refs", []) if r in by_id]
    return json.dumps(picked, ensure_ascii=False, indent=1)


@mcp.tool()
def get_pointer_screenshot(event_id: str) -> Image:
    """The cropped screenshot saved for one pointer event."""
    for e in read_jsonl(EVENTS):
        if e.get("id") == event_id and e.get("screenshot") and Path(e["screenshot"]).exists():
            return Image(path=e["screenshot"])
    raise ValueError(f"No screenshot for {event_id}")


@mcp.tool()
def mark_pointed(event_ids: list[str], status: str) -> str:
    """Record a status for pointer events: new | seen | done | deferred (append-only)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    HOME.mkdir(parents=True, exist_ok=True)
    at = dt.datetime.now(dt.timezone.utc).isoformat()
    with STATUS.open("a", encoding="utf-8") as f:
        for i in event_ids:
            f.write(json.dumps({"id": i, "status": status, "at": at}) + "\n")
    return f"Marked {len(event_ids)} event(s) {status}"


if __name__ == "__main__":
    mcp.run()
