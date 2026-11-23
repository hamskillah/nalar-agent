"""Prompt commands: discovery, parsing and rendering."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.commands import (MAX_BODY, MAX_RENDERED, CommandError, check, discover,
                            find, render)
from nalar.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.commands = self.root / ".nalar" / "commands"
        self.commands.mkdir(parents=True)

    def write(self, name, text):
        (self.commands / f"{name}.md").write_text(text, encoding="utf-8")

    def test_discovery_parses_front_matter_and_deduplicates(self):
        self.write("review", "---\ndescription: Review a change\n---\nCheck $ARGUMENTS carefully.")
        self.write("plain", "First line\n\nBody text.")
        other = self.root / "extra"
        other.mkdir()
        (other / "review.md").write_text("ignored duplicate", encoding="utf-8")
        found = discover([self.commands, other])
        self.assertEqual([item.name for item in found], ["plain", "review"])
        review = find(found, "review")
        self.assertEqual(review.description, "Review a change")
        self.assertEqual(render(review, "app.py"), "Check app.py carefully.")
        plain = find(found, "plain")
        self.assertEqual(plain.description, "First line")
        self.assertEqual(render(plain), "First line\n\nBody text.")

    def test_check_explains_rejected_commands(self):
        self.write("ok", "Fine.")
        self.write("empty", "   ")
        self.write("Upper", "Name is rejected.")
        found, problems = check([self.commands])
        self.assertEqual([item.name for item in found], ["ok"])
        reasons = " ".join(item["reason"] for item in problems)
        self.assertIn("invalid command name", reasons)
        self.assertIn("body is empty", reasons)
        missing = self.root / "missing"
        found, problems = check([missing])
        self.assertEqual(found, [])
        self.assertIn("missing", problems[0]["reason"])

    def test_check_reports_duplicate_commands(self):
        self.write("review", "Fine.")
        other = self.root / "other"
        other.mkdir()
        (other / "review.md").write_text("Duplicate.", encoding="utf-8")
        found, problems = check([self.commands, other])
        self.assertEqual([item.name for item in found], ["review"])
        self.assertIn("duplicate", problems[0]["reason"])

    def test_cli_check_reports_problems_and_exit_codes(self):
        self.write("bump", "Bump $ARGUMENTS.")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}
        command = [sys.executable, "-m", "nalar", "commands", "check",
                   "--repo", str(self.root), "--format", "json"]
        clean = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                               text=True)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        payload = json.loads(clean.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual([item["name"] for item in payload["commands"]], ["bump"])

        self.write("empty", "   ")
        found = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                               text=True)
        self.assertEqual(found.returncode, 1, found.stderr)
        payload = json.loads(found.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])
        text = subprocess.run(command[:-2] + ["--format", "text"], cwd=ROOT,
                              env=env, capture_output=True, text=True)
        self.assertEqual(text.returncode, 1)
        self.assertIn("problem(s)", text.stdout)

    def test_unsafe_and_oversized_entries_are_skipped(self):
        self.write("ok", "Fine.")
        self.write("Upper", "Name is rejected.")
        self.write("bad name", "Space is rejected.")
        self.write("empty", "   ")
        self.write("huge", "x" * (MAX_BODY + 1))
        outside = self.root / "outside.md"
        outside.write_text("Symlinked.", encoding="utf-8")
        (self.commands / "link.md").symlink_to(outside)
        found = discover([self.commands])
        self.assertEqual([item.name for item in found], ["ok"])

    def test_config_discovers_the_default_directory(self):
        self.write("deploy", "Ship it with $ARGUMENTS.")
        config = load_config(self.root)
        self.assertEqual(len(config.commands), 1)
        self.assertEqual(find(discover(config.commands), "deploy").description, "Ship it with $ARGUMENTS.")

    def test_render_is_bounded_to_the_body(self):
        self.write("long", "x" * 10)
        found = find(discover([self.commands]), "long")
        self.assertEqual(render(found, "y" * 50),
                         "x" * 10 + "\n\nArguments: " + "y" * 50)
        self.assertEqual(render(found, ""), "x" * 10)

    def test_arguments_are_appended_when_the_body_has_no_placeholder(self):
        self.write("bump", "Set the answer.")
        found = find(discover([self.commands]), "bump")
        self.assertEqual(render(found, "to 42"), "Set the answer.\n\nArguments: to 42")
        self.assertEqual(render(found), "Set the answer.")

    def test_late_alphabetical_entries_survive_the_cap(self):
        for index in range(64):
            self.write(f"Bad{index}", "Invalid uppercase name.")
        self.write("zzz", "Kept after the invalid block.")
        found = discover([self.commands])
        self.assertEqual([item.name for item in found], ["zzz"])

    def test_render_rejects_expansion_beyond_the_task_limit(self):
        self.write("wide", "Use $ARGUMENTS.")
        found = find(discover([self.commands]), "wide")
        with self.assertRaises(CommandError):
            render(found, "y" * MAX_RENDERED)
        self.assertTrue(render(found, "fine").endswith("fine."))

    def test_body_limit_matches_the_task_limit(self):
        self.assertEqual(MAX_BODY, MAX_RENDERED)


class CommandCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".nalar" / "commands").mkdir(parents=True)
        (self.root / ".nalar" / "commands" / "audit.md").write_text(
            "---\ndescription: Audit the repo\n---\nAudit $ARGUMENTS.", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="], check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@e.test", "commit", "-qm", "fixture"], check=True)

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json"),
               "NALAR_API_KEY": "fixture"}
        return subprocess.run([sys.executable, "-m", "nalar", *args, "--repo", str(self.root)],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def test_chat_refuses_an_empty_expansion(self):
        calls = []

        def chat(messages, tools):
            calls.append(1)
            from nalar.core import Reply
            return Reply(text="ok")

        blank = self.root / ".nalar" / "commands"
        blank.mkdir(parents=True, exist_ok=True)
        (blank / "blank.md").write_text("$ARGUMENTS", encoding="utf-8")
        from nalar.coding import Repository
        from nalar.session import interactive

        out = __import__("io").StringIO()
        interactive(Repository(self.root), lambda: chat, __import__("io").StringIO("/blank\n/quit\n"),
                    out, commands=discover([blank]))
        self.assertEqual(calls, [])
        self.assertIn("empty task", out.getvalue())

    def test_chat_commands_lists_descriptions(self):
        from nalar.commands import Command
        from nalar.coding import Repository
        from nalar.session import interactive

        command = Command("audit", "Audit one area", "Audit $ARGUMENTS.", self.root / "audit.md")
        out = __import__("io").StringIO()
        interactive(Repository(self.root), lambda: None, __import__("io").StringIO("/commands\n/quit\n"),
                    out, commands=(command,))
        self.assertIn("/audit: Audit one area", out.getvalue())

    def test_chat_help_lists_discovered_commands(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        out = __import__("io").StringIO()
        interactive(Repository(self.root), lambda: None,
                    __import__("io").StringIO("/help\n/quit\n"), out,
                    commands=discover([self.root / ".nalar" / "commands"]))
        self.assertIn("/audit", out.getvalue())

    def test_chat_reports_an_oversized_expansion_without_calling_the_model(self):
        calls = []

        def chat(messages, tools):
            calls.append(messages)
            from nalar.core import Reply
            return Reply(text="ok")

        from nalar.coding import Repository
        from nalar.session import interactive

        out = __import__("io").StringIO()
        interactive(Repository(self.root), lambda: chat,
                    __import__("io").StringIO("/audit " + "x" * 7_000 + "\n/quit\n"), out,
                    commands=discover([self.root / ".nalar" / "commands"]))
        self.assertEqual(calls, [])
        self.assertIn("exceeds", out.getvalue())

    def test_cli_lists_commands(self):
        result = self.cli("commands", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([item["name"] for item in payload], ["audit"])
        self.assertEqual(payload[0]["description"], "Audit the repo")
        text = self.cli("commands", "list", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("audit: Audit the repo", text.stdout)
        shown = self.cli("commands", "show", "audit")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("$ARGUMENTS", json.loads(shown.stdout)["body"])
        shown_text = self.cli("commands", "show", "audit", "--format", "text")
        self.assertIn("Audit the repo", shown_text.stdout)
        self.assertEqual(self.cli("commands", "show", "missing").returncode, 2)

    def test_chat_slash_command_runs_the_template(self):
        messages = []

        class Chat:
            def __call__(self, msgs, tools):
                messages.append(msgs[-1]["content"])
                from nalar.core import Reply
                return Reply(text="ok")

            def stream(self, msgs, tools, on_text=None, cancel=None):
                from nalar.core import Reply
                messages.append(msgs[-1]["content"])
                return Reply(text="ok")

        from nalar.coding import Repository
        from nalar.session import interactive

        out = __import__("io").StringIO()
        interactive(Repository(self.root), lambda: Chat(),
                    __import__("io").StringIO("/audit src/app.py\n/quit\n"),
                    out, commands=discover([self.root / ".nalar" / "commands"]))
        self.assertIn("Audit src/app.py.", messages[0])
