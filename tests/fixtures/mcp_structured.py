"""A stdio MCP server that sends client-directed requests before replying."""

import json
import sys

TOOL = {
    "name": "echo",
    "description": "Echo text back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def wait_for(identifier):
    while True:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(1)
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == identifier:
            return message


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
                  "serverInfo": {"name": "structured", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": 900, "method": "ping"})
        if wait_for(900).get("result") != {}:
            raise SystemExit(2)
        send({"jsonrpc": "2.0", "id": 901, "method": "roots/list"})
        roots = wait_for(901).get("result", {}).get("roots")
        if not isinstance(roots, list):
            raise SystemExit(3)
        send({"jsonrpc": "2.0", "id": 902, "method": "sampling/createMessage",
              "params": {}})
        reply = wait_for(902)
        if not isinstance(reply.get("error"), dict) or reply["error"].get("code") != -32601:
            raise SystemExit(4)
        text = str(request.get("params", {}).get("arguments", {}).get("text", ""))
        structured = {"text": text, "length": len(text)}
        if roots:
            structured["roots"] = roots
        result = {"content": [], "structuredContent": structured}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unknown method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
