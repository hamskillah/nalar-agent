import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixtures") / "mcp_server.py"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.absent = self.repo / "absent.json"
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "fixture"], check=True)

    def doctor(self, *args, key="fixture-key", config=None, home=None):
        if config is not None:
            (self.repo / "nalar.json").write_text(json.dumps(config), encoding="utf-8")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent),
               "NALAR_API_KEY": key}
        if home is not None:
            env["HOME"] = str(home)
        return subprocess.run([sys.executable, "-m", "nalar", "doctor", "--repo", str(self.repo),
                               "--trust-project", *args],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def checks(self, result):
        return {item["name"]: item for item in json.loads(result.stdout)["checks"]}

    def test_healthy_repository_passes_without_calling_a_model(self):
        result = self.doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        checks = self.checks(result)
        self.assertTrue(checks["nalar"]["ok"])
        self.assertRegex(checks["nalar"]["detail"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(checks["python"]["ok"])
        self.assertTrue(checks["git"]["ok"])
        self.assertTrue(checks["api_key"]["ok"])
        self.assertTrue(checks["repository"]["ok"])
        self.assertTrue(checks["tui"]["ok"])
        self.assertNotIn("fixture-key", result.stdout)

    def test_missing_key_and_bad_config_fail(self):
        result = self.doctor(key="")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.checks(result)["api_key"]["ok"])
        result = self.doctor(config={"permission": "yolo"})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.checks(result)["config"]["ok"])

    def test_verify_suggests_detected_commands(self):
        (self.repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
        result = self.doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"])
        self.assertIn("detected", check["detail"])
        self.assertIn("python3 -m pytest -q", check["detail"])
        self.assertIn("init --detect", check["detail"])

    def test_prompt_commands_are_reported(self):
        commands = self.repo / ".nalar" / "commands"
        commands.mkdir(parents=True)
        (commands / "audit.md").write_text("Audit $ARGUMENTS.", encoding="utf-8")
        result = self.doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.checks(result)["commands"]
        self.assertTrue(check["ok"])
        self.assertIn("/audit", check["detail"])

    def test_verify_commands_are_checked_for_missing_executables(self):
        result = self.doctor(config={"verify": [["definitely-not-a-command-xyz", "-q"],
                                                [sys.executable, "-c", "pass"]]})
        check = self.checks(result)["verify"]
        self.assertFalse(check["ok"])
        self.assertIn("definitely-not-a-command-xyz", check["detail"])
        result = self.doctor(config={"verify": [[sys.executable, "-c", "pass"]]})
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"])
        self.assertIn("1 configured", check["detail"])
        result = self.doctor(config={})
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"])
        self.assertIn("no verification commands", check["detail"])

        scripts = self.repo / "scripts"
        scripts.mkdir()
        checker = scripts / "check.sh"
        checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(checker, 0o755)
        result = self.doctor(config={"verify": [["scripts/check.sh"]]})
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"], check["detail"])

    def test_instructions_report_nested_guidance(self):
        (self.repo / "AGENTS.md").write_text("root\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "AGENTS.md").write_text("nested\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "guidance"],
                       check=True)
        result = self.doctor()
        check = self.checks(result)["instructions"]
        self.assertTrue(check["ok"])
        self.assertIn("AGENTS.md", check["detail"])
        self.assertIn("nested", check["detail"])
        self.assertIn("src/AGENTS.md", check["detail"])

    def test_verify_distinguishes_worktree_and_committed_executables(self):
        scripts = self.repo / "scripts"
        scripts.mkdir()
        checker = scripts / "check.sh"
        checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(checker, 0o755)
        result = self.doctor(config={"verify": [["scripts/check.sh"]]})
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"])
        self.assertIn("not committed", check["detail"])
        subprocess.run(["git", "-C", str(self.repo), "add", "scripts/check.sh"],
                       check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "script"],
                       check=True)
        result = self.doctor(config={"verify": [["scripts/check.sh"]]})
        check = self.checks(result)["verify"]
        self.assertTrue(check["ok"])
        self.assertNotIn("not committed", check["detail"])

    def test_connect_checks_the_provider_without_a_model_call(self):
        import http.server
        import threading

        calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                body = b'{"data":[{"id":"alpha"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = self.enterContext(http.server.HTTPServer(("127.0.0.1", 0), Handler))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        result = self.doctor("--connect", config={
            "model": "alpha", "review_model": "alpha",
            "subagent_model": "alpha",
            "provider": {"base_url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        checks = self.checks(result)
        self.assertTrue(checks["provider_connect"]["ok"])
        self.assertTrue(checks["model_available"]["ok"])
        self.assertTrue(checks["review_model_available"]["ok"])
        self.assertTrue(checks["subagent_model_available"]["ok"])
        self.assertEqual(calls, ["/v1/models"])

    def test_connect_warns_when_the_model_is_not_listed(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"data":[{"id":"alpha"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = self.enterContext(http.server.HTTPServer(("127.0.0.1", 0), Handler))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        result = self.doctor("--connect", config={
            "model": "missing-model", "review_model": "missing-reviewer",
            "subagent_model": "missing-subagent",
            "provider": {"base_url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        reviewer = self.checks(result)["review_model_available"]
        self.assertFalse(reviewer["ok"])
        self.assertIn("not in the model list", reviewer["detail"])
        subagent = self.checks(result)["subagent_model_available"]
        self.assertFalse(subagent["ok"])
        self.assertIn("not in the model list", subagent["detail"])
        check = self.checks(result)["model_available"]
        self.assertFalse(check["ok"])
        self.assertIn("not in the model list", check["detail"])
        text = self.doctor("--connect", "--format", "text", config={
            "model": "missing-model",
            "provider": {"base_url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions"}})
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("WARN model_available", text.stdout)
        self.assertIn("Overall: OK", text.stdout)

    def test_text_format_summarizes_checks(self):
        result = self.doctor("--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK   python", result.stdout)
        self.assertIn("Overall: OK", result.stdout)
        missing = self.doctor("--format", "text", key="")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("FAIL api_key", missing.stdout)

    def test_tools_less_mcp_server_is_ready(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_resources.py"
        result = self.doctor("--mcp", config={
            "mcp": {"res": {"command": [sys.executable, str(fixture)]}}})
        self.assertEqual(result.returncode, 0, result.stderr)
        servers = {item["name"]: item for item in self.checks(result)["mcp"]["servers"]}
        self.assertEqual(servers["res"]["status"], "ok")

    def test_malformed_tools_are_not_reported_as_ready(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_invalid_tools.py"
        result = self.doctor("--mcp", config={
            "mcp": {"bad": {"command": [sys.executable, str(fixture)]}}})
        servers = {item["name"]: item for item in self.checks(result)["mcp"]["servers"]}
        self.assertEqual(servers["bad"]["status"], "no_valid_tools")
        self.assertFalse(self.checks(result)["mcp"]["ok"])
        self.assertTrue(self.checks(result)["mcp"]["required"])

    def test_mcp_servers_are_checked_on_request(self):
        result = self.doctor("--mcp", config={
            "mcp": {"fixture": {"command": [sys.executable, str(FIXTURE)]},
                    "broken": {"command": ["/nonexistent/nalar-server"]}}})
        self.assertEqual(result.returncode, 1, result.stderr)
        servers = {item["name"]: item for item in self.checks(result)["mcp"]["servers"]}
        self.assertEqual(servers["fixture"]["status"], "ok")
        self.assertNotEqual(servers["broken"]["status"], "ok")

    def test_web_state_permissions_are_reported(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = home / ".local" / "share" / "nalar"
        state.mkdir(parents=True)
        os.chmod(state, 0o700)
        workspaces = state / "workspaces"
        workspaces.mkdir()
        os.chmod(workspaces, 0o755)
        result = self.doctor(home=home)
        check = self.checks(result)["web_state"]
        self.assertFalse(check["ok"])
        self.assertIn("workspaces is not private", check["detail"])
        os.chmod(workspaces, 0o700)
        result = self.doctor(home=home)
        self.assertTrue(self.checks(result)["web_state"]["ok"])

    def test_state_mode_is_reported_before_the_store_repairs_it(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = home / ".local" / "share" / "nalar"
        state.mkdir(parents=True)
        os.chmod(state, 0o755)
        result = self.doctor(home=home)
        check = self.checks(result)["web_state"]
        self.assertFalse(check["ok"])
        self.assertIn("0o755", check["detail"])

    def test_sessions_check_is_not_duplicated(self):
        result = self.doctor()
        checks = json.loads(result.stdout)["checks"]
        self.assertEqual([item["name"] for item in checks].count("sessions"), 1)

    def test_verify_detail_escapes_newlines(self):
        result = self.doctor("--format", "text", config={
            "verify": [["missing\nINJECTED: doctor", "x"]]})
        self.assertIn("missing\\nINJECTED", result.stdout)
        self.assertNotIn("\nINJECTED: doctor\n", "\n" + result.stdout)
