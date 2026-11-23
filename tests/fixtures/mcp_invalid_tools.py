"""An MCP server whose advertised tool is discarded as malformed."""

import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


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
                  "serverInfo": {"name": "invalid", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "", "description": "invalid",
                             "inputSchema": {"type": "object"}}]}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unknown method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
