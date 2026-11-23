import http.client
import json
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from nalar.benchmark import benchmark
from nalar.core import Reply, run
from nalar.go import ChatCompletions
from nalar.workspace import Workspace


class IntegrationTests(unittest.TestCase):
    def serve(self, responses):
        requests = []
        replies = deque(responses)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                 self.headers))
                response = replies.popleft()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

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

    def test_http_tool_roundtrip_creates_artifact(self):
        calls = [{"id": "call_42", "type": "function",
                  "function": {"name": "create", "arguments": '{"name":"answer.txt","text":"42\\n"}'}}]
        requests = self.serve([
            {"choices": [{"message": {"content": None, "tool_calls": calls}}], "usage": {"total_tokens": 7}},
            {"choices": [{"message": {"content": "Done"}}], "usage": {"total_tokens": 3}},
        ])
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with Workspace(root) as workspace:
            result = run(ChatCompletions("test-model", "fixture-key", base_url="https://gateway.example/v1/chat/completions"), workspace, "Create the answer.")
        self.assertEqual((result.status, result.text, result.tokens), ("ok", "Done", 10))
        self.assertEqual((root / "answer.txt").read_bytes(), b"42\n")
        self.assertEqual(requests[0][0], "/v1/chat/completions")
        payload = requests[1][1]
        self.assertEqual(payload["model"], "test-model")
        self.assertIs(payload["stream"], False)
        self.assertIn("create", {tool["function"]["name"] for tool in payload["tools"]})
        self.assertEqual(payload["messages"][-1]["tool_call_id"], "call_42")
        self.assertEqual(payload["messages"][-2]["tool_calls"], calls)
        self.assertEqual(json.loads(payload["messages"][-1]["content"])["ok"], True)
        self.assertEqual(requests[0][2]["Authorization"], "Bearer fixture-key")
        self.assertTrue(requests[0][2]["User-Agent"].startswith("nalar-agent/"))
        self.assertTrue(requests[0][2]["x-nalar-session"])
        self.assertEqual(requests[0][2]["x-nalar-session"], requests[1][2]["x-nalar-session"])

    def test_malformed_provider_payload_is_not_a_success(self):
        self.serve([{"choices": [{"message": {"content": "", "tool_calls": [{"function": None}]}}]}])
        root = self.enterContext(tempfile.TemporaryDirectory())
        with Workspace(root) as workspace:
            result = run(ChatCompletions("test-model", "fixture-key", base_url="https://gateway.example/v1/chat/completions"), workspace, "task")
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(result.trace[0]["status"], "invalid_response")

    def test_cli_requires_key(self):
        result = subprocess.run([sys.executable, "-m", "nalar", "bench", "--model", "beam-free"],
                                env={}, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"NALAR_API_KEY", result.stderr)

    def test_recovers_a_malformed_call_without_running_later_batch_members(self):
        def call(call_id, name, args):
            return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}

        requests = self.serve([
            {"choices": [{"message": {"content": None, "tool_calls": [
                call("bad", "read", '{"name":'),
                call("premature", "create", '{"name":"out.txt","text":"guessed"}'),
            ]}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [
                call("corrected", "create", '{"name":"out.txt","text":"verified"}'),
            ]}}]},
            {"choices": [{"message": {"content": "Done"}}]},
        ])
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with Workspace(root) as workspace:
            result = run(ChatCompletions("test-model", "fixture-key", base_url="https://gateway.example/v1/chat/completions"), workspace, "Do the task.")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.calls, 3)
        self.assertEqual((root / "out.txt").read_text(), "verified")
        tools = requests[1][1]["messages"][-2:]
        self.assertEqual(
            [(msg["tool_call_id"], json.loads(msg["content"])["error"]) for msg in tools],
            [("bad", "invalid_arguments"), ("premature", "skipped_after_error")],
        )
        self.assertEqual([step["status"] for step in result.trace],
                         ["invalid_arguments", "skipped_after_error", "ok"])

    def test_benchmark_requires_exact_artifacts(self):
        from nalar.core import Call

        def chat(messages, _):
            prompt = messages[1]["content"]
            if "sum" in prompt:
                return Reply(text="Done, trust me")
            if "copy" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("read", {"name": "source.txt"}),))
                if len(messages) == 4:
                    return Reply(calls=(Call("create", {"name": "copy.txt",
                                                         "text": "alpha\n"}),))
                return Reply(text="Done.")
            if len(messages) == 2:
                return Reply(calls=(Call("create", {"name": "ready-alt.txt",
                                                     "text": "READY\n"}),))
            return Reply(text="Done.")

        report = benchmark(chat, model="local-test")
        self.assertEqual(report["model"], "local-test")
        self.assertEqual([row["passed"] for row in report["runs"]],
                         [True, True, False, False, True, True, False, False,
                          False, False, False, False])
        self.assertNotIn("trust me", json.dumps(report))

    def test_benchmark_passes_with_exact_artifacts(self):
        from nalar.core import Call

        def chat(messages, _):
            prompt = messages[1]["content"]
            if "sum" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("create", {"name": "total.txt", "text": "18\n"}),))
                return Reply(text="Done.")
            if "copy" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("read", {"name": "source.txt"}),))
                if len(messages) == 4:
                    return Reply(calls=(Call("create", {"name": "copy.txt",
                                                         "text": "alpha\n"}),))
                return Reply(text="Done.")
            if "Delete obsolete.txt" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("delete", {"name": "obsolete.txt"}),))
                return Reply(text="Done.")
            if "Replace DRAFT" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"name": "note.txt",
                                                         "before": "DRAFT",
                                                         "after": "FINAL"}),))
                return Reply(text="Done.")
            if "Rename old.txt" in prompt:
                if len(messages) == 2:
                    return Reply(calls=(Call("rename", {"name": "old.txt",
                                                     "new_name": "new.txt"}),))
                return Reply(text="Done.")
            if len(messages) == 2:
                return Reply(calls=(Call("create", {"name": "ready-alt.txt",
                                                     "text": "READY\n"}),))
            return Reply(text="Done.")

        report = benchmark(chat, model="local-test")
        self.assertTrue(all(row["passed"] for row in report["runs"]), report)

    def test_cli_help_lists_commands(self):
        output = subprocess.run([sys.executable, "-m", "nalar", "--help"], capture_output=True, check=True)
        for command in (b"run", b"bench", b"serve"):
            self.assertRegex(output.stdout, rb"(?m)^\s+" + command + rb"\s{2,}")

    def test_benchmark_contains_a_real_create_collision(self):
        from nalar.core import Call

        def chat(messages, _):
            if "ready.txt" not in messages[1]["content"]:
                return Reply(text="Done")
            if len(messages) == 2:
                return Reply(calls=(Call("create", {"name": "ready.txt", "text": "READY\n"}),))
            last = json.loads(messages[-1]["content"])
            if last.get("error") == "already_exists":
                return Reply(calls=(Call("create", {"name": "ready-alt.txt", "text": "READY\n"}),))
            return Reply(text="Done")

        report = benchmark(chat, model="local-test")
        first_pair = report["runs"][:2]
        self.assertEqual(
            [(row["status"], row["passed"]) for row in first_pair],
            [("tool_error", False), ("ok", True)],
        )
