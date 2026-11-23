import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nalar.__main__ import _approver
from nalar.agent import Runner
from nalar.config import ConfigError, load_config
from nalar.core import Call, Reply
from nalar.tools import ToolBelt


def read_belt(executed):
    belt = ToolBelt()

    def handler(args):
        executed.append(args.get("path", "?"))
        return {"text": "ok"}

    belt.add({"type": "function", "function": {
        "name": "fixture__write", "description": "write",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False}}}, handler)
    return belt


class CliApproverTests(unittest.TestCase):
    def test_yes_and_non_interactive_modes(self):
        with patch("sys.stdin.isatty", return_value=False):
            self.assertIsNone(_approver(SimpleNamespace(yes=False)))
        self.assertTrue(_approver(SimpleNamespace(yes=True))("replace", {}))

    def test_interactive_prompt_and_denials(self):
        args = SimpleNamespace(yes=False)
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="yes"), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(_approver(args)("replace", {"path": "a.py"}))
        self.assertIn("replace", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        for answer in ("no", ""):
            with patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input", return_value=answer):
                self.assertFalse(_approver(args)("write", {}))
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_approver(args)("write", {}))


class ApprovalConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_ask_patterns_are_parsed(self):
        self.write({"tools": {"allow": ["*"], "deny": [], "ask": ["fixture__*"]}})
        config = load_config(self.root, self.root / "none.json", trust_project=True)
        self.assertEqual(config.tools_ask, ("fixture__*",))
        self.assertEqual(config.tools_allow, ("*",))

    def test_invalid_ask_patterns_are_rejected(self):
        for value in ({"tools": {"ask": "x"}}, {"tools": {"ask": [""]}},
                      {"tools": {"ask": ["x"] * 33}}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    load_config(self.root, self.root / "none.json", trust_project=True)


class ApprovalLoopTests(unittest.TestCase):
    def chat(self, calls):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=calls)
            return Reply(text="done")

        return chat

    def test_belt_reports_tools_that_require_approval(self):
        belt = read_belt([])
        belt.apply_policy(None, (), ["fixture__*"])
        self.assertTrue(belt.requires_approval("fixture__write"))
        self.assertFalse(belt.requires_approval("read_lines"))

    def test_denied_tool_is_not_executed_and_is_reported(self):
        executed = []
        belt = read_belt(executed)
        belt.apply_policy(None, (), ["fixture__*"])
        seen = []

        def approve(name, args):
            seen.append(name)
            return False

        result = Runner(self.chat((Call("fixture__write", {}),)), belt,
                        approve=approve).run("task")
        self.assertEqual((result.status, executed, seen), ("ok", [], ["fixture__write"]))
        self.assertEqual(result.trace[0]["status"], "approval_required")

    def test_approved_tool_runs(self):
        executed = []
        belt = read_belt(executed)
        belt.apply_policy(None, (), ["fixture__*"])
        result = Runner(self.chat((Call("fixture__write", {}),)), belt,
                        approve=lambda name, args: True).run("task")
        self.assertEqual((result.status, executed), ("ok", ["?"]))

    def test_missing_approver_fails_closed(self):
        executed = []
        belt = read_belt(executed)
        belt.apply_policy(None, (), ["fixture__*"])
        result = Runner(self.chat((Call("fixture__write", {}),)), belt).run("task")
        self.assertEqual(executed, [])
        self.assertEqual(result.trace[0]["status"], "approval_required")

    def test_denial_skips_the_rest_of_the_batch(self):
        executed = []
        belt = read_belt(executed)
        belt.apply_policy(None, (), ["fixture__*"])
        calls = (Call("fixture__write", {"path": "a"}), Call("fixture__write", {"path": "b"}))
        result = Runner(self.chat(calls), belt, approve=lambda name, args: False).run("task")
        self.assertEqual(executed, [])
        self.assertEqual([step["status"] for step in result.trace],
                         ["approval_required", "skipped_after_error"])
