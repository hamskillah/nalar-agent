import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.core import Call, Reply
from nalar.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="],
                       check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.sessions = self.root / "sessions"

    def test_code_record_writes_a_summary_without_patch_contents(self):
        def fake_client(provider, model, fallbacks=()):
            state = {"n": 0}

            def chat(messages, tools):
                state["n"] += 1
                if state["n"] == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),))
                return Reply(text="done")
            return chat

        out, err = StringIO(), StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.root / "none.json"),
               "NALAR_SESSIONS": str(self.sessions)}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--record", "--json",
                                   "Record this run"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        session_id = payload["session"]
        store = SessionStore(self.sessions)
        loaded = store.load(session_id)
        kinds = [entry["kind"] for entry in loaded.entries]
        self.assertIn("user", kinds)
        self.assertIn("assistant", kinds)
        self.assertIn("patch", kinds)
        texts = "\n".join(entry["text"] for entry in loaded.entries)
        self.assertIn("Record this run", texts)
        self.assertNotIn("+answer = 42", texts)
        patch_entry = next(entry for entry in loaded.entries
                           if entry["kind"] == "patch")
        self.assertEqual(len(patch_entry["sha256"]), 64)
        self.assertIn("app.py", patch_entry["files"])
        found = store.search("Record this run", self.root.resolve())
        self.assertEqual([item["id"] for item in found], [session_id])

    def test_record_run_captures_summaries_without_command_output(self):
        store = SessionStore(self.sessions)
        session = store.record_run(
            repo=str(self.root), model="m", base="b", task="task",
            status="failed", text="summary", tokens=3, turns=2, calls=4,
            verification={"ok": False, "error": "bad",
                          "commands": [{"output": "SECRET OUTPUT"}]},
            review={"status": "ok", "findings": [{"title": "x"}],
                    "model": "reviewer", "tokens": 2},
            diff="diff --git a/app.py b/app.py\n+answer = 42\n",
            files=["app.py"])
        loaded = store.load(session.id)
        self.assertEqual([entry["kind"] for entry in loaded.entries],
                         ["user", "assistant", "verify", "review", "patch"])
        texts = "\n".join(entry["text"] for entry in loaded.entries)
        self.assertNotIn("SECRET OUTPUT", texts)
        self.assertNotIn("+answer = 42", texts)
        verification = loaded.entries[2]
        self.assertEqual((verification["text"], verification["error"]),
                         ("failed", "bad"))
        self.assertEqual(loaded.summary()["tokens"], 5)
        self.assertEqual(loaded.summary()["by_model"],
                         {"m": 3, "reviewer": 2})

    def test_record_run_attributes_subagent_usage(self):
        store = SessionStore(self.sessions)
        session = store.record_run(
            repo=str(self.root), model="parent", base="b", task="task",
            status="ok", text="done", tokens=6,
            usage=[{"model": "vendor/tiny", "tokens": 4},
                   {"model": "vendor/tiny", "tokens": "3"},
                   {"model": "", "tokens": 5},
                   {"model": "bad", "tokens": -2},
                   {"model": "bad", "tokens": "x"}, "broken"])
        summary = store.load(session.id).summary()
        self.assertEqual(summary["tokens"], 13)
        self.assertEqual(summary["by_model"],
                         {"parent": 6, "vendor/tiny": 7})

    def test_ask_record_attributes_subagent_usage(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "primary", "subagent_model": "vendor/tiny"}),
            encoding="utf-8")

        def fake_client(provider, model, fallbacks=()):
            def chat(messages, tools):
                if messages[0]["content"].startswith("You are a read-only"):
                    return Reply(text="sub", tokens=7)
                if not any(message.get("role") == "tool" for message in messages):
                    return Reply(calls=(Call("delegate", {"task": "explore"}),))
                return Reply(text="done", tokens=20)
            return chat

        out, err = StringIO(), StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.root / "none.json"),
               "NALAR_SESSIONS": str(self.sessions)}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "ask", "--repo", str(self.root),
                                   "--model", "primary", "--record", "--json",
                                   "Investigate"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        loaded = SessionStore(self.sessions).load(payload["session"])
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual(subagent["model"], "vendor/tiny")
        self.assertEqual(int(subagent["tokens"]), 7)
        self.assertEqual(int(assistant["tokens"]) + 7, payload["tokens"])

    def test_code_record_attributes_subagent_usage(self):
        (self.root / "nalar.json").write_text(json.dumps({
            "model": "primary", "subagent_model": "vendor/tiny"}),
            encoding="utf-8")

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
                return Reply(text="done")
            return chat

        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "code.patch"
        out, err = StringIO(), StringIO()
        env = {"NALAR_API_KEY": "fixture-key",
               "NALAR_GLOBAL_CONFIG": str(self.root / "none.json"),
               "NALAR_SESSIONS": str(self.sessions)}
        with patch.dict(os.environ, env), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--record", "--json",
                                   "--patch-out", str(target), "Investigate"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertIn("+answer = 42", target.read_text())
        loaded = SessionStore(self.sessions).load(payload["session"])
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((int(subagent["tokens"]), subagent["model"]),
                         (7, "vendor/tiny"))
        self.assertEqual(int(assistant["tokens"]) + 7, payload["tokens"])

    def test_usage_totals_ignores_boolean_tokens(self):
        from nalar.usage import usage_totals

        self.assertEqual(usage_totals([
            {"model": "m", "tokens": True},
            {"model": "m", "tokens": 3},
        ]), {"m": 3})
