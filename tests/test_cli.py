import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

FIXTURE = Path(__file__).with_name("fixtures") / "mcp_server.py"
ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.absent = self.repo / "absent-global.json"

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent),
               "NALAR_API_KEY": ""}
        return subprocess.run([sys.executable, "-m", "nalar", *map(str, args), "--repo", str(self.repo),
                               "--trust-project"],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def write(self, value):
        (self.repo / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_confirmation_treats_eof_as_no(self):
        from unittest.mock import patch

        from nalar.__main__ import _confirm

        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_confirm("Type yes: "))

    def test_plan_text_formats_statuses(self):
        from nalar.__main__ import _plan_text

        lines = _plan_text([
            {"step": "Read", "status": "completed"},
            {"step": "Edit", "status": "in_progress"},
            {"step": "Test", "status": "pending"},
        ])
        self.assertEqual(lines, ["  [x] Read", "  [>] Edit", "  [ ] Test"])

    def test_clean_strips_c1_and_bidi_controls(self):
        from nalar.__main__ import _clean

        self.assertEqual(_clean("a\x9bb"), "ab")
        self.assertEqual(_clean("a\u202eb"), "ab")
        self.assertEqual(_clean("a\nb\tc"), "a\nb\tc")
        self.assertIn("\u200d", _clean("\U0001f468\u200d\U0001f469"))
        self.assertEqual(_clean("a\u200eb\u200fc"), "abc")
        self.assertIn("\u200c", _clean("a\u200cb"))
        self.assertEqual(_clean("ok\x1b[2J"), "ok")
        self.assertEqual(_clean("ok\x1b]0;title\x07"), "ok")
        self.assertEqual(_clean("ok\x1b]0;title"), "ok")

    def test_review_text_formats_findings(self):
        from nalar.__main__ import _review_text

        lines = _review_text({"findings": [
            {"severity": "high", "file": "a.py", "line": 3, "title": "Bug"}]})
        self.assertEqual(lines, ["  ! [high] a.py:3 Bug"])

    def test_version_flag_prints_the_package_version(self):
        from nalar import __version__

        result = self.cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"nalar {__version__}", result.stdout)

    def test_plugins_list_reports_configured_plugin_tools(self):
        fixtures = Path(__file__).with_name("fixtures")
        global_file = self.repo / "global.json"
        global_file.write_text(json.dumps({"plugins": ["nalar_demo_plugin"]}),
                               encoding="utf-8")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(global_file),
               "PYTHONPATH": str(fixtures)}
        command = [sys.executable, "-m", "nalar", "plugins", "list",
                   "--repo", str(self.repo)]
        text = subprocess.run([*command, "--format", "text"], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("demo_lookup: Look up a demo value.", text.stdout)
        encoded = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(encoded.returncode, 0, encoded.stderr)
        payload = json.loads(encoded.stdout)
        self.assertEqual(payload["configured"], ["nalar_demo_plugin"])
        self.assertIn("entry_points", payload)
        self.assertEqual(payload["tools"], [{"name": "demo_lookup",
                                             "description": "Look up a demo value."}])

    def test_config_show_reports_effective_values_without_secrets(self):
        self.write({"model": "cfg-model", "review_model": "vendor/reviewer",
                    "subagent_model": "vendor/tiny",
                    "subagent_max_turns": 3, "subagent_max_calls": 4,
                    "subagent_token_budget": 5000,
                    "max_turns": 5,
                    "mcp": {"docs": {"command": ["docs-server"],
                                     "env": {"TOKEN": "{env:CLI_TEST_TOKEN}"}}}})
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent), "CLI_TEST_TOKEN": "top-secret"}
        result = subprocess.run([sys.executable, "-m", "nalar", "config", "show", "--repo", str(self.repo),
                                 "--trust-project"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("top-secret", result.stdout)
        value = json.loads(result.stdout)
        self.assertEqual((value["model"], value["review_model"],
                          value["subagent_model"], value["subagent_max_turns"],
                          value["subagent_max_calls"],
                          value["subagent_token_budget"], value["max_turns"],
                          value["permission"]),
                         ("cfg-model", "vendor/reviewer", "vendor/tiny", 3, 4,
                          5000, 5, "ask"))
        self.assertEqual(value["mcp"][0]["env"], ["TOKEN"])
        self.assertEqual(value["provider"]["api_key_env"], "NALAR_API_KEY")
        self.assertTrue(value["provider"]["session_header"])
        shown = subprocess.run([sys.executable, "-m", "nalar", "config", "show", "--repo",
                                str(self.repo), "--trust-project", "--format", "text"],
                               cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("top-secret", shown.stdout)
        self.assertIn("model: cfg-model", shown.stdout)
        self.assertIn("review_model: vendor/reviewer", shown.stdout)
        self.assertIn("subagent_model: vendor/tiny", shown.stdout)
        self.assertIn("subagent_max_turns: 3", shown.stdout)
        self.assertIn("subagent_max_calls: 4", shown.stdout)
        self.assertIn("subagent_token_budget: 5000", shown.stdout)
        self.assertIn("subagents: true", shown.stdout)
        self.assertIn("provider.session_header: true", shown.stdout)
        self.assertIn("provider.base_url:", shown.stdout)
        self.assertIn("provider.headers:", shown.stdout)
        self.assertIn("mcp: docs(on)", shown.stdout)

    def test_skills_list_reports_discovered_skills(self):
        folder = self.repo / ".nalar" / "skills" / "review"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: review\ndescription: review a diff\n---\nRead the diff.\n", encoding="utf-8")
        result = self.cli("skills", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["name"] for item in json.loads(result.stdout)], ["review"])
        text = self.cli("skills", "list", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("review: review a diff", text.stdout)
        shown = self.cli("skills", "show", "review")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["body"], "Read the diff.\n")
        shown_text = self.cli("skills", "show", "review", "--format", "text")
        self.assertIn("Read the diff.", shown_text.stdout)
        self.assertEqual(self.cli("skills", "show", "missing").returncode, 2)
        self.assertEqual(self.cli("skills", "show").returncode, 2)

    def test_mcp_list_starts_configured_servers(self):
        self.write({"mcp": {"fixture": {"command": [sys.executable, str(FIXTURE)]},
                            "broken": {"command": ["/nonexistent/nalar-server"]}}})
        result = self.cli("mcp", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = {item["name"]: item for item in json.loads(result.stdout)}
        self.assertEqual(report["fixture"]["status"], "ok")
        self.assertIn("fixture__echo", report["fixture"]["tools"])
        self.assertEqual(report["fixture"]["resources"], ["fixture://readme"])
        self.assertEqual(report["fixture"]["prompts"], ["review"])
        self.assertNotEqual(report["broken"]["status"], "ok")

    def test_invalid_configuration_is_rejected(self):
        self.write({"permission": "yolo"})
        result = self.cli("config", "show")
        self.assertEqual(result.returncode, 2)
        self.assertIn("permission", result.stderr)

    def test_mcp_test_initializes_and_calls_a_tool(self):
        self.write({"mcp": {"fixture": {"command": [sys.executable, str(FIXTURE)]}}})
        result = self.cli("mcp", "test", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("fixture__echo", report["tools"])
        self.assertEqual(report["resources"], ["fixture://readme"])
        self.assertEqual(report["prompts"], ["review"])
        called = self.cli("mcp", "test", "fixture", "--tool", "fixture__echo", "--arg", "text=hi")
        self.assertEqual(called.returncode, 0, called.stderr)
        self.assertEqual(json.loads(called.stdout)["call"]["result"], "hi")
        missing = self.cli("mcp", "test", "missing")
        self.assertEqual(missing.returncode, 2)

    def test_text_format_is_rejected_for_unsupported_actions(self):
        result = self.cli("config", "set", "model", "beam-free", "--format", "text")
        self.assertEqual(result.returncode, 2)
        self.assertIn("config show", result.stderr)
        fixture = Path(__file__).with_name("fixtures") / "mcp_empty.py"
        self.write({"mcp": {"empty": {"command": [sys.executable, str(fixture)]}}})
        result = self.cli("mcp", "remove", "empty", "--format", "text")
        self.assertEqual(result.returncode, 2)
        self.assertIn("read-only MCP actions", result.stderr)

    def test_mcp_list_text_format(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_resources.py"
        self.write({"mcp": {"res": {"command": [sys.executable, str(fixture)]}}})
        result = self.cli("mcp", "list", "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("res: ok", result.stdout)
        self.assertIn("file:///alpha.txt", result.stdout)

    def test_mcp_resources_lists_templates_and_reads(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_resources.py"
        self.write({"mcp": {"res": {"command": [sys.executable, str(fixture)]}}})
        listing = self.cli("mcp", "resources", "res")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        payload = json.loads(listing.stdout)
        self.assertEqual(payload["templates"][0]["uriTemplate"], "file:///{path}")
        self.assertEqual([item["uri"] for item in payload["resources"]],
                         ["file:///alpha.txt", "file:///beta.txt"])
        read = self.cli("mcp", "resources", "res", "--read", "file:///dynamic.txt")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["read"]["text"],
                         "dynamic file:///dynamic.txt")
        text = self.cli("mcp", "resources", "res", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("file:///alpha.txt", text.stdout)
        self.assertIn("template: file:///{path}", text.stdout)
        read_text = self.cli("mcp", "resources", "res", "--read",
                             "file:///dynamic.txt", "--format", "text")
        self.assertIn("dynamic file:///dynamic.txt", read_text.stdout)

    def test_mcp_prompt_text_is_sanitized(self):
        fixture = Path(__file__).with_name("fixtures") / "mcp_escape_prompt.py"
        self.write({"mcp": {"escape": {"command": [sys.executable, str(fixture)]}}})
        result = self.cli("mcp", "prompt", "escape__danger", "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\u202e", result.stdout)
        self.assertIn("safe", result.stdout)

    def test_mcp_test_handles_tools_less_and_disabled_servers(self):
        empty = Path(__file__).with_name("fixtures") / "mcp_empty.py"
        self.write({"mcp": {"empty": {"command": [sys.executable, str(empty)]},
                            "off": {"command": [sys.executable, str(empty)], "enabled": False}}})
        result = self.cli("mcp", "test", "empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tools"], [])
        text = self.cli("mcp", "test", "empty", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("empty: ok", text.stdout)
        disabled = self.cli("mcp", "test", "off")
        self.assertEqual(disabled.returncode, 2)
        self.assertIn("disabled", disabled.stderr)

    def test_mcp_prompts_can_be_listed_and_rendered(self):
        self.write({"mcp": {"fixture": {"command": [sys.executable, str(FIXTURE)]}}})
        listed = self.cli("mcp", "prompts")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        prompts = json.loads(listed.stdout)
        self.assertEqual([item["command"] for item in prompts], ["fixture__review"])
        text = self.cli("mcp", "prompts", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("fixture__review:", text.stdout)
        rendered = self.cli("mcp", "prompt", "fixture__review", "--arg", "focus=security")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(json.loads(rendered.stdout)["text"].strip(),
                         "Review focus: security")
        plain = self.cli("mcp", "prompt", "fixture__review", "--arg", "focus=security",
                         "--format", "text")
        self.assertEqual(plain.stdout.strip(), "Review focus: security")

    def test_mcp_add_and_remove_edit_the_user_config(self):
        global_config = self.repo / "user-config.json"
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(global_config)}
        add = subprocess.run([sys.executable, "-m", "nalar", "mcp", "add", "fixture", "--",
                              sys.executable, str(FIXTURE)], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(add.returncode, 0, add.stderr)
        data = json.loads(global_config.read_text())
        self.assertEqual(data["mcp"]["fixture"]["command"][0], sys.executable)
        disabled = subprocess.run([sys.executable, "-m", "nalar", "mcp", "disable", "fixture"],
                                  cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(json.loads(disabled.stdout)["disabled"], "fixture")
        self.assertIs(json.loads(global_config.read_text())["mcp"]["fixture"]["enabled"],
                      False)
        enabled = subprocess.run([sys.executable, "-m", "nalar", "mcp", "enable", "fixture"],
                                 cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertTrue(json.loads(global_config.read_text())["mcp"]["fixture"]["enabled"])
        listed = subprocess.run([sys.executable, "-m", "nalar", "mcp", "list",
                                 "--repo", str(self.repo)], cwd=ROOT, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual([item["name"] for item in json.loads(listed.stdout)], ["fixture"])
        removed = subprocess.run([sys.executable, "-m", "nalar", "mcp", "remove", "fixture"],
                                 cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotIn("fixture", json.loads(global_config.read_text()).get("mcp", {}))

    def test_ask_is_read_only_and_prints_the_answer(self):
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 42\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "fixture"], check=True)
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from nalar.__main__ import main
        from nalar.core import Reply

        seen = {}

        def chat(messages, tools):
            seen["tools"] = {tool["function"]["name"] for tool in tools}
            return Reply(text="The answer.")

        out, err = io.StringIO(), io.StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "ask", "--repo", str(self.repo),
                                   "--model", "fixture", "Where is the answer?"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("The answer.", out.getvalue())
        self.assertNotIn("replace", seen["tools"])
        self.assertNotIn("create_file", seen["tools"])

    def test_ask_reports_extension_warnings(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from nalar.__main__ import main
        from nalar.core import Reply
        from nalar.tools import ToolBelt

        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 42\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "fixture"], check=True)

        class Extensions:
            def __init__(self):
                self.belt = ToolBelt()
                self.system = "system"
                self.skills = ()
                self.warnings = ("mcp srv: unavailable",)

            def close(self):
                pass

        out, err = io.StringIO(), io.StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__.ChatCompletions",
                      return_value=lambda *_: Reply(text="ok")), \
                patch("nalar.extensions.build_extensions", return_value=Extensions()), \
                patch("sys.argv", ["nalar", "ask", "--repo", str(self.repo),
                                   "--model", "fixture", "--json", "Question?"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["warnings"], ["mcp srv: unavailable"])
        self.assertIn("warning: mcp srv: unavailable", err.getvalue())

    def test_config_set_and_unset_edit_user_values(self):
        global_config = self.repo / "user-config.json"
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(global_config)}
        set_result = subprocess.run([sys.executable, "-m", "nalar", "config", "set",
                                     "model", "beam-free"], cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(set_result.returncode, 0, set_result.stderr)
        shown = subprocess.run([sys.executable, "-m", "nalar", "config", "show",
                                "--repo", str(self.repo)], cwd=ROOT, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(shown.stdout)["model"], "beam-free")
        bad = subprocess.run([sys.executable, "-m", "nalar", "config", "set",
                              "permission", "yolo"], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(bad.returncode, 2)
        removed = subprocess.run([sys.executable, "-m", "nalar", "config", "unset", "model"],
                                 cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotIn("model", json.loads(global_config.read_text()))

    def test_sessions_list_reads_the_private_store(self):
        from nalar.store import SessionStore

        sessions = self.repo / "sessions"
        store = SessionStore(sessions)
        session = store.create(self.repo, "cfg-model", "base")
        store.append(session, "user", "Fix the parser")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent),
               "NALAR_SESSIONS": str(sessions)}
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "list", "--repo", str(self.repo)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(result.stdout)
        self.assertEqual([(item["id"], item["task"]) for item in listed],
                         [(session.id, "Fix the parser")])

    def test_sessions_search_returns_snippets_and_text(self):
        from nalar.store import SessionStore

        sessions = self.repo / "sessions"
        store = SessionStore(sessions)
        session = store.create(self.repo, "cfg-model", "base")
        store.append(session, "user", "Fix the parser")
        store.append(session, "assistant", "Changed parser.py")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent),
               "NALAR_SESSIONS": str(sessions)}
        command = [sys.executable, "-m", "nalar", "sessions", "search",
                   "--query", "parser", "--repo", str(self.repo)]
        found = subprocess.run(command, cwd=ROOT, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=30)
        self.assertEqual(found.returncode, 0, found.stderr)
        payload = json.loads(found.stdout)
        self.assertEqual(payload[0]["id"], session.id)
        self.assertEqual([match["kind"] for match in payload[0]["matches"]],
                         ["user", "assistant"])
        text = subprocess.run(command + ["--format", "text"], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("user: Fix the parser", text.stdout)
        missing = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "search",
             "--repo", str(self.repo)], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--query", missing.stderr)
        markdown = subprocess.run(command + ["--format", "markdown"], cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(markdown.returncode, 2)

    def test_sessions_export_honors_json_text_and_markdown(self):
        from nalar.store import SessionStore

        sessions = self.repo / "sessions"
        store = SessionStore(sessions)
        session = store.create(self.repo, "cfg-model", "base")
        store.append(session, "user", "Fix the parser")
        store.append(session, "assistant", "Done")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.absent),
               "NALAR_SESSIONS": str(sessions)}
        command = [sys.executable, "-m", "nalar", "sessions", "export",
                   session.id, "--repo", str(self.repo)]
        exported = subprocess.run(command + ["--format", "json"], cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        payload = json.loads(exported.stdout)
        self.assertEqual(payload["id"], session.id)
        self.assertEqual(payload["entries"][0]["text"], "Fix the parser")

        text = subprocess.run(command + ["--format", "text"], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Fix the parser", text.stdout)
        self.assertIn("Session " + session.id, text.stdout)

        markdown = subprocess.run(command, cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(markdown.returncode, 0, markdown.stderr)
        self.assertIn("# Nalar session", markdown.stdout)
        self.assertIn("## User", markdown.stdout)

        invalid = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "list",
             "--repo", str(self.repo), "--format", "markdown"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("markdown", invalid.stderr)

    def test_print_patch_sanitizes_tty_but_preserves_pipe_bytes(self):
        from nalar.__main__ import _print_patch

        class Tty:
            def __init__(self):
                self.buffer = io.BytesIO()
                self.text = io.StringIO()

            def isatty(self):
                return True

            def write(self, value):
                self.text.write(value)

            def flush(self):
                pass

        tty = Tty()
        with redirect_stdout(tty):
            _print_patch("a\x1b[31mb\u202ec")
        self.assertNotIn("\x1b", tty.text.getvalue())
        self.assertNotIn("\u202e", tty.text.getvalue())
        self.assertIn("\\x1b", tty.text.getvalue())
        self.assertEqual(tty.buffer.getvalue(), b"")

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        with redirect_stdout(stream):
            _print_patch("a\x1b[31mb")
        stream.flush()
        self.assertEqual(raw.getvalue(), b"a\x1b[31mb")

    def test_print_patch_flushes_prior_text_first(self):
        from nalar.__main__ import _print_patch

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        with redirect_stdout(stream):
            print("STATUS")
            _print_patch("diff --git a/x b/x\n")
        stream.flush()
        self.assertTrue(raw.getvalue().startswith(b"STATUS\n"))

    def test_print_patch_handles_surrogates_and_closed_streams(self):
        from nalar.__main__ import _print_patch

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        with redirect_stdout(stream):
            _print_patch("a\ud800b")
        stream.flush()
        self.assertEqual(raw.getvalue(), b"a\\ud800b")

        closed = io.StringIO()
        closed.close()
        with redirect_stdout(closed):
            _print_patch("ignored")

    def test_print_patch_returns_false_when_the_stream_fails(self):
        from nalar.__main__ import _print_patch

        class Full:
            def __init__(self):
                self.buffer = self
            def isatty(self):
                return False
            def flush(self):
                pass
            def write(self, value):
                raise OSError("full")

        with redirect_stdout(Full()):
            self.assertFalse(_print_patch("x"))

    def test_mcp_tool_error_text_is_escaped(self):
        self.write({"mcp": {"fixture": {"command": [sys.executable, str(FIXTURE)]}}})
        result = self.cli("mcp", "test", "fixture", "--tool", "fixture__explode")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fixture failure", result.stderr)
        self.assertNotIn("\x1b", result.stderr)
        self.assertIn("\\nOK forged", result.stderr)
        self.assertNotIn("\nOK forged\n", "\n" + result.stderr)
