"""Provider matrix: the same scenarios across local/recorded provider profiles.

The paid/cross-provider matrix needs owner approval for credentials and spend.
This suite exercises the provider boundary with a local HTTP stub and the
recorded profiles in ``tests/fixtures/providers/matrix.json`` instead.
"""

import http.client
import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlparse

from nalar.agent import AgentLoop
from nalar.core import ProviderError
from nalar.go import FallbackChat, ChatCompletions, list_models
from nalar.tools import ToolBelt

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "providers" / "matrix.json"


def sse(*chunks):
    return (b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n"
                     for chunk in chunks) + b"data: [DONE]\n\n")


class Stub:
    """A small OpenAI-compatible endpoint with recorded responses."""

    def __init__(self, responses, models=("vendor/model", "vendor/backup")):
        self.responses = list(responses)
        self.requests = []
        self.models = list(models)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                outer.requests.append({
                    "path": self.path,
                    "body": json.loads(raw),
                    "headers": dict(self.headers),
                })
                status, payload, headers = outer.responses.pop(0)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "text/event-stream"
                                 if isinstance(payload, bytes) else "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                data = json.dumps({"data": [{"id": name} for name in outer.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port

    def start(self, testcase):
        testcase.enterContext(self.server)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        testcase.addCleanup(thread.join)
        testcase.addCleanup(self.server.shutdown)
        testcase.enterContext(patch(
            "nalar.go.HTTPSConnection",
            side_effect=lambda *args, **kwargs: http.client.HTTPConnection(
                "127.0.0.1", self.port, **kwargs)))
        return self

    def url(self, profile):
        return profile["base_url"].replace("{port}", str(self.port))


class ProviderMatrixTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())
        self.profiles = self.fixture["profiles"]
        self.responses = self.fixture["responses"]

    def client(self, profile, stub, model=None):
        return ChatCompletions(
            model or profile["model"], "fixture-key",
            base_url=stub.url(profile),
            extra_headers=profile["headers"],
            session=profile["session"])

    def test_text_matrix_across_profiles(self):
        stub = Stub([(200, self.responses["text"], {})] * len(self.profiles)).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                reply = self.client(profile, stub)(
                    [{"role": "user", "content": "hi"}], [])
                self.assertEqual(reply.text, "OK")
                request = stub.requests[-1]
                self.assertEqual(request["path"], urlparse(stub.url(profile)).path)
                self.assertEqual(request["body"]["model"], profile["model"])
                self.assertEqual(request["headers"]["Authorization"],
                                 "Bearer fixture-key")
                for name, value in profile["headers"].items():
                    self.assertEqual(request["headers"][name], value)
                self.assertEqual("x-nalar-session" in request["headers"],
                                 profile["session"])

    def test_tool_matrix_across_profiles(self):
        stub = Stub([(200, self.responses["tool"], {})] * len(self.profiles)).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                reply = self.client(profile, stub)(
                    [{"role": "user", "content": "list"}], [])
                self.assertEqual([call.name for call in reply.calls], ["list_files"])
                self.assertEqual(reply.calls[0].arguments, {"path": "."})

    def test_stream_matrix_across_profiles(self):
        body = sse(
            {"choices": [{"delta": {"content": "O"}}]},
            {"choices": [{"delta": {"content": "K"}}], "usage": {"total_tokens": 9}},
        )
        stub = Stub([(200, body, {})] * len(self.profiles)).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                pieces = []
                reply = self.client(profile, stub).stream(
                    [{"role": "user", "content": "hi"}], [], on_text=pieces.append)
                self.assertEqual(("".join(pieces), reply.text), ("OK", "OK"))
                self.assertEqual(reply.tokens, 9)
                self.assertTrue(stub.requests[-1]["body"]["stream"])
                self.assertEqual(stub.requests[-1]["body"]["stream_options"],
                                 {"include_usage": True})

    def test_models_matrix_across_profiles(self):
        stub = Stub([]).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                models = list_models(stub.url(profile), "fixture-key",
                                     profile["headers"])
                self.assertEqual(models, ["vendor/backup", "vendor/model"])
                request = stub.requests[-1]
                self.assertEqual(request["path"], profile["models_path"])
                self.assertEqual(request["headers"]["Authorization"],
                                 "Bearer fixture-key")
                for name, value in profile["headers"].items():
                    self.assertEqual(request["headers"][name], value)

    def test_fallback_on_missing_model_matrix(self):
        stub = Stub([(404, {"error": "missing"}, {}),
                     (200, self.responses["text"], {})]
                    * len(self.profiles)).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                start = len(stub.requests)
                chat = FallbackChat(
                    ["vendor/missing", profile["model"]],
                    lambda model, profile=profile, stub=stub: self.client(
                        profile, stub, model=model))
                self.assertEqual(chat([{"role": "user", "content": "hi"}], []).text, "OK")
                self.assertEqual([item["body"]["model"]
                                  for item in stub.requests[start:]],
                                 ["vendor/missing", profile["model"]])

    def test_retry_after_on_429_and_500(self):
        stub = Stub([(429, {"error": "busy"}, {"Retry-After": "0"}),
                     (500, {"error": "down"}, {}),
                     (200, self.responses["text"], {})]).start(self)
        profile = self.profiles[0]
        delays = []
        loop = AgentLoop(self.client(profile, stub), ToolBelt(), max_turns=1,
                         retries=2, sleep=delays.append)
        result = loop.run("hi")
        self.assertEqual((result.status, result.text), ("ok", "OK"))
        self.assertEqual(len(stub.requests), 3)
        self.assertEqual([item["status"] for item in result.trace],
                         ["http_429", "http_500"])
        self.assertEqual(delays, [0, 1.0])

    def test_auth_error_is_not_retried(self):
        stub = Stub([(401, {"error": "bad key"}, {})] * 3).start(self)
        profile = self.profiles[1]
        loop = AgentLoop(self.client(profile, stub), ToolBelt(), max_turns=1,
                         retries=2, sleep=lambda _: None)
        result = loop.run("hi")
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(len(stub.requests), 1)

    def test_invalid_json_is_a_provider_error(self):
        stub = Stub([(200, b"not-json", {})]).start(self)
        profile = self.profiles[2]
        with self.assertRaises(ProviderError) as caught:
            self.client(profile, stub)([{"role": "user", "content": "hi"}], [])
        self.assertEqual(str(caught.exception), "invalid_response")

    def test_timeout_maps_to_transport_error(self):
        class TimedOut:
            def request(self, *args, **kwargs):
                raise TimeoutError

            def getresponse(self):
                raise AssertionError("unreachable")

            def close(self):
                pass

        with patch("nalar.go.HTTPSConnection", return_value=TimedOut()):
            client = ChatCompletions("vendor/model", "fixture-key",
                                     base_url="https://gateway.example/v1/chat/completions")
            with self.assertRaises(ProviderError) as caught:
                client([{"role": "user", "content": "hi"}], [])
        self.assertEqual(str(caught.exception), "request_timeout")

    def test_session_header_rotates(self):
        stub = Stub([(200, self.responses["text"], {})] * 3).start(self)
        profile = self.profiles[0]
        client = self.client(profile, stub)
        conversation = lambda text: [{"role": "system", "content": "s"},
                                     {"role": "user", "content": text}]
        client(conversation("one"), [])
        client(conversation("one") + [{"role": "assistant", "content": "x"}], [])
        client(conversation("two"), [])
        first = stub.requests[0]["headers"]["x-nalar-session"]
        continued = stub.requests[1]["headers"]["x-nalar-session"]
        second = stub.requests[2]["headers"]["x-nalar-session"]
        self.assertEqual(first, continued)
        self.assertNotEqual(first, second)


    def test_stream_tool_matrix_across_profiles(self):
        body = sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "list_files", "arguments": "{\"path\""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ": \".\"}"}}]}}]},
        )
        stub = Stub([(200, body, {})] * len(self.profiles)).start(self)
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                reply = self.client(profile, stub).stream(
                    [{"role": "user", "content": "list"}], [])
                self.assertEqual([call.name for call in reply.calls], ["list_files"])
                self.assertEqual(reply.calls[0].arguments, {"path": "."})


    def test_negative_stream_usage_is_ignored(self):
        body = sse(
            {"choices": [{"delta": {"content": "OK"}}]},
            {"choices": [], "usage": {"total_tokens": -500}},
        )
        stub = Stub([(200, body, {})]).start(self)
        reply = self.client(self.profiles[0], stub).stream(
            [{"role": "user", "content": "hi"}], [])
        self.assertEqual(reply.text, "OK")
        self.assertIsNone(reply.tokens)


if __name__ == "__main__":
    unittest.main()
