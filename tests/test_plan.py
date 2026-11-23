import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.core import Call, Reply


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 41\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c", "user.email=f@e.test",
                        "commit", "-qm", "fixture"], check=True)

    def run_plan(self, *args, output=None):
        seen = {}

        def chat(messages, tools):
            seen["tools"] = {tool["function"]["name"] for tool in tools}
            seen["prompt"] = messages[1]["content"]
            return Reply(text="# Plan\n1. Change app.py", tokens=5)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", *args, "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), seen

    def test_plan_is_read_only_and_can_be_saved(self):
        target = self.repo / "plan.md"
        code, out, err, seen = self.run_plan("--out", str(target))
        self.assertEqual(code, 0, err)
        self.assertIn("# Plan", out)
        self.assertNotIn("replace", seen["tools"])
        self.assertNotIn("create_file", seen["tools"])
        self.assertIn("Return a concrete implementation plan", seen["prompt"])
        self.assertEqual(target.read_text(), "# Plan\n1. Change app.py")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_plan_reports_extension_warnings(self):
        from nalar.tools import ToolBelt

        class Extensions:
            def __init__(self):
                self.belt = ToolBelt()
                self.system = "system"
                self.warnings = ("mcp srv: unavailable",)
                self.skills = ()

            def close(self):
                pass

        with patch("nalar.extensions.build_extensions", return_value=Extensions()):
            code, out, err, _ = self.run_plan("--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["warnings"], ["mcp srv: unavailable"])
        self.assertIn("warning: mcp srv: unavailable", err)

    def test_plan_does_not_write_a_file_when_the_run_fails(self):
        from nalar.core import ProviderError

        target = self.repo / "plan.md"

        def chat(messages, tools):
            raise ProviderError("http_500")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", "--retries", "0",
                                   "--out", str(target), "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        self.assertIn("no plan file was written", err.getvalue())

    def test_plan_attaches_context(self):
        context = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(context) / "requirements.md"
        path.write_text("Use pagination.", encoding="utf-8")
        code, out, err, seen = self.run_plan("--context", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("Use pagination.", seen["prompt"])

    def test_plan_streams_once_on_a_tty(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        class Chat:
            def __call__(self, messages, tools, cancel=None):
                return Reply(text="PLAN")

            def stream(self, messages, tools, on_text=None, cancel=None):
                on_text("PLAN")
                return Reply(text="PLAN")

        out = TTY()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=Chat()), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count("PLAN"), 1)

    def test_plan_json_output(self):
        code, out, err, _ = self.run_plan("--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_plan_refuses_symlink_target(self):
        outside = self.repo / "outside.md"
        outside.write_text("keep")
        link = self.repo / "plan.md"
        link.symlink_to(outside)
        code, _, err, _ = self.run_plan("--out", str(link))
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        self.assertEqual(outside.read_text(), "keep")

    def test_plan_can_record_the_run(self):
        sessions = self.repo / "sessions"

        def chat(messages, tools):
            return Reply(text="# Plan\n1. Change app.py", tokens=5)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
                    "NALAR_API_KEY": "k",
                    "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json"),
                    "NALAR_SESSIONS": str(sessions)}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", "--json", "--record",
                                   "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(payload["session"])
        self.assertEqual(loaded.summary()["task"], "Add a feature")

    def test_ask_can_record_the_run(self):
        sessions = self.repo / "sessions"

        def chat(messages, tools):
            return Reply(text="The answer.", tokens=2)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
                    "NALAR_API_KEY": "k",
                    "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json"),
                    "NALAR_SESSIONS": str(sessions)}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "ask", "--repo", str(self.repo),
                                   "--model", "fixture", "--json", "--record",
                                   "Where is the answer?"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(payload["session"])
        self.assertEqual(loaded.summary()["task"], "Where is the answer?")

    def test_plan_records_delegated_usage(self):
        (self.repo / "nalar.json").write_text(json.dumps({
            "model": "fixture", "subagent_model": "vendor/tiny"}),
            encoding="utf-8")
        sessions = self.repo / "sessions"

        def chat(messages, tools):
            if messages[0]["content"].startswith("You are a read-only"):
                return Reply(text="sub", tokens=7)
            if not any(message.get("role") == "tool" for message in messages):
                return Reply(calls=(Call("delegate", {"task": "explore"}),))
            return Reply(text="# Plan\n1. Change app.py", tokens=20)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
                    "NALAR_API_KEY": "k",
                    "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json"),
                    "NALAR_SESSIONS": str(sessions)}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", "--json", "--record",
                                   "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(payload["session"])
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((int(subagent["tokens"]), subagent["model"]),
                         (7, "vendor/tiny"))
        self.assertEqual(int(assistant["tokens"]) + 7, payload["tokens"])

    def test_plan_out_escapes_unpaired_surrogates(self):
        target = self.enterContext(tempfile.TemporaryDirectory())
        target = Path(target) / "plan.md"

        def chat(messages, tools):
            return Reply(text="plan \ud800 \x1b[31mred\x1b[0m \u202e \ufeff \x00 ok")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
                    "NALAR_API_KEY": "k",
                    "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "plan", "--repo", str(self.repo),
                                   "--model", "fixture", "--out", str(target),
                                   "Add a feature"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        written = target.read_text()
        self.assertIn("\\ud800", written)
        for bad in ("\x1b", "\x00", "\u202e", "\ufeff"):
            self.assertNotIn(bad, written)
        self.assertIn("red", written)
