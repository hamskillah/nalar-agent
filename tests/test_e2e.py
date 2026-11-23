import json
import os
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]


class Provider(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(payload)
        messages = payload["messages"]
        system = messages[0]["content"]
        if system.lower().startswith("you review code changes"):
            content = json.dumps([{"file": "app.py", "line": 1, "severity": "high",
                                   "title": "Wrong answer", "detail": "Return 41."}])
            calls = []
        elif messages[-1].get("role") == "tool":
            content = "Changed the answer."
            calls = []
        else:
            content = ""
            calls = [{"id": "call_1", "type": "function", "function": {
                "name": "replace",
                "arguments": json.dumps({"path": "app.py", "before": "41", "after": "42"})}}]
        body = json.dumps({"choices": [{"message": {"content": content,
                                                    "tool_calls": calls}}],
                           "usage": {"total_tokens": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 41\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c", "user.email=f@example.test",
                        "commit", "-qm", "fixture"], check=True)
        Provider.requests = []
        self.server = self.enterContext(HTTPServer(("127.0.0.1", 0), Provider))
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.config = {
            "model": "e2e",
            "provider": {"base_url": f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions",
                         "api_key_env": "E2E_KEY"},
        }
        self.write_config()

    def write_config(self):
        (self.repo / "nalar.json").write_text(json.dumps(self.config), encoding="utf-8")

    def nalar(self, *args):
        env = {**os.environ, "E2E_KEY": "e2e-secret", "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        return subprocess.run([sys.executable, "-m", "nalar", *map(str, args),
                               "--repo", str(self.repo), "--trust-project"],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_code_review_is_an_optional_gate(self):
        open_review = self.nalar("code", "--review", "Bump")
        self.assertEqual(open_review.returncode, 0, open_review.stderr)
        metadata = json.loads(open_review.stderr.strip().splitlines()[-1])
        self.assertEqual(metadata["review"]["status"], "ok")
        self.assertEqual(metadata["review"]["findings"][0]["severity"], "high")
        gated = self.nalar("code", "--review", "--review-fail-on", "high", "Bump")
        self.assertEqual(gated.returncode, 1)
        self.assertIn("+answer = 42", gated.stdout)

    def test_code_review_verify_end_to_end(self):
        result = self.nalar("code", "Bump the answer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+answer = 42", result.stdout)
        metadata = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual((metadata["status"], metadata["patch_created"]), ("ok", True))
        self.assertEqual((self.repo / "app.py").read_text(), "answer = 41\n")
        check = subprocess.run(["git", "-C", str(self.repo), "apply", "--check", "-"],
                               input=result.stdout, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(Provider.requests[0]["messages"][0]["role"], "system")
        self.assertEqual(Provider.requests[0]["model"], "e2e")

        (self.repo / "app.py").write_text("answer = 0\n")
        reviewed = self.nalar("review", "--scope", "worktree")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        payload = json.loads(reviewed.stdout)
        self.assertEqual(payload["findings"][0]["severity"], "high")

        self.config["verify"] = [[sys.executable, "-c", "print('checks pass')"]]
        self.write_config()
        verified = self.nalar("verify", "--yes")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("checks pass", json.loads(verified.stdout)["commands"][0]["output"])
