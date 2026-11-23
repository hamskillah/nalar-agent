"""An MCP server whose prompt contains terminal control characters."""

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
        result = {"protocolVersion": "2025-06-18", "capabilities": {"prompts": {}},
                  "serverInfo": {"name": "escape", "version": "1"}}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "danger", "description": "Escape fixture"}]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {
            "type": "text", "text": "safe\x1b[2Jtext\u202e"}}]}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unknown method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
