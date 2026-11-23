import json
import os
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]


class FixLoopTests(unittest.TestCase):
    state = {"fail_review": False, "reviews": []}

    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 41\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F",
                        "-c", "user.email=f@e.test", "commit", "-qm", "fixture"], check=True)
        self.requests = []
        FixLoopTests.state = {"fail_review": False, "reviews": [],
                              "repair_breaks": False}

    def serve(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.requests.append(payload)
                messages = payload["messages"]
                if messages[0]["content"].lower().startswith("you review code changes"):
                    from_server = FixLoopTests.state
                    from_server["reviews"].append(messages[1]["content"])
                    if from_server["fail_review"]:
                        self.send_response(500)
                        self.end_headers()
                        return
                    content = json.dumps([{"file": "app.py", "line": 1, "severity": "high",
                                           "title": "Defect", "detail": "Fix it"}])
                    calls = []
                elif messages[-1].get("role") == "tool":
                    content, calls = "Done.", []
                elif "A reviewer found defects" in messages[1]["content"]:
                    before, after = ("42", "97") if FixLoopTests.state.get("repair_breaks") else ("0", "42")
                    content, calls = "", [{"id": "c3", "type": "function", "function": {
                        "name": "replace",
                        "arguments": json.dumps({"path": "app.py", "before": before,
                                                 "after": after})}}]
                elif "failed the configured verification" in messages[1]["content"]:
                    content, calls = "", [{"id": "c2", "type": "function", "function": {
                        "name": "replace",
                        "arguments": json.dumps({"path": "app.py", "before": "0",
                                                 "after": "42"})}}]
                else:
                    content, calls = "", [{"id": "c1", "type": "function", "function": {
                        "name": "replace",
                        "arguments": json.dumps({"path": "app.py", "before": "41",
                                                 "after": "0"})}}]
                body = json.dumps({"choices": [{"message": {"content": content,
                                                            "tool_calls": calls}}],
                                   "usage": {"total_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.config = {
            "model": "e2e",
            "provider": {"base_url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                         "api_key_env": "E2E_KEY"},
            "verify": [[sys.executable, "-c", "import app; assert app.answer == 42"]],
        }
        (self.repo / "nalar.json").write_text(json.dumps(self.config), encoding="utf-8")

    def code(self, *args):
        env = {**os.environ, "E2E_KEY": "k", "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        return subprocess.run([sys.executable, "-m", "nalar", "code", "--repo", str(self.repo),
                               "--trust-project", *args, "Fix the answer"],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_review_sees_the_final_patch_after_repair(self):
        self.serve()
        result = self.code("--yes", "--verify", "--fix-attempts", "1", "--review")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(FixLoopTests.state["reviews"])
        self.assertIn("+answer = 42", FixLoopTests.state["reviews"][-1])
        self.assertNotIn("+answer = 0", FixLoopTests.state["reviews"][-1])

    def test_review_repair_updates_the_patch_and_reviews_again(self):
        self.serve()
        result = self.code("--review", "--review-fix-attempts", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(metadata["review_fix_attempts"], 1)
        self.assertEqual(len(metadata["review_history"]), 2)
        self.assertIn("+answer = 42", FixLoopTests.state["reviews"][-1])
        self.assertNotIn("+answer = 0", result.stdout)

    def test_review_repair_is_bounded(self):
        self.serve()
        events = Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.ndjson"
        result = self.code("--review", "--review-fix-attempts", "2", "--events", str(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(metadata["review_fix_attempts"], 2)
        self.assertEqual(len(metadata["review_history"]), 3)
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        attempts = [item["attempt"] for item in lines if item["type"] == "review_fix"]
        self.assertEqual(attempts, [1, 2])

    def test_a_repair_that_fails_verification_is_not_reported_as_reviewed(self):
        self.serve()
        FixLoopTests.state["repair_breaks"] = True
        result = self.code("--review", "--review-fix-attempts", "1", "--verify", "--yes",
                           "--fix-attempts", "1")
        self.assertEqual(result.returncode, 1)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertFalse(metadata["verification"]["ok"])
        self.assertEqual(metadata["review"]["status"], "skipped")
        self.assertEqual([item["status"] for item in metadata["review_history"]],
                         ["ok", "skipped"])
        self.assertIn("+answer = 97", result.stdout)

    def test_review_repair_requires_review(self):
        self.serve()
        result = self.code("--review-fix-attempts", "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--review", result.stderr)

    def test_review_failure_keeps_the_patch_and_returns_nonzero(self):
        self.serve()
        FixLoopTests.state["fail_review"] = True
        result = self.code("--review")
        self.assertEqual(result.returncode, 1)
        self.assertIn("+answer = 0", result.stdout)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(metadata["review"]["status"], "provider_error")

    def test_preflight_failure_triggers_a_bounded_fix(self):
        self.serve()
        result = self.code("--yes", "--verify", "--fix-attempts", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertTrue(metadata["verification"]["ok"])
        self.assertEqual(metadata["fix_attempts"], 1)
        self.assertIn("+answer = 42", result.stdout)
        self.assertEqual(len(self.requests), 4)
        self.assertIn("failed the configured verification",
                      self.requests[2]["messages"][1]["content"])

    def test_verification_failure_without_fix_returns_nonzero(self):
        self.serve()
        result = self.code("--yes", "--verify")
        self.assertEqual(result.returncode, 1)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertFalse(metadata["verification"]["ok"])
        self.assertIn("+answer = 0", result.stdout)
        self.assertEqual(len(self.requests), 2)

    def test_events_report_each_verification_attempt(self):
        self.serve()
        events = Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.ndjson"
        result = self.code("--yes", "--verify", "--fix-attempts", "1", "--events", str(events))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual([item["attempt"] for item in lines if item["type"] == "verify"], [0, 1])
        self.assertIn({"type": "fix_attempt", "attempt": 1}, lines)

    def test_verification_feedback_includes_the_failed_patch(self):
        from nalar.__main__ import _verification_feedback

        text = _verification_feedback({"error": "patch_rejected", "detail": "context"},
                                      "diff --git a/app.py b/app.py\n+new\n")
        self.assertIn("patch_rejected", text)
        self.assertIn("Failed patch", text)
        self.assertIn("+new", text)

    def test_verify_requires_confirmation_without_yes(self):
        self.serve()
        result = self.code("--verify")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--yes", result.stderr)

    def test_fix_attempts_requires_verify(self):
        self.serve()
        result = self.code("--fix-attempts", "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--fix-attempts requires --verify", result.stderr)
