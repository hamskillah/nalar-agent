"""A resources-only MCP fixture with paginated listings and no tools.

Used to prove that servers without tools still contribute resources and
prompts, and that listing follows `nextCursor`.
"""

import json
import sys

REJECT_TOOLS = sys.argv[1:] == ["reject-tools"]

PAGES = {
    "resources/list": [
        ({"resources": [{"uri": "file:///alpha.txt", "name": "alpha",
                         "description": "first page"}], "nextCursor": "two"}),
        ({"resources": [{"uri": "file:///beta.txt", "name": "beta"}]}),
    ],
    "resources/templates/list": [
        ({"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "dynamic",
                                 "description": "Any file URI"},
                                {"uriTemplate": "mem://{+path}", "name": "memory",
                                 "description": "Any memory path"}]}),
    ],
    "prompts/list": [
        ({"prompts": [{"name": "brief", "description": "first page",
                       "arguments": [{"name": "topic", "required": True}]}],
          "nextCursor": "two"}),
        ({"prompts": [{"name": "review", "description": "second page"}]}),
    ],
}

CONTENT = {"file:///alpha.txt": "alpha text", "file:///beta.txt": "beta text"}


def main():
    seen = {}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if "id" not in request:
            continue
        method = request.get("method")
        params = request.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "serverInfo": {"name": "resources", "version": "1"}}
        elif method in PAGES:
            pages = PAGES[method]
            cursor = params.get("cursor")
            index = 0 if not cursor else 1
            seen[method] = index
            result = pages[min(index, len(pages) - 1)]
        elif method == "resources/read":
            uri = params.get("uri")
            if uri in CONTENT:
                result = {"contents": [{"uri": uri, "text": CONTENT[uri]}]}
            elif isinstance(uri, str) and uri.startswith(("file:///", "mem://")):
                result = {"contents": [{"uri": uri, "text": f"dynamic {uri}"}]}
            else:
                result = {"contents": []}
        elif method == "prompts/get":
            name = params.get("name")
            topic = (params.get("arguments") or {}).get("topic", "")
            result = {"messages": [{"role": "user", "content": {
                "type": "text", "text": f"{name}:{topic}"}}]}
        elif method == "tools/list":
            if REJECT_TOOLS:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                             "error": {"code": -32601,
                                                       "message": "Method not found"}}) + "\n")
                sys.stdout.flush()
                continue
            result = {"tools": []}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                     "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
