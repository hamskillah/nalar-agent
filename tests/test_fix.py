import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.agent import Budget
from nalar.coding import Repository
from nalar.config import load_config
from nalar.core import Call, ProviderError, Reply
from nalar.fix import run_fix

ROOT = Path(__file__).resolve().parents[1]


class FixTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture-model",
            "verify": [[sys.executable, "-c",
                        "import app; assert app.answer == 42"]],
        }), encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "fixture")
        self.config = load_config(self.root, global_file=self.root / "missing.json")
        self.repo = Repository(self.root)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def chat(self, tasks, replies=None):
        replies = replies or [
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"}),)),
            Reply(text="done"),
        ]
        queue = list(replies)

        def run(messages, tools):
            tasks.append(messages[-1]["content"])
            return queue.pop(0) if len(queue) > 1 else queue[0]

        return run

    def test_fix_proposes_a_patch_after_a_failed_snapshot_verification(self):
        tasks = []
        payload = run_fix(self.repo, self.chat(tasks), self.config, attempts=1)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["already_ok"])
        self.assertEqual(payload["attempts"], 1)
        self.assertTrue(payload["verification"]["ok"])
        self.assertIn("+answer = 42", payload["patch"])
        self.assertIn("assert app.answer == 42", tasks[0])
        self.assertIn("The committed snapshot fails", tasks[0])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_an_already_passing_snapshot_needs_no_model(self):
        (self.root / "app.py").write_text("answer = 42\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "passing")
        called = []
        payload = run_fix(Repository(self.root), lambda *_: called.append(1),
                          self.config, attempts=2)
        self.assertTrue(payload["already_ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["patch"], "")
        self.assertEqual(called, [])

    def test_zero_attempts_reports_failure_without_a_model(self):
        payload = run_fix(self.repo, None, self.config, attempts=0)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["already_ok"])
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["patch"], "")

    def test_a_model_that_does_not_stage_a_patch_fails_cleanly(self):
        tasks = []
        payload = run_fix(self.repo, self.chat(tasks, [Reply(text="cannot fix")]),
                          self.config, attempts=2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["patch"], "")
        self.assertEqual(payload["error"], "no_patch")
        self.assertFalse(payload["verification"]["ok"])

    def test_fix_prompt_includes_instructions_and_context(self):
        tasks = []
        payload = run_fix(self.repo, self.chat(tasks), self.config, attempts=1,
                          instructions="Prefer a tiny change.",
                          context="--- NOTES.md ---\nUse tabs.")
        self.assertEqual(payload["status"], "ok")
        self.assertIn("Prefer a tiny change.", tasks[0])
        self.assertIn("Use tabs.", tasks[0])

    def test_fix_emits_one_initial_verify_and_attempt_events(self):
        events = []
        payload = run_fix(self.repo, self.chat([]), self.config, attempts=1,
                          on_event=events.append)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(events[0], {"type": "verify", "ok": False, "attempt": 0})
        self.assertIn({"type": "fix_attempt", "attempt": 1}, events)
        self.assertEqual(events[-1], {"type": "verify", "ok": True, "attempt": 1})
        self.assertEqual(sum(1 for item in events if item.get("type") == "verify"
                             and item.get("attempt") == 0), 1)

    def test_fix_rejects_oversized_instructions_or_context(self):
        with self.assertRaisesRegex(ValueError, "instructions"):
            run_fix(self.repo, self.chat([]), self.config, attempts=1,
                    instructions="x" * 4001)
        with self.assertRaisesRegex(ValueError, "context is too large"):
            run_fix(self.repo, self.chat([]), self.config, attempts=1,
                    context="x" * 32_001)

    def test_fix_rejects_non_string_guidance(self):
        with self.assertRaisesRegex(ValueError, "strings"):
            run_fix(self.repo, self.chat([]), self.config, attempts=1,
                    instructions=None)

    def test_fix_reports_a_provider_failure(self):
        def chat(messages, tools):
            raise ProviderError("provider_error")

        payload = run_fix(self.repo, chat, self.config, attempts=1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "provider_error")

    def test_fix_restores_staging_when_solving_crashes(self):
        calls = []

        def chat(messages, tools):
            calls.append(1)
            if len(calls) == 1:
                return Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41", "after": "42"}),))
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            run_fix(self.repo, chat, self.config, attempts=1)
        self.assertEqual(self.repo.patch(), "")

    def test_fix_refuses_a_staged_proposal(self):
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        with self.assertRaisesRegex(ValueError, "staged"):
            run_fix(self.repo, None, self.config, attempts=1)

    def test_fix_retries_with_previous_failure_feedback(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture-model",
            "verify": [[sys.executable, "-c",
                        "import app; assert app.answer == 43"]],
        }), encoding="utf-8")
        self.git("add", "nalar.json")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "verify-43")
        config = load_config(self.root, global_file=self.root / "missing.json")
        tasks = []
        replies = [
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"}),), tokens=3),
            Reply(text="first try", tokens=2),
            Reply(calls=(Call("replace", {"path": "app.py", "before": "42",
                                          "after": "43"}),), tokens=4),
            Reply(text="second try", tokens=1),
        ]

        def chat(messages, tools):
            tasks.append(messages[-1]["content"])
            return replies.pop(0)

        budget = Budget(0)
        payload = run_fix(Repository(self.root), chat, config, attempts=2,
                          budget=budget)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(payload["tokens"], 10)
        self.assertEqual(budget.spent, 10)
        self.assertGreaterEqual(payload["turns"], 2)
        self.assertGreaterEqual(payload["calls"], 2)
        self.assertIn("+answer = 43", payload["patch"])
        self.assertIn("Verification failed", tasks[2])
        self.assertIn("assert app.answer == 43", tasks[2])
        self.assertIn("AssertionError", tasks[2])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_an_unexportable_snapshot_is_not_sent_to_the_model(self):
        def never(messages, tools):
            raise AssertionError("the model must not be called")

        payload = run_fix(
            self.repo, never, self.config, attempts=2,
            initial={"ok": False, "error": "Cannot export the snapshot.",
                     "commands": []})
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["attempts"], 0)
        self.assertIn("Cannot export", payload["verification"]["error"])

    def test_invalid_attempts_and_missing_verify_are_refused(self):
        with self.assertRaisesRegex(ValueError, "attempts"):
            run_fix(self.repo, None, self.config, attempts=4)
        with self.assertRaisesRegex(ValueError, "no verify"):
            run_fix(self.repo, None, replace(self.config, verify=()), attempts=1)

    def test_cli_fix_reports_an_already_passing_snapshot_without_a_model(self):
        (self.root / "app.py").write_text("answer = 42\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "passing")
        sessions = self.root / "fix-sessions"
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json"),
               "NALAR_SESSIONS": str(sessions)}
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--record", "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["already_ok"])
        self.assertEqual(payload["patch"], "")
        self.assertEqual(payload["attempts"], 0)
        self.assertIn("session", payload)

        text = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--record", "--format", "text"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Verified: the committed snapshot already passes.", text.stdout)
        self.assertIn("session ", text.stdout)

        events = self.root.parent / "fix-events.jsonl"
        with_events = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--events", str(events), "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(with_events.returncode, 0, with_events.stderr)
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(lines, [{"type": "verify", "ok": True, "attempt": 0}])

    def test_cli_fix_requires_configured_verification_and_consent(self):
        (self.root / "nalar.json").write_text("{}", encoding="utf-8")
        self.git("add", "nalar.json")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "no-verify")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json")}
        missing = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("Configure verify commands", missing.stderr)

        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "revert", "--no-edit", "HEAD")
        without_yes = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(without_yes.returncode, 2)
        self.assertIn("--yes", without_yes.stderr)

    def test_cli_fix_writes_the_patch_and_reports_verification(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "fix.patch"
        chat = self.chat([])
        stdout = io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__._client", return_value=chat), \
                patch.object(sys, "argv", [
                    "nalar", "fix", "--repo", str(self.root), "--yes",
                    "--no-extensions", "--patch-out", str(target),
                    "--format", "json"]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["model"], "fixture-model")
        self.assertIn("+answer = 42", target.read_text())
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_cli_fix_needs_a_key_when_the_snapshot_fails(self):
        env = {key: value for key, value in os.environ.items()
               if key != "NALAR_API_KEY"}
        env["NALAR_GLOBAL_CONFIG"] = str(self.root / "missing.json")
        events = Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.jsonl"
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--events", str(events)],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Set NALAR_API_KEY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(lines, [{"type": "verify", "ok": False, "attempt": 0}])

    def test_cli_fix_yes_approves_tools_listed_as_ask(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture-model",
            "verify": [[sys.executable, "-c",
                        "import app; assert app.answer == 42"]],
            "tools": {"ask": ["replace"]},
        }), encoding="utf-8")
        chat = self.chat([])
        stdout = io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__._client", return_value=chat), \
                patch.object(sys, "argv", [
                    "nalar", "fix", "--repo", str(self.root), "--yes",
                    "--trust-project", "--no-extensions", "--format", "json"]),                 contextlib.redirect_stdout(stdout),                 contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertIn("+answer = 42", payload["patch"])

    def test_a_rejected_patch_is_rolled_back_and_not_offered(self):
        calls = []

        def chat(messages, tools):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41", "after": "42"}),))
            return Reply(text="fixed")

        with patch("nalar.fix.verify_patch", return_value={
                "ok": False, "error": "patch_rejected", "commands": []}):
            payload = run_fix(self.repo, chat, self.config, attempts=2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["patch"], "")
        self.assertEqual(self.repo.patch(), "")
        self.assertEqual(len(calls), 2)

    def test_cli_fix_events_have_one_initial_verify(self):
        events = Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.jsonl"
        chat = self.chat([])
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__._client", return_value=chat), \
                patch.object(sys, "argv", [
                    "nalar", "fix", "--repo", str(self.root), "--yes",
                    "--no-extensions", "--events", str(events),
                    "--format", "json"]),                 contextlib.redirect_stdout(io.StringIO()),                 contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        initial = [item for item in lines
                   if item.get("type") == "verify" and item.get("attempt") == 0]
        self.assertEqual(len(initial), 1)
        self.assertIn({"type": "fix_attempt", "attempt": 1}, lines)

    def test_cli_fix_passes_instructions_and_context_to_the_model(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        context = directory / "notes.md"
        context.write_text("Use tabs.\n", encoding="utf-8")
        prompts = []

        def chat(messages, tools):
            prompts.append(messages[-1]["content"])
            if len(prompts) == 1:
                return Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41", "after": "42"}),))
            return Reply(text="fixed")

        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__._client", return_value=chat), \
                patch.object(sys, "argv", [
                    "nalar", "fix", "--repo", str(self.root), "--yes",
                    "--no-extensions", "--instructions", "Prefer minimal",
                    "--context", str(context), "--format", "json"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        self.assertIn("Prefer minimal", prompts[0])
        self.assertIn("Use tabs.", prompts[0])

    def test_cli_fix_validates_instructions_and_context(self):
        too_long = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--instructions", "x" * 4001],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(too_long.returncode, 2)
        self.assertIn("--instructions", too_long.stderr)

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        args = [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
                "--yes"]
        for number in range(5):
            path = directory / f"context{number}.md"
            path.write_text("x\n", encoding="utf-8")
            args.extend(["--context", str(path)])
        too_many = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(too_many.returncode, 2)
        self.assertIn("At most 4", too_many.stderr)

        large_args = [sys.executable, "-m", "nalar", "fix", "--repo",
                      str(self.root), "--yes"]
        for number in range(4):
            path = directory / f"large{number}.md"
            path.write_text("x" * 9_000, encoding="utf-8")
            large_args.extend(["--context", str(path)])
        too_large = subprocess.run(large_args, cwd=ROOT, capture_output=True,
                                   text=True)
        self.assertEqual(too_large.returncode, 2)
        self.assertIn("32000", too_large.stderr)

    def test_cli_fix_rejects_a_symlinked_events_path(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = directory / "real.jsonl"
        target.write_text("", encoding="utf-8")
        link = directory / "events.jsonl"
        link.symlink_to(target)
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--events", str(link)],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Cannot write events", result.stderr)

    def test_cli_fix_rejects_bad_attempt_counts(self):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json")}
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "fix", "--repo", str(self.root),
             "--yes", "--max-fix-attempts", "4"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--max-fix-attempts", result.stderr)

    def test_cli_fix_attributes_subagent_usage(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture-model",
            "subagent_model": "vendor/tiny",
            "verify": [[sys.executable, "-c",
                        "import app; assert app.answer == 42"]],
        }), encoding="utf-8")

        def fake_client(provider, model, fallbacks=()):
            def chat(messages, tools):
                if messages[0]["content"].startswith("You are a read-only"):
                    return Reply(text="sub", tokens=7)
                results = sum(1 for message in messages
                              if message.get("role") == "tool")
                if results == 0:
                    return Reply(calls=(Call("delegate", {"task": "explore"}),))
                if results == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),))
                return Reply(text="fixed")
            return chat

        sessions = self.root / "sessions"
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "fix.patch"
        stdout = io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_SESSIONS": str(sessions)}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch.object(sys, "argv", [
                    "nalar", "fix", "--repo", str(self.root), "--yes",
                    "--record", "--patch-out", str(target),
                    "--format", "json"]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("+answer = 42", target.read_text())
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(payload["session"])
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((int(subagent["tokens"]), subagent["model"]),
                         (7, "vendor/tiny"))
        self.assertEqual(int(assistant["tokens"]) + 7, payload["tokens"])
