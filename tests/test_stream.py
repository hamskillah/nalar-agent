import http.client
import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

from nalar.agent import Runner
from nalar.core import ProviderError, Reply
from nalar.go import ChatCompletions
from nalar.tools import ToolBelt


def sse(*chunks):
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


class StreamTests(unittest.TestCase):
    def serve(self, status, body, headers=None):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = self.enterContext(HTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.enterContext(patch("nalar.go.HTTPSConnection",
                                side_effect=lambda *a, **kw: http.client.HTTPConnection(
                                    "127.0.0.1", server.server_port, **kw)))
        return requests

    def test_assembles_text_tool_calls_and_usage(self):
        body = sse(
            {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "create", "arguments": '{"na'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'me":"a.txt"}'}}]}}]},
            {"choices": [], "usage": {"total_tokens": 21}},
        )
        requests = self.serve(200, body)
        pieces = []
        reply = ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([{"role": "user", "content": "hi"}],
                                           [], on_text=pieces.append)
        self.assertEqual(("".join(pieces), reply.text), ("Hello", "Hello"))
        self.assertEqual(reply.tokens, 21)
        self.assertEqual([(call.name, call.arguments, call.id) for call in reply.calls],
                         [("create", {"name": "a.txt"}, "call_1")])
        self.assertIs(requests[0]["stream"], True)
        self.assertEqual(requests[0]["model"], "m")

    def test_repeated_tool_names_are_not_concatenated(self):
        body = sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "create", "arguments": "{"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "create", "arguments": '"a":1}'}}]}}]},
        )
        requests = self.serve(200, body)
        reply = ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([{"role": "user", "content": "x"}], [])
        self.assertEqual([call.name for call in reply.calls], ["create"])
        self.assertEqual(requests[0]["stream_options"], {"include_usage": True})

    def test_cancel_stops_a_stream_after_partial_text(self):
        import threading
        body = sse(
            {"choices": [{"delta": {"content": "first"}}]},
            {"choices": [{"delta": {"content": "second"}}]},
        )
        self.serve(200, body)
        cancel = threading.Event()
        pieces = []

        def on_text(piece):
            pieces.append(piece)
            cancel.set()

        with self.assertRaises(ProviderError) as caught:
            ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([{"role": "user", "content": "x"}], [],
                                        on_text=on_text, cancel=cancel)
        self.assertEqual(str(caught.exception), "cancelled")
        self.assertEqual(pieces, ["first"])

    def test_error_status_and_partial_stream_failures(self):
        self.serve(500, b"")
        with self.assertRaises(ProviderError) as caught:
            ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([], [])
        self.assertEqual(str(caught.exception), "http_500")

    def test_streamed_finish_reason_is_not_accepted_as_complete(self):
        body = sse({"choices": [{"delta": {"content": "half"},
                                 "finish_reason": "length"}]})
        self.serve(200, body)
        with self.assertRaises(ProviderError) as caught:
            ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([{"role": "user", "content": "x"}], [])
        self.assertEqual(str(caught.exception), "incomplete_response")

    def test_malformed_sse_lines_are_ignored(self):
        body = b'data: {bad json\n\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        self.serve(200, body)
        reply = ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions").stream([], [])
        self.assertEqual(reply.text, "ok")


class StreamAgentTests(unittest.TestCase):
    def test_loop_streams_text_and_returns_final_reply(self):
        pieces = []

        class Chat:
            def stream(self, messages, tools, on_text=None):
                for piece in ("a", "b"):
                    on_text(piece)
                return Reply(text="ab")

        result = Runner(Chat(), ToolBelt()).run("task", on_text=pieces.append)
        self.assertEqual((result.status, result.text, "".join(pieces)), ("ok", "ab", "ab"))

    def test_loop_reports_cancelled_when_the_stream_is_interrupted(self):
        class Chat:
            def stream(self, messages, tools, on_text=None, cancel=None):
                on_text("partial")
                raise ProviderError("cancelled")

        result = Runner(Chat(), ToolBelt()).run("task", on_text=lambda _: None)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.trace[-1]["status"], "cancelled")

    def test_no_retry_after_partial_output(self):
        attempts = []

        class Chat:
            def stream(self, messages, tools, on_text=None):
                attempts.append(1)
                on_text("partial")
                raise ProviderError("transport_unavailable")

        result = Runner(Chat(), ToolBelt()).run("task", on_text=lambda _: None)
        self.assertEqual((result.status, len(attempts)), ("provider_error", 1))
