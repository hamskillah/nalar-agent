"""The embedding SDK: run, prompt commands, apply, review and verify."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.core import Call, Reply
from nalar.sdk import Agent, WorkspaceAgent


class SdkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / ".nalar" / "commands").mkdir(parents=True)
        (self.root / ".nalar" / "commands" / "bump.md").write_text(
            "---\ndescription: Bump the answer\n---\nSet answer to $ARGUMENTS.", encoding="utf-8")
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "fixture-model",
            "verify": [[sys.executable, "-c", "import app; assert app.answer == 42"]],
        }), encoding="utf-8")
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def chat(self, tasks):
        def chat(messages, tools):
            if messages[0]["content"].startswith("You review code changes"):
                return Reply(text=json.dumps([{"file": "app.py", "line": 1,
                                               "severity": "low", "title": "Note",
                                               "detail": "Change is small."}]))
            tasks.append(messages[-1]["content"])
            if len(tasks) == 1:
                return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                     "after": "42"}),))
            return Reply(text="done")
        return chat

    def test_command_runs_a_template_and_apply_verifies(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            self.assertEqual([item.name for item in agent.commands], ["bump"])
            payload = agent.command("bump", "42")
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(tasks[0], "Set answer to 42.")
            self.assertIn("+answer = 42", agent.patch)
            applied = agent.apply()
            self.assertTrue(applied["applied"])
            self.assertEqual(agent.patch, "")
            with self.assertRaises(ValueError):
                agent.apply()
            self.assertTrue(agent.verify()["ok"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

    def test_fix_stages_a_verified_patch_and_apply_lands_it(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            payload = agent.fix(attempts=1)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["attempts"], 1)
            self.assertIn("+answer = 42", agent.patch)
            applied = agent.apply()
            self.assertTrue(applied["applied"])
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

    def test_apply_blocks_credentials_unless_allowed(self):
        with Agent(self.root, chat=self.chat([]), start_mcp=False) as agent:
            agent.repo.call("replace", {
                "path": "app.py", "before": "41",
                "after": "42\nAWS_KEY = \"AKIAABCDEFGHIJKLMNOP\""})
            with self.assertRaisesRegex(ValueError, "credentials"):
                agent.apply()
            self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
            applied = agent.apply(allow_secrets=True)
            self.assertTrue(applied["applied"])
            self.assertIn("AKIA", (self.root / "app.py").read_text())

    def test_fix_resets_the_token_budget(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            agent.budget.limit = 1000
            agent.budget.spent = 1000
            payload = agent.fix(attempts=1)
            self.assertEqual(payload["status"], "ok")
            self.assertIn("+answer = 42", agent.patch)

    def test_fix_short_circuits_when_the_snapshot_already_passes(self):
        self.git("add", "app.py")
        (self.root / "app.py").write_text("answer = 42\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "passing")

        def never(messages, tools):
            raise AssertionError("the model must not be called")

        with Agent(self.root, chat=never, start_mcp=False) as agent:
            payload = agent.fix(attempts=2)
            self.assertTrue(payload["already_ok"])
            self.assertEqual(payload["attempts"], 0)
            self.assertEqual(agent.patch, "")

    def test_apply_can_be_undone_through_the_record(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            self.assertEqual(agent.command("bump", "42")["status"], "ok")
            agent.apply()
            self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
            undone = agent.undo()
            self.assertTrue(undone["reversed"])
            self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
            with self.assertRaisesRegex(ValueError, "No recorded apply"):
                agent.undo()

    def test_undo_refuses_to_discard_a_staged_proposal(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            agent.command("bump", "42")
            self.assertTrue(agent.patch)
            with self.assertRaisesRegex(ValueError, "staged"):
                agent.undo()
            self.assertTrue(agent.patch)

    def test_undo_refuses_a_moved_head(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            agent.command("bump", "42")
            agent.apply()
            self.git("add", "app.py")
            self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                     "commit", "-qm", "moved")
            with self.assertRaisesRegex(ValueError, "HEAD moved"):
                agent.undo()
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

    def test_workspace_agent_runs_and_lists_files(self):
        replies = iter([
            Reply(calls=(Call("create", {"name": "note.txt", "text": "hi\n"}),)),
            Reply(text="done"),
        ])

        def chat(messages, tools):
            return next(replies)

        with WorkspaceAgent(self.root, chat=chat) as agent:
            payload = agent.run("make a note")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["text"], "done")
        self.assertIn("note.txt", payload["files"])
        self.assertEqual(payload["workspace"], str(self.root))
        self.assertTrue((self.root / "note.txt").exists())
        self.assertEqual(WorkspaceAgent.__module__, "nalar.sdk")

    def test_workspace_agent_follow_up_keeps_the_conversation(self):
        seen = []

        def chat(messages, tools):
            seen.append([dict(message) for message in messages])
            return Reply(text=f"reply-{len(seen)}")

        with WorkspaceAgent(self.root, chat=chat) as agent:
            first = agent.run("first")
            second = agent.follow_up("second")
            self.assertEqual(first["text"], "reply-1")
            self.assertEqual(second["text"], "reply-2")
            self.assertEqual([message["role"] for message in seen[1]],
                             ["system", "user", "assistant", "user"])
            self.assertEqual(seen[1][-1]["content"], "second")
            self.assertEqual(seen[1][2]["content"], "reply-1")
            agent.reset_conversation()
            self.assertEqual(agent.conversation, ())

    def test_workspace_agent_failed_run_does_not_extend_history(self):
        from nalar.core import ProviderError

        calls = []

        def chat(messages, tools):
            calls.append(len(messages))
            if len(calls) == 1:
                raise ProviderError("invalid_response")
            return Reply(text="ok")

        with WorkspaceAgent(self.root, chat=chat) as agent:
            failed = agent.run("first")
            self.assertEqual(failed["status"], "provider_error")
            self.assertEqual(agent.conversation, ())
            second = agent.follow_up("second")
            self.assertEqual(second["text"], "ok")
            self.assertEqual(calls[1], 2)

    def test_run_streams_text_when_the_provider_supports_it(self):
        pieces = []

        class StreamChat:
            def __call__(self, messages, tools):
                raise AssertionError("the streaming path should be used")

            def stream(self, messages, tools, on_text=None):
                on_text("hel")
                on_text("lo")
                return Reply(text="hello")

        with Agent(self.root, chat=StreamChat(), start_mcp=False) as agent:
            payload = agent.run("Greet", on_text=pieces.append)
        self.assertEqual(pieces, ["hel", "lo"])
        self.assertEqual(payload["text"], "hello")

    def test_review_returns_findings_for_the_staged_patch(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            agent.command("bump", "42")
            result = agent.review()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["findings"][0]["file"], "app.py")

    def test_review_can_use_a_separate_review_chat(self):
        tasks = []
        seen = []

        def reviewer(messages, tools):
            seen.append(messages[0]["content"])
            return Reply(text=json.dumps([{
                "file": "app.py", "line": 1, "severity": "low",
                "title": "Note", "detail": "Small change."}]))

        with Agent(self.root, chat=self.chat(tasks), review_chat=reviewer,
                   start_mcp=False) as agent:
            agent.command("bump", "42")
            result = agent.review()
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["findings"][0]["detail"], "Small change.")
        self.assertEqual(result["model"], "fixture-model")

    def test_injected_review_chat_uses_configured_review_model(self):
        data = json.loads((self.root / "nalar.json").read_text())
        data["review_model"] = "fixture-reviewer"
        (self.root / "nalar.json").write_text(json.dumps(data),
                                              encoding="utf-8")

        def reviewer(messages, tools):
            return Reply(text=json.dumps([{
                "file": "app.py", "line": 1, "severity": "low",
                "title": "Note", "detail": "Small change."}]))

        with Agent(self.root, chat=self.chat([]), review_chat=reviewer,
                   start_mcp=False) as agent:
            agent.command("bump", "42")
            result = agent.review()
        self.assertEqual(result["model"], "fixture-reviewer")

    def test_record_writes_a_private_summary_without_patch_contents(self):
        tasks = []
        sessions = self.root / "sessions"
        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with Agent(self.root, chat=self.chat(tasks), start_mcp=False,
                       record=True) as agent:
                payload = agent.run("Record this task")
                self.assertEqual(payload["session"], agent.session.id)
                session_id = payload["session"]
        from nalar.store import SessionStore

        store = SessionStore(sessions)
        loaded = store.load(session_id)
        texts = "\n".join(entry["text"] for entry in loaded.entries)
        self.assertIn("Record this task", texts)
        self.assertNotIn("+answer = 42", texts)
        patch_entry = next(entry for entry in loaded.entries
                           if entry["kind"] == "patch")
        self.assertEqual(len(patch_entry["sha256"]), 64)
        self.assertIn("app.py", patch_entry["files"])

    def test_follow_up_continues_the_conversation(self):
        seen = []

        def chat(messages, tools):
            seen.append([dict(message) for message in messages])
            return Reply(text=f"reply-{len(seen)}")

        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            first = agent.run("first")
            self.assertEqual(first["text"], "reply-1")
            self.assertEqual([message["role"] for message in agent.conversation],
                             ["system", "user", "assistant"])
            second = agent.follow_up("second")
            self.assertEqual(second["text"], "reply-2")
            self.assertEqual([message["role"] for message in seen[1]],
                             ["system", "user", "assistant", "user"])
            self.assertEqual(seen[1][-1]["content"], "second")
            self.assertEqual(seen[1][2]["content"], "reply-1")
            agent.reset_conversation()
            self.assertEqual(agent.conversation, ())

    def test_failed_run_does_not_extend_the_conversation(self):
        from nalar.core import ProviderError

        seen = []

        def chat(messages, tools):
            seen.append(len(messages))
            if len(seen) == 1:
                raise ProviderError("invalid_response")
            return Reply(text="ok")

        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            failed = agent.run("first")
            self.assertEqual(failed["status"], "provider_error")
            self.assertEqual(agent.conversation, ())
            second = agent.follow_up("second")
            self.assertEqual(second["text"], "ok")
            self.assertEqual(seen[1], 2)

    def test_id_less_tool_calls_are_unique_across_follow_up(self):
        replies = iter([
            Reply(calls=(Call("read_lines", {"path": "app.py", "start": 1, "end": 1},
                              id=None),)),
            Reply(text="first"),
            Reply(calls=(Call("read_lines", {"path": "app.py", "start": 1, "end": 1},
                              id=None),)),
            Reply(text="second"),
        ])

        def chat(messages, tools):
            return next(replies)

        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            agent.run("one")
            agent.follow_up("two")
            ids = [call["id"] for message in agent.conversation
                   if message.get("role") == "assistant"
                   for call in message.get("tool_calls") or ()]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_conversation_is_an_immutable_copy(self):
        with Agent(self.root, chat=lambda messages, tools: Reply(text="ok"),
                   start_mcp=False) as agent:
            agent.run("one")
            snapshot = agent.conversation
            snapshot[0]["content"] = "tampered"
            self.assertNotEqual(agent.conversation[0]["content"], "tampered")

    def test_follow_up_does_not_replay_reasoning(self):
        seen = []

        def chat(messages, tools):
            seen.append([dict(message) for message in messages])
            return Reply(text="ok", reasoning="private reasoning")

        with Agent(self.root, chat=chat, start_mcp=False) as agent:
            agent.run("one")
            agent.follow_up("two")
        self.assertTrue(all("reasoning_content" not in message
                            for message in seen[1]))

    def test_reset_drops_the_proposal(self):
        tasks = []
        with Agent(self.root, chat=self.chat(tasks), start_mcp=False) as agent:
            agent.command("bump", "42")
            self.assertIn("+answer = 42", agent.patch)
            agent.reset()
            self.assertEqual(agent.patch, "")

    def test_errors_are_explicit(self):
        with Agent(self.root, chat=self.chat([]), start_mcp=False) as agent:
            with self.assertRaises(ValueError):
                agent.command("missing")
            with self.assertRaises(ValueError):
                agent.command("bump", "x" * 7_000)
            with self.assertRaises(ValueError):
                agent.command("bump", "x" * 5_990)
            with self.assertRaises(ValueError):
                agent.apply()
            with self.assertRaises(ValueError):
                agent.review()

    def test_verify_without_commands_is_refused(self):
        (self.root / "nalar.json").write_text(json.dumps({"model": "fixture-model"}),
                                              encoding="utf-8")
        self.git("add", "nalar.json")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "config")
        with Agent(self.root, chat=self.chat([]), start_mcp=False) as agent:
            with self.assertRaises(ValueError):
                agent.verify()

    def test_workspace_agent_can_record_runs(self):
        sessions = self.root / "workspace-sessions"
        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with WorkspaceAgent(self.root, chat=lambda messages, tools: Reply(text="ok"),
                                record=True) as agent:
                payload = agent.run("Record workspace")
                self.assertEqual(payload["session"], agent.session.id)
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(payload["session"])
        self.assertEqual(loaded.summary()["task"], "Record workspace")
        self.assertEqual(loaded.summary()["tokens"], 0)

    def test_workspace_agent_records_a_resolved_relative_root(self):
        parent = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (parent / "notes").mkdir()
        sessions = parent / "relative-sessions"
        previous = os.getcwd()
        os.chdir(parent)
        try:
            with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
                with WorkspaceAgent("notes",
                                    chat=lambda messages, tools: Reply(text="ok"),
                                    record=True) as agent:
                    payload = agent.run("relative root")
        finally:
            os.chdir(previous)
        from nalar.store import SessionStore

        found = SessionStore(sessions).list((parent / "notes").resolve())
        self.assertEqual([item["id"] for item in found], [payload["session"]])

    def test_review_records_usage_when_recording(self):
        sessions = self.root / "review-sessions"
        tasks = []
        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with Agent(self.root, chat=self.chat(tasks), record=True,
                       start_mcp=False) as agent:
                agent.command("bump", "42")
                result = agent.review()
                session_id = agent.session.id
        self.assertEqual(result["status"], "ok")
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(session_id)
        reviews = [entry for entry in loaded.entries if entry["kind"] == "review"]
        self.assertTrue(reviews)
        self.assertEqual(reviews[-1]["model"], "fixture-model")

    def test_review_provider_error_is_recorded_and_reraised(self):
        from nalar.core import ProviderError

        sessions = self.root / "review-error-sessions"

        def chat(messages, tools):
            if messages[0]["content"].startswith("You review code changes"):
                raise ProviderError("http_500")
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41", "after": "42"}),))
            return Reply(text="done")

        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with Agent(self.root, chat=chat, record=True, start_mcp=False) as agent:
                agent.command("bump", "42")
                with self.assertRaises(ProviderError):
                    agent.review()
                session_id = agent.session.id
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(session_id)
        reviews = [entry for entry in loaded.entries if entry["kind"] == "review"]
        self.assertEqual(reviews[-1]["status"], "provider_error")
        self.assertEqual(reviews[-1]["model"], "fixture-model")

    def test_record_subtracts_subagent_usage(self):
        sessions = self.root / "usage-sessions"
        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with Agent(self.root, chat=self.chat([]), record=True,
                       start_mcp=False) as agent:
                agent.extensions.subagent_usage.append(
                    {"model": "vendor/tiny", "tokens": 7})
                agent._record({"status": "ok", "text": "done", "tokens": 20,
                               "turns": 1, "calls": 0, "patch": ""}, "task")
                session_id = agent.session.id
                self.assertEqual(agent.extensions.subagent_usage, [])
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(session_id)
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((assistant["tokens"], subagent["tokens"],
                          subagent["model"]), ("13", "7", "vendor/tiny"))
        self.assertEqual(loaded.summary()["tokens"], 20)

    def test_review_tolerates_a_transcript_write_failure(self):
        def reviewer(messages, tools):
            return Reply(text=json.dumps([{
                "file": "app.py", "line": 1, "severity": "low",
                "title": "Note", "detail": "Small change."}]))

        class FailingStore:
            def append(self, *args, **kwargs):
                raise OSError("disk full")

        with Agent(self.root, chat=self.chat([]), review_chat=reviewer,
                   start_mcp=False) as agent:
            agent.command("bump", "42")
            agent.store = FailingStore()
            agent.session = object()
            result = agent.review()
        self.assertEqual(result["findings"][0]["detail"], "Small change.")

    def test_record_clears_and_ignores_malformed_usage(self):
        sessions = self.root / "malformed-usage-sessions"
        with patch.dict(os.environ, {"NALAR_SESSIONS": str(sessions)}):
            with Agent(self.root, chat=self.chat([]), record=True,
                       start_mcp=False) as agent:
                usage = agent.extensions.subagent_usage
                usage.extend([
                    {"model": "vendor/tiny", "tokens": -5},
                    {"model": "vendor/tiny", "tokens": "x"},
                    {"model": "bad", "tokens": float("inf")},
                    {"model": "bad", "tokens": 10**30},
                    {"model": "", "tokens": 4},
                    {"model": "  ", "tokens": 3},
                    {"model": "vendor/tiny", "tokens": 7},
                ])
                agent._record({"status": "ok", "text": "done", "tokens": 20,
                               "turns": 1, "calls": 0, "patch": ""}, "task")
                self.assertEqual(usage, [])
                session_id = agent.session.id
        from nalar.store import SessionStore

        loaded = SessionStore(sessions).load(session_id)
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((assistant["tokens"], subagent["tokens"],
                          subagent["model"]), ("13", "7", "vendor/tiny"))
        self.assertEqual(loaded.summary()["tokens"], 20)

    def test_record_disabled_still_clears_delegated_usage(self):
        with Agent(self.root, chat=self.chat([]), start_mcp=False) as agent:
            usage = agent.extensions.subagent_usage
            usage.append({"model": "vendor/tiny", "tokens": 7})
            agent._record({"status": "ok", "text": "done", "tokens": 20}, "task")
            self.assertEqual(usage, [])
