"""An MCP server with no tools/resources/prompts, used by diagnostics tests."""

import json
import sys

for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {},
                  "serverInfo": {"name": "empty", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": []}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                 "result": result}) + "\n")
    sys.stdout.flush()
