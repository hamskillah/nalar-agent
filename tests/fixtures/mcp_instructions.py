"""An MCP server that advertises init-time guidance."""

import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


INSTRUCTIONS = "Prefer the fixture conventions.\nNever claim unverified results."

for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "guided", "version": "1"},
                  "instructions": INSTRUCTIONS}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo.",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unknown method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
