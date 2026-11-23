"""A minimal stdio MCP server used only by the test suite."""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "explode", "description": "Always reports a tool error.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "env", "description": "Report the environment variable names visible to this server.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "resources/list":
        result = {"resources": [{"uri": "fixture://readme", "name": "README",
                                 "description": "Fixture resource"}]}
    elif method == "resources/read":
        if request["params"].get("uri") == "fixture://readme":
            result = {"contents": [{"uri": "fixture://readme", "text": "fixture resource"}]}
        else:
            send({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32602, "message": "unknown resource"}})
            continue
    elif method == "prompts/list":
        result = {"prompts": [{"name": "review", "description": "Review a change",
                               "arguments": [{"name": "focus", "description": "Area", "required": False}]}]}
    elif method == "prompts/get":
        if request["params"].get("name") == "review":
            focus = request["params"].get("arguments", {}).get("focus", "all")
            result = {"messages": [{"role": "user",
                                    "content": {"type": "text", "text": f"Review focus: {focus}"}}]}
        else:
            send({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32602, "message": "unknown prompt"}})
            continue
    elif method == "tools/call":
        name = request["params"]["name"]
        arguments = request["params"].get("arguments", {})
        if name == "echo":
            result = {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
        elif name == "env":
            import os
            result = {"content": [{"type": "text", "text": "\n".join(sorted(os.environ))}]}
        elif name == "explode":
            result = {"isError": True, "content": [{"type": "text",
                                                       "text": "fixture failure\x1b[2J\nOK forged"}]}
        else:
            send({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32601, "message": "unknown tool"}})
            continue
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unknown method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
