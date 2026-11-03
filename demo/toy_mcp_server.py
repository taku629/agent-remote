"""Tiny stdio MCP server used to test the proxy (no SDK needed)."""
import json, sys
TOOLS = [{"name": "run_shell", "description": "Run a shell command",
          "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "toy", "version": "0"}}
    elif m["method"] == "tools/list":
        res = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        res = {"content": [{"type": "text", "text": f"(pretend) ran: {m['params']['arguments']['command']}"}]}
    else:
        res = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
