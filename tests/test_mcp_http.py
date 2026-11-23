import json
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from nalar.config import ConfigError, load_config
from nalar.mcp import MCPError, MCPHttp, make_client, validate_url

TOOLS = [
    {"name": "echo", "description": "Echo text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "explode", "description": "Fail.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


class HttpMCPTests(unittest.TestCase):
    def serve(self, *, sse=False, status=200):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length))
                seen.append((dict(self.headers), message))
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if status != 200:
                    self.send_response(status)
                    self.end_headers()
                    return
                request_id = message["id"]
                method = message.get("method")
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "remote", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    name = message["params"].get("name")
                    if name == "echo":
                        result = {"content": [{"type": "text",
                                               "text": message["params"]["arguments"]["text"]}]}
                    elif name == "explode":
                        result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
                    else:
                        self._error(request_id, -32601)
                        return
                elif method == "prompts/list":
                    result = {"prompts": [{"name": "review", "description": "Review"}]}
                elif method == "resources/list":
                    result = {"resources": [{"uri": "mem://readme"}]}
                else:
                    self._error(request_id, -32601)
                    return
                payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
                self.send_response(200)
                self.send_header("Mcp-Session-Id", "sess-1")
                if sse:
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload.encode())

            def _error(self, request_id, code):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                             "error": {"code": code, "message": "no"}}).encode())

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_port, seen

    def test_initializes_lists_and_calls_over_http(self):
        port, seen = self.serve()
        client = MCPHttp("remote", f"http://127.0.0.1:{port}/mcp",
                         {"Authorization": "Bearer test"})
        self.addCleanup(client.close)
        client.initialize()
        names = [tool["function"]["name"] for tool in client.tools()]
        self.assertEqual(names, ["remote__echo", "remote__explode"])
        self.assertEqual(client.call("remote__echo", {"text": "hi"}), "hi")
        self.assertEqual(client.prompts()[0]["name"], "review")
        self.assertEqual(client.resources()[0]["uri"], "mem://readme")
        with self.assertRaises(MCPError) as caught:
            client.call("remote__explode", {})
        self.assertIn("boom", str(caught.exception))
        later = [headers for headers, message in seen
                 if message.get("method") == "tools/list"][0]
        self.assertEqual(later.get("Mcp-Session-Id"), "sess-1")
        self.assertEqual(later.get("Authorization"), "Bearer test")
        self.assertEqual(later.get("MCP-Protocol-Version"), "2025-06-18")

    def test_sse_responses_are_parsed(self):
        port, _ = self.serve(sse=True)
        client = MCPHttp("remote", f"http://127.0.0.1:{port}/mcp")
        self.addCleanup(client.close)
        client.initialize()
        self.assertEqual(len(client.tools()), 2)

    def test_multiline_sse_data_is_joined(self):

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                method = message.get("method")
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "split", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
                middle = payload.index(",") + 1
                body = f"event: message\ndata: {payload[:middle]}\ndata: {payload[middle:]}\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body.encode())
                self.wfile.flush()

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = MCPHttp("remote", f"http://127.0.0.1:{server.server_port}/mcp")
        self.addCleanup(client.close)
        client.initialize()
        self.assertEqual(len(client.tools()), 2)

    def test_sse_returns_without_waiting_for_eof(self):
        import time as _time

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                payload = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                      "result": {"protocolVersion": "2025-06-18",
                                                 "capabilities": {}, "serverInfo": {}}})
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    _time.sleep(1.0)
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = MCPHttp("remote", f"http://127.0.0.1:{server.server_port}/mcp")
        self.addCleanup(client.close)
        start = _time.monotonic()
        client.initialize()
        self.assertLess(_time.monotonic() - start, 0.8)

    def test_absolute_timeout_bounds_a_stalled_server(self):
        import time as _time

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                _time.sleep(2)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = MCPHttp("remote", f"http://127.0.0.1:{server.server_port}/mcp", timeout=0.4)
        self.addCleanup(client.close)
        start = _time.monotonic()
        with self.assertRaises(MCPError) as caught:
            client.initialize()
        self.assertEqual(str(caught.exception), "server_timeout")
        self.assertLess(_time.monotonic() - start, 1.5)

    def test_http_errors_do_not_leak_body(self):
        port, _ = self.serve(status=500)
        client = MCPHttp("remote", f"http://127.0.0.1:{port}/mcp")
        self.addCleanup(client.close)
        with self.assertRaises(MCPError) as caught:
            client.initialize()
        self.assertEqual(str(caught.exception), "http_500")

    def test_urls_require_tls_except_loopback(self):
        for url in ("http://example.com/mcp", "https://user:pass@example.com/mcp",
                    "ftp://example.com/mcp", "https://example.com/mcp#frag"):
            with self.subTest(url=url), self.assertRaises(MCPError):
                validate_url(url)
        validate_url("https://example.com/mcp")
        validate_url("http://127.0.0.1:9000/mcp")

    def test_make_client_selects_http_for_url_servers(self):
        class Server:
            name = "remote"
            command = ()
            env = {}
            url = "https://example.com/mcp"
            headers = {"Authorization": "Bearer x"}

        client = make_client(Server())
        self.assertIsInstance(client, MCPHttp)
        client.close()


class HttpMCPConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_url_servers_and_headers_are_validated(self):
        self.write({"mcp": {"remote": {"url": "https://example.com/mcp",
                                       "headers": {"Authorization": "Bearer {env:REMOTE_TOKEN}"}}}})
        with patch.dict(__import__("os").environ, {"REMOTE_TOKEN": "secret"}):
            config = load_config(self.root, self.root / "none.json", trust_project=True)
        server = config.mcp[0]
        self.assertEqual((server.url, server.headers["Authorization"]),
                         ("https://example.com/mcp", "Bearer secret"))
        self.assertEqual(server.command, ())

    def test_invalid_combinations_are_rejected(self):
        cases = [
            {"mcp": {"x": {"url": "https://example.com/mcp", "command": ["x"]}}},
            {"mcp": {"x": {}}},
            {"mcp": {"x": {"url": "http://example.com/mcp"}}},
            {"mcp": {"x": {"url": "https://example.com/mcp", "headers": {"Host": "x"}}}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    load_config(self.root, self.root / "none.json", trust_project=True)

    def test_a_notification_response_without_a_body_does_not_time_out(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length))
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": message["id"],
                    "result": {"protocolVersion": "2025-06-18",
                               "capabilities": {}, "serverInfo": {
                                   "name": "keepalive", "version": "1"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = MCPHttp("keepalive", f"http://127.0.0.1:{server.server_port}/",
                         timeout=0.5)
        started = time.monotonic()
        client.initialize()
        self.assertLess(time.monotonic() - started, 2)

    def test_deep_json_responses_are_protocol_errors(self):
        deep = "[" * 20_000 + "]" * 20_000
        for sse in (False, True):
            with self.subTest(sse=sse):
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", "0"))
                        self.rfile.read(length)
                        if sse:
                            body = ("data: " + deep + "\n\n").encode()
                            content_type = "text/event-stream"
                        else:
                            body = deep.encode()
                            content_type = "application/json"
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = True
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                client = MCPHttp("deep", f"http://127.0.0.1:{server.server_port}/",
                                 timeout=2)
                with self.assertRaises(MCPError):
                    client.initialize()

    def test_non_latin1_headers_and_control_urls_are_rejected(self):
        from nalar.mcp import MCPError, MCPHttp, validate_url

        with self.assertRaises(MCPError):
            MCPHttp("docs", "http://127.0.0.1:9/mcp",
                    headers={"X-Org": "price \u20ac"})
        with self.assertRaises(MCPError):
            validate_url("http://127.0.0.1:9/mcp\nX: 1")

    def test_invalid_ports_and_control_headers_are_rejected(self):
        from nalar.mcp import MCPError, MCPHttp, validate_url

        for url in ("http://127.0.0.1:99999/mcp", "http://[zz]/mcp",
                    "http://127.0.0.1:9/mcp\u0085"):
            with self.subTest(url=url), self.assertRaises(MCPError):
                validate_url(url)
        with self.assertRaises(MCPError):
            MCPHttp("docs", "http://127.0.0.1:9/mcp",
                    headers={"X": "a\r\nb"})
