"""A stdio MCP server that floods client requests without reading replies."""

import json
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    if request.get("method") != "initialize":
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "spam", "version": "1"}}})
    for index in range(200_000):
        send({"jsonrpc": "2.0", "id": 1_000_000 + index, "method": "ping"})
        if index % 500 == 0:
            time.sleep(0.001)
    break
