"""A stand-in for Windows-MCP in tests: same tool names (UI ones and the ones Gate must hide), no Windows."""
import asyncio
import json

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

NAMES = ["App", "ControlStatus", "Clipboard", "Click", "Type", "Scroll", "Move", "Shortcut", "Wait", "WaitFor",
         "PowerShell", "FileSystem", "Registry", "Process", "Snapshot", "Screenshot", "Scrape", "MultiEdit", "Notification", "BrandNewTool"]
SNAP = '''Focused Window:
Notepad - Untitled
Opened Windows:
Barclays Online Banking - Google Chrome
UI Tree:
desktop
├── window "Untitled - Notepad"
│   └── (100,200) edit "Text editor"  [action: type]
└── window "Barclays Online Banking - Google Chrome"
    └── (500,600) edit "Card number"  [action: type]'''


async def list_tools(ctx, params):
    schema = {"type": "object", "properties": {"loc": {"type": "array"}, "label": {"type": "integer"}, "text": {"type": "string"}}}
    return types.ListToolsResult(tools=[types.Tool(name=n, description=n, input_schema=schema) for n in NAMES])


async def call_tool(ctx, params):
    if params.name == "Snapshot":
        return types.CallToolResult(content=[types.TextContent(type="text", text=SNAP)])
    if (params.arguments or {}).get("fail"):
        return types.CallToolResult(content=[types.TextContent(type="text", text="boom")], is_error=True)
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"did {params.name} {json.dumps(params.arguments or {})}")])


async def main():
    server = Server("fake-wmcp", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
