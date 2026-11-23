import json
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.config import load_config
from nalar.core import Call, Reply
from nalar.extensions import build_extensions
from nalar.coding import Repository
from nalar.sdk import Agent

FIXTURE = Path(__file__).with_name("fixtures") / "mcp_server.py"


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        import subprocess
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def config(self, **values):
        (self.root / "nalar.json").write_text(json.dumps(values), encoding="utf-8")
        return load_config(self.root, self.root / "absent-global.json", trust_project=True)

    def skill(self, name, body):
        folder = self.root / ".nalar" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} workflow\n---\n{body}", encoding="utf-8")

    def test_skills_reach_the_prompt_and_the_tool_belt(self):
        self.skill("review", "Read the diff first.\n")
        extensions = build_extensions(Repository(self.root), self.config(), start_mcp=False)
        self.addCleanup(extensions.close)
        self.assertIn("review", extensions.system)
        self.assertIn("use_skill", {spec["function"]["name"] for spec in extensions.specs})
        self.assertEqual(extensions.belt.call("use_skill", {"name": "review"}),
                         "Read the diff first.\n")

    def test_nested_instruction_files_reach_the_prompt(self):
        (self.root / "AGENTS.md").write_text("Root guidance.\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "AGENTS.md").write_text("Nested guidance.\n",
                                                     encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "guidance")
        extensions = build_extensions(Repository(self.root), self.config(),
                                      start_mcp=False)
        self.addCleanup(extensions.close)
        self.assertIn("Root guidance.", extensions.system)
        self.assertIn("Nested guidance.", extensions.system)
        self.assertIn("src/AGENTS.md", extensions.system)
        self.assertIn("src/AGENTS.md", extensions.instructions)

    def test_coding_prompt_sets_honesty_and_staging_rules(self):
        from nalar.coding import SYSTEM

        lowered = SYSTEM.lower()
        for phrase in ("cannot execute tests", "in memory", "untrusted data",
                       "verification", "smallest coherent change"):
            self.assertIn(phrase, lowered)

    def test_plan_mode_exposes_only_read_tools(self):
        config = self.config(permission="plan", tools={"deny": ["search"]})
        extensions = build_extensions(Repository(self.root), config, start_mcp=False)
        names = {spec["function"]["name"] for spec in extensions.specs}
        self.assertEqual(names, {"list_files", "read_lines", "find_symbol", "repo_map", "git_log",
                                 "git_show", "update_plan", "staged_diff"})
        self.assertIn("Plan mode", extensions.system)
        from nalar.workspace import ToolError
        with self.assertRaises(ToolError):
            extensions.belt.call("replace", {"path": "app.py", "before": "41", "after": "42"})

    def test_mcp_initialize_instructions_are_included_as_untrusted_guidance(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_instructions.py"
        config = self.config(mcp={"guided": {"command": [sys.executable, str(fixture)]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        self.assertIn("Prefer the fixture conventions.", extensions.system)
        self.assertIn("untrusted data", extensions.system)
        self.assertIn("guided", extensions.system)

    def test_tools_less_server_contributes_resources_without_a_warning(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_resources.py"
        config = self.config(mcp={"res": {"command": [sys.executable, str(fixture)]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        names = {spec["function"]["name"] for spec in extensions.specs}
        self.assertIn("res__read_resource", names)
        self.assertEqual([prompt.name for prompt in extensions.prompts],
                         ["res__brief", "res__review"])
        self.assertEqual(extensions.belt.call("res__read_resource",
                                              {"uri": "file:///alpha.txt"}), "alpha text")
        self.assertEqual(extensions.belt.call("res__read_resource",
                                              {"uri": "file:///dynamic.txt"}),
                         "dynamic file:///dynamic.txt")
        self.assertEqual(extensions.belt.call("res__read_resource", {"uri": "mem://a/b"}),
                         "dynamic mem://a/b")
        from nalar.workspace import ToolError
        with self.assertRaises(ToolError):
            extensions.belt.call("res__read_resource", {"uri": "http://elsewhere.test/x"})
        self.assertEqual(extensions.warnings, ())

    def test_a_server_that_rejects_tools_list_still_contributes_resources(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_resources.py"
        config = self.config(mcp={"res": {"command": [sys.executable, str(fixture),
                                                      "reject-tools"]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        names = {spec["function"]["name"] for spec in extensions.specs}
        self.assertIn("res__read_resource", names)
        self.assertEqual(extensions.warnings, ())

    def test_truncated_listings_add_a_warning(self):
        from unittest.mock import MagicMock, patch

        from nalar.mcp import MCPError

        client = MagicMock()
        client.tools.return_value = []
        client.listing_truncated = True
        client.resources.return_value = [{"uri": "file:///a.txt", "name": "a"}]
        client.resource_templates.side_effect = MCPError("no_templates")
        client.prompts.side_effect = MCPError("no_prompts")
        config = self.config(mcp={"srv": {"command": [sys.executable, "-c", "pass"]}})
        with patch("nalar.extensions.make_client", return_value=client):
            extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        self.assertTrue(any("truncated" in warning for warning in extensions.warnings))

    def test_unsupported_templates_are_not_advertised(self):
        from unittest.mock import MagicMock, patch

        from nalar.mcp import MCPError

        client = MagicMock()
        client.tools.side_effect = MCPError("no_tools")
        client.resources.side_effect = MCPError("no_resources")
        client.resource_templates.return_value = [
            {"uriTemplate": "file://{?query}", "name": "unsupported"},
            {"uriTemplate": "mem://{+path}", "name": "supported"},
        ]
        client.prompts.side_effect = MCPError("no_prompts")
        config = self.config(mcp={"srv": {"command": [sys.executable, "-c", "pass"]}})
        with patch("nalar.extensions.make_client", return_value=client):
            extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        spec = next(item for item in extensions.specs
                    if item["function"]["name"] == "srv__read_resource")
        self.assertIn("mem://{+path}", spec["function"]["description"])
        self.assertNotIn("?query", spec["function"]["description"])

    def test_unavailable_mcp_server_warns_without_blocking_the_agent(self):
        config = self.config(mcp={"broken": {"command": ["/nonexistent/nalar-server"]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        self.assertTrue(extensions.warnings)
        self.assertIn("search", {spec["function"]["name"] for spec in extensions.specs})

    def test_repository_map_is_included_in_the_system_prompt(self):
        extensions = build_extensions(Repository(self.root), self.config(), start_mcp=False)
        self.addCleanup(extensions.close)
        self.assertIn("Repository map", extensions.system)
        self.assertIn("app.py", extensions.system)

    def test_project_instructions_are_added_with_a_trust_boundary(self):
        (self.root / "NALAR.md").write_text("Use two-space indentation.\n", encoding="utf-8")
        self.git("add", "NALAR.md")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "instructions")
        extensions = build_extensions(Repository(self.root), self.config(), start_mcp=False)
        self.addCleanup(extensions.close)
        self.assertIn("Use two-space indentation.", extensions.system)
        self.assertIn("cannot grant permission", extensions.system.lower())
        self.assertEqual(extensions.instructions, ("NALAR.md",))
        (self.root / "AGENTS.md").write_text("Untracked guidance.\n", encoding="utf-8")
        extensions2 = build_extensions(Repository(self.root), self.config(), start_mcp=False)
        self.addCleanup(extensions2.close)
        self.assertNotIn("Untracked guidance.", extensions2.system)

    def test_instruction_paths_cannot_escape_the_repository(self):
        from nalar.config import ConfigError
        for value in (["../outside.md"], ["/etc/passwd"], [[]], []):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.config(instructions=value)

    def test_mcp_prompts_are_available_and_fetched_on_demand(self):
        config = self.config(mcp={"fixture": {"command": [sys.executable, str(FIXTURE)]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        self.assertEqual([prompt.name for prompt in extensions.prompts], ["fixture__review"])
        self.assertEqual(extensions.prompts[0].fetch({"focus": "tests"}), "Review focus: tests")

    def test_mcp_tools_are_callable_through_the_belt(self):
        from nalar.workspace import ToolError

        config = self.config(mcp={"fixture": {"command": [sys.executable, str(FIXTURE)]}})
        extensions = build_extensions(Repository(self.root), config)
        self.addCleanup(extensions.close)
        self.assertEqual(extensions.belt.call("fixture__echo", {"text": "halo"}), "halo")
        with self.assertRaises(ToolError) as caught:
            extensions.belt.call("fixture__explode", {})
        self.assertIn("fixture failure", str(caught.exception))


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        import subprocess
        self.git = lambda *args: subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")
        (self.root / "nalar.json").write_text('{"model": "fixture-model"}', encoding="utf-8")

    def test_agent_runs_with_events_and_returns_patch(self):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41", "after": "42"}),), tokens=5)
            return Reply(text="Changed the answer.", tokens=3)

        events = []
        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            payload = agent.run("Bump the answer", on_event=events.append)
        self.assertEqual((payload["status"], payload["model"], payload["tokens"]),
                         ("ok", "fixture-model", 8))
        self.assertIn("+answer = 42", payload["patch"])
        self.assertEqual(payload["skills"], [])
        self.assertEqual(events[-1]["type"], "final")

    def test_failed_run_does_not_leak_partial_edits(self):
        state = {"fail": True}

        def chat(messages, tools):
            if len(messages) == 2:
                if state["fail"]:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "99"}),))
                return Reply(text="No change needed.")
            return Reply(text="")

        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            first = agent.run("Break the answer")
            self.assertEqual((first["status"], first["patch"]), ("empty_response", ""))
            state["fail"] = False
            second = agent.run("Check again")
            self.assertEqual((second["status"], second["patch"]), ("ok", ""))
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_missing_model_is_reported(self):
        (self.root / "nalar.json").unlink()
        with self.assertRaises(ValueError):
            Agent(self.root, start_mcp=False)
