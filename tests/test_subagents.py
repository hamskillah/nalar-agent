import json
import tempfile
import unittest
from pathlib import Path

from nalar.config import ConfigError, load_config
from nalar.core import Reply
from nalar.extensions import build_extensions
from nalar.coding import Repository
from nalar.workspace import ToolError


class SubagentConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_subagent_model_and_limits_validate(self):
        config = load_config(self.root, self.root / "none.json")
        self.assertEqual((config.subagent_model, config.subagent_max_turns,
                          config.subagent_max_calls,
                          config.subagent_token_budget), (None, 6, 8, 40_000))
        self.write({"subagent_model": "vendor/tiny",
                    "subagent_max_turns": 3, "subagent_max_calls": 5,
                    "subagent_token_budget": 5_000})
        config = load_config(self.root, self.root / "none.json")
        self.assertEqual((config.subagent_model, config.subagent_max_turns,
                          config.subagent_max_calls,
                          config.subagent_token_budget),
                         ("vendor/tiny", 3, 5, 5_000))
        for value in ({"subagent_model": "bad model!"},
                      {"subagent_max_turns": 0},
                      {"subagent_max_turns": 21},
                      {"subagent_max_turns": True},
                      {"subagent_max_calls": 0},
                      {"subagent_max_calls": 41},
                      {"subagent_token_budget": 999},
                      {"subagent_token_budget": 200_001},
                      {"subagent_token_budget": True}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    load_config(self.root, self.root / "none.json",
                                trust_project=True)

    def test_subagents_default_on_and_can_be_disabled(self):
        self.assertTrue(load_config(self.root, self.root / "none.json").subagents)
        self.write({"subagents": False})
        self.assertFalse(load_config(self.root, self.root / "none.json",
                                     trust_project=True).subagents)
        self.write({"subagents": "yes"})
        with self.assertRaises(ConfigError):
            load_config(self.root, self.root / "none.json", trust_project=True)
        with self.assertRaises(ConfigError):
            load_config(self.root, self.root / "none.json")


class SubagentBeltTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="], check=True)
        (self.root / "app.py").write_text("answer = 41\n")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "fixture"], check=True)

    def extensions(self, chat_factory=None, subagent_factory=None, **values):
        (self.root / "nalar.json").write_text(json.dumps(values), encoding="utf-8")
        config = load_config(self.root, self.root / "none.json", trust_project=True)
        return build_extensions(Repository(self.root), config, start_mcp=False,
                                chat_factory=chat_factory,
                                subagent_factory=subagent_factory)

    def test_delegate_runs_a_read_only_subagent(self):
        seen = {}

        def chat(messages, tools):
            if messages[0]["content"].startswith("You are a read-only"):
                seen["sub_tools"] = {tool["function"]["name"] for tool in tools}
                return Reply(text="The parser lives in app.py.", tokens=5)
            return Reply(text="parent", tokens=1)

        extensions = self.extensions(lambda: chat)
        self.addCleanup(extensions.close)
        self.assertIn("delegate", {spec["function"]["name"] for spec in extensions.specs})
        answer = extensions.belt.call("delegate", {"task": "Where is the parser?"})
        self.assertEqual(answer, "The parser lives in app.py.")
        self.assertEqual(seen["sub_tools"],
                         {"list_files", "search", "read_lines", "find_symbol", "repo_map",
                          "git_log", "git_show"})
        self.assertNotIn("replace", seen["sub_tools"])

    def test_delegate_uses_the_configured_factory_and_limits(self):
        from unittest.mock import patch

        built = []
        seen = {}

        def parent(messages, tools):
            raise AssertionError("parent must not handle a delegated task")

        def subagent(messages, tools):
            return Reply(text="sub", tokens=1)

        def factory():
            built.append(True)
            return subagent

        class FakeLoop:
            def __init__(self, chat, belt, **kwargs):
                seen.update(kwargs)
                self.budget = kwargs["budget"]

            def run(self, task):
                from types import SimpleNamespace
                self.budget.add(7)
                return SimpleNamespace(status="ok", text="sub")

        with patch("nalar.agent.AgentLoop", FakeLoop):
            extensions = self.extensions(
                lambda: parent, subagent_factory=factory,
                subagent_model="vendor/tiny",
                subagent_max_turns=3, subagent_max_calls=4,
                subagent_token_budget=5_000)
            self.addCleanup(extensions.close)
            answer = extensions.belt.call("delegate", {"task": "explore"})
        self.assertEqual(answer, "sub")
        self.assertEqual(built, [True])
        self.assertEqual((seen["max_turns"], seen["max_calls"],
                          seen["token_budget"]), (3, 4, 5_000))
        self.assertEqual(extensions.subagent_usage,
                         [{"model": "vendor/tiny", "tokens": 7}])

    def test_delegate_is_absent_without_a_chat_or_when_disabled(self):
        extensions = self.extensions(None)
        self.addCleanup(extensions.close)
        self.assertNotIn("delegate", {spec["function"]["name"] for spec in extensions.specs})
        extensions = self.extensions(lambda: None, subagents=False)
        self.addCleanup(extensions.close)
        self.assertNotIn("delegate", {spec["function"]["name"] for spec in extensions.specs})

    def test_delegate_validates_and_reports_failures(self):
        def chat(messages, tools):
            return Reply(text="")

        extensions = self.extensions(lambda: chat)
        self.addCleanup(extensions.close)
        for args in ({}, {"task": ""}, {"task": "x" * 5000}, {"task": 1}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                extensions.belt.call("delegate", args)
        with self.assertRaises(ToolError) as caught:
            extensions.belt.call("delegate", {"task": "explore"})
        self.assertIn("empty_response", str(caught.exception))
