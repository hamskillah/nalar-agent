import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.core import Reply
from nalar.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]


class ContinueTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        (self.repo / "app.py").write_text("answer = 41\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "fixture"], check=True)
        self.sessions = self.repo / "sessions"
        self.store = SessionStore(self.sessions)
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                              check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout.strip()
        self.session = self.store.create(self.repo, "fixture", head)
        self.store.append(self.session, "user", "old task about the parser")

    def chat(self, *args, **kwargs):
        seen = []

        def chat(messages, tools):
            seen.append(messages)
            return Reply(text="ok")

        chat.seen = seen
        return chat

    def run_main(self, argv, chat, stdin="new task\n/quit\n"):
        out, err = io.StringIO(), io.StringIO()
        env = {"NALAR_API_KEY": "fixture-key", "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        with patch.dict(os.environ, env), patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", *argv]), patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_continue_reuses_the_latest_transcript(self):
        chat = self.chat()
        code, _, err = self.run_main(["chat", "--repo", str(self.repo), "--model", "fixture",
                                      "--continue"], chat)
        self.assertEqual(code, 0, err)
        first = chat.seen[0][1]["content"]
        self.assertIn("Previous user revisions", first)
        self.assertIn("old task about the parser", first)

    def test_continue_starts_fresh_when_no_session_matches_the_commit(self):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "--allow-empty", "-qm",
                        "later"], check=True)
        code, _, err = self.run_main(["chat", "--repo", str(self.repo), "--model", "fixture",
                                      "--continue"], self.chat())
        self.assertEqual(code, 0, err)
        self.assertIn("starting a new one", err)
        self.assertGreaterEqual(len(self.store.list(self.repo)), 2)

    def test_continue_without_a_session_starts_fresh(self):
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(empty), "init", "-q", "--template="], check=True)
        subprocess.run(["git", "-C", str(empty), "-c", "user.name=F", "-c", "user.email=f@example.test",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        code, _, err = self.run_main(["chat", "--repo", str(empty), "--model", "fixture",
                                      "--continue"], self.chat())
        self.assertEqual(code, 0, err)
        self.assertIn("starting a new one", err)

    def test_resume_and_continue_together_are_rejected(self):
        code, _, err = self.run_main(["chat", "--repo", str(self.repo), "--model", "fixture",
                                      "--resume", self.session.id, "--continue"], self.chat())
        self.assertEqual(code, 2)
        self.assertIn("not both", err)

    def test_model_switch_updates_the_saved_session(self):
        code, _, err = self.run_main(
            ["chat", "--repo", str(self.repo), "--model", "fixture",
             "--resume", self.session.id], self.chat(), stdin="/model other-model\n/quit\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.load(self.session.id).model, "other-model")

    def test_sessions_prune_requires_confirmation_or_yes(self):
        second = self.store.create(self.repo, "fixture", self.session.base)
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        refused = subprocess.run([sys.executable, "-m", "nalar", "sessions", "prune",
                                  "--keep", "1", "--repo", str(self.repo)],
                                 cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(refused.returncode, 2)
        pruned = subprocess.run([sys.executable, "-m", "nalar", "sessions", "prune",
                                 "--keep", "1", "--yes", "--repo", str(self.repo)],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(pruned.returncode, 0, pruned.stderr)
        self.assertEqual(len(json.loads(pruned.stdout)["deleted"]), 1)
        remaining = self.store.list(self.repo)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], second.id)

    def test_chat_fork_branches_the_transcript(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        original = self.store.load(self.session.id)
        original_entries = list(original.entries)
        output = io.StringIO()

        def set_model(value, active):
            active.model = value
            self.store.save(active)

        interactive(Repository(self.repo),
                    lambda: (lambda messages, tools: Reply(text="ok")),
                    io.StringIO("/fork\nfollow-up\n/model other-model\n/quit\n"),
                    output, store=self.store, session=self.session,
                    model="fixture", set_model=set_model)
        self.assertIn("Forked session", output.getvalue())
        listed = self.store.list(self.repo)
        self.assertEqual(len(listed), 2)
        parent = self.store.load(self.session.id)
        self.assertEqual(parent.entries, original_entries)
        self.assertEqual(parent.model, "fixture")
        fork = next(item for item in listed if item["id"] != self.session.id)
        forked = self.store.load(fork["id"])
        self.assertEqual(forked.entries[:len(original_entries)], original_entries)
        self.assertTrue(any(entry.get("text") == "follow-up"
                            for entry in forked.entries))
        self.assertEqual(forked.model, "other-model")

    def test_chat_fork_failure_is_readable_and_keeps_the_session(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        output = io.StringIO()
        with patch.object(self.store, "fork",
                          side_effect=ValueError("session_not_found")):
            interactive(Repository(self.repo),
                        lambda: (lambda messages, tools: Reply(text="ok")),
                        io.StringIO("/fork\n/quit\n"), output,
                        store=self.store, session=self.session)
        self.assertIn("could not be found", output.getvalue())
        self.assertEqual(len(self.store.list(self.repo)), 1)

    def test_sessions_stats_aggregate_the_store(self):
        self.store.append(self.session, "assistant", "done", tokens=5)
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "stats",
                                 "--repo", str(self.repo)],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["sessions"], payload["tokens"]), (1, 5))

    def test_sessions_list_and_stats_text_output(self):
        self.store.append(self.session, "assistant", "done", tokens=5)
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        listed = subprocess.run([sys.executable, "-m", "nalar", "sessions", "list",
                                 "--repo", str(self.repo), "--format", "text"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(self.session.id, listed.stdout)
        self.assertFalse(listed.stdout.lstrip().startswith("["))
        stats = subprocess.run([sys.executable, "-m", "nalar", "sessions", "stats",
                                "--repo", str(self.repo), "--format", "text"],
                               cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(stats.returncode, 0, stats.stderr)
        self.assertIn("1 session ·", stats.stdout)
        self.assertIn("models:", stats.stdout)
        self.assertIn("tokens by model: fixture: 5", stats.stdout)

    def test_sessions_text_output_strips_control_characters(self):
        session = self.store.create(self.repo, "fixture", "base")
        self.store.append(session, "user", "\x1b[2Jspoof")
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "list",
                                 "--repo", str(self.repo), "--format", "text"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("spoof", result.stdout)

    def test_sessions_text_format_is_limited_to_list_and_stats(self):
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "delete",
                                 self.session.id, "--yes", "--repo", str(self.repo),
                                 "--format", "text"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("fork, show, export and search", result.stderr)

    def test_sessions_show_text_format(self):
        self.store.append(self.session, "assistant", "done", status="ok")
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "show",
                                 self.session.id, "--repo", str(self.repo),
                                 "--format", "text"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Session {self.session.id}", result.stdout)
        self.assertIn("USER", result.stdout)
        self.assertIn("old task about the parser", result.stdout)
        self.assertIn("NALAR (ok)", result.stdout)
        self.assertIn("done", result.stdout)

    def test_sessions_fork_copies_the_transcript(self):
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        text = subprocess.run([sys.executable, "-m", "nalar", "sessions", "fork",
                               self.session.id, "--repo", str(self.repo),
                               "--format", "text"], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn(f"forked from {self.session.id}", text.stdout)
        encoded = subprocess.run([sys.executable, "-m", "nalar", "sessions", "fork",
                                  self.session.id, "--repo", str(self.repo)],
                                 cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(encoded.returncode, 0, encoded.stderr)
        forked = json.loads(encoded.stdout)
        self.assertNotEqual(forked["forked"], self.session.id)
        self.assertEqual(forked["from"], self.session.id)
        listed = subprocess.run([sys.executable, "-m", "nalar", "sessions", "list",
                                 "--repo", str(self.repo)], cwd=ROOT, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(len(json.loads(listed.stdout)), 3)

    def test_sessions_export_prints_markdown(self):
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "export", self.session.id],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"# Nalar session {self.session.id}", result.stdout)
        self.assertIn("old task about the parser", result.stdout)

    def test_sessions_export_strips_control_characters(self):
        self.store.append(self.session, "assistant", "ok\x1b[2J\u202e", status="ok")
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "export",
                                 self.session.id, "--repo", str(self.repo)],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\u202e", result.stdout)
        self.assertIn("ok", result.stdout)

    def test_sessions_export_does_not_invent_a_status(self):
        self.store.append(self.session, "assistant", "plain")
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "export",
                                 self.session.id, "--repo", str(self.repo)],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Nalar\n", result.stdout)
        self.assertNotIn("## Nalar (ok)", result.stdout)

    def test_sessions_show_prints_the_transcript(self):
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "sessions", "show", self.session.id],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"][0]["text"], "old task about the parser")

    def test_sessions_list_limit_is_validated_and_applied(self):
        self.store.create(self.repo, "second-model", "other-base")
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        command = [sys.executable, "-m", "nalar", "sessions", "list",
                   "--repo", str(self.repo)]
        bad = subprocess.run(command + ["--limit", "0"], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--limit", bad.stderr)
        one = subprocess.run(command + ["--limit", "1"], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertEqual(len(json.loads(one.stdout)), 1)

    def test_interactive_model_holder_tracks_the_switch(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        holder = {"value": "model-a"}
        output = io.StringIO()
        interactive(Repository(self.repo),
                    lambda: (lambda messages, tools: Reply(text="ok")),
                    io.StringIO("/model model-b\n/quit\n"), output,
                    model="model-a", model_holder=holder)
        self.assertEqual(holder["value"], "model-b")

    def test_sessions_model_flag_is_limited_to_list_and_search(self):
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "stats",
             "--repo", str(self.repo), "--model", "fixture"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--model applies", result.stderr)
        custom = self.store.create(self.repo, "custom model!", "base")
        accepted = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "list",
             "--repo", str(self.repo), "--model", "custom model!"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn(custom.id, accepted.stdout)
        for value in ("", " ", "\t", "\x01", "a" * 201):
            invalid = subprocess.run(
                [sys.executable, "-m", "nalar", "sessions", "list",
                 "--repo", str(self.repo), "--model", value],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("printable label", invalid.stderr)

    def test_chat_passes_the_model_holder_to_interactive(self):
        captured = {}

        def fake(repo, chat_factory, stdin, stdout, **kwargs):
            captured.update(kwargs)
            return 0

        env = {**os.environ, "NALAR_API_KEY": "k",
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        with patch.dict(os.environ, env), \
                patch("nalar.session.interactive", side_effect=fake), \
                patch("nalar.__main__._client",
                      return_value=lambda messages, tools: Reply(text="ok")), \
                patch("sys.argv", ["nalar", "chat", "--repo", str(self.repo),
                                   "--model", "fixture", "--no-sessions"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 0)
        self.assertEqual(captured["model_holder"], {"value": "fixture"})

    def test_unparsed_review_usage_is_recorded(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        repo = Repository(self.repo)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        output = io.StringIO()
        interactive(repo, lambda: (lambda messages, tools: Reply(
            text="prose", tokens=3)),
            io.StringIO("/review\n/quit\n"), output,
            store=self.store, session=self.session)
        loaded = self.store.load(self.session.id)
        reviews = [entry for entry in loaded.entries if entry["kind"] == "review"]
        self.assertTrue(reviews)
        self.assertEqual((reviews[-1]["status"], reviews[-1]["tokens"]),
                         ("unparsed", "3"))

    def test_chat_review_uses_the_configured_factory_and_model(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        repo = Repository(self.repo)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        built = []

        def factory():
            built.append(True)

            def chat(messages, tools):
                return Reply(text=json.dumps([{
                    "file": "app.py", "line": 1, "severity": "low",
                    "title": "Note", "detail": "Small."}]), tokens=4)
            return chat

        interactive(repo, lambda: (lambda messages, tools: Reply(text="ok")),
                    io.StringIO("/review\n/quit\n"), io.StringIO(),
                    store=self.store, session=self.session,
                    review_factory=factory, review_model="vendor/reviewer")
        self.assertEqual(built, [True])
        loaded = self.store.load(self.session.id)
        review = [entry for entry in loaded.entries if entry["kind"] == "review"][-1]
        self.assertEqual((review["model"], review["tokens"]),
                         ("vendor/reviewer", "4"))

    def test_failed_review_usage_is_recorded(self):
        from nalar.coding import Repository
        from nalar.core import ProviderError
        from nalar.session import interactive

        repo = Repository(self.repo)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})

        def chat(messages, tools):
            raise ProviderError("http_500")

        interactive(repo, lambda: chat, io.StringIO("/review\n/quit\n"),
                    io.StringIO(), store=self.store, session=self.session)
        loaded = self.store.load(self.session.id)
        reviews = [entry for entry in loaded.entries if entry["kind"] == "review"]
        self.assertTrue(reviews)
        self.assertEqual((reviews[-1]["status"], reviews[-1]["tokens"]),
                         ("provider_error", "0"))

    def test_interactive_attributes_subagent_usage(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        from nalar.agent import Budget

        repo = Repository(self.repo)
        usage = [{"model": "vendor/tiny", "tokens": 7}]
        interactive(repo, lambda: (lambda messages, tools: Reply(
            text="done", tokens=20)),
            io.StringIO("do a task\n/quit\n"), io.StringIO(),
            store=self.store, session=self.session, subagent_usage=usage,
            budget=Budget(0))
        loaded = self.store.load(self.session.id)
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((assistant["tokens"], subagent["tokens"],
                          subagent["model"]), ("13", "7", "vendor/tiny"))
        self.assertEqual(usage, [])

    def test_interactive_without_budget_does_not_subtract_delegated(self):
        from nalar.coding import Repository
        from nalar.session import interactive

        repo = Repository(self.repo)
        usage = [{"model": "vendor/tiny", "tokens": 5}]
        interactive(repo, lambda: (lambda messages, tools: Reply(
            text="done", tokens=20)),
            io.StringIO("do a task\n/quit\n"), io.StringIO(),
            store=self.store, session=self.session, subagent_usage=usage)
        loaded = self.store.load(self.session.id)
        assistant = [entry for entry in loaded.entries
                     if entry["kind"] == "assistant"][-1]
        self.assertEqual((assistant["tokens"], loaded.summary()["tokens"]),
                         ("20", 25))
        self.assertEqual(usage, [])

    def test_lone_surrogates_are_escaped_for_text_output(self):
        from nalar.__main__ import _clean

        self.assertEqual(_clean("bad \ud800 name"), "bad \\ud800 name")

    def test_sessions_show_and_export_json_survive_deep_metadata(self):
        path = self.sessions / f"{self.session.id}.json"
        payload = json.loads(path.read_text())
        deep = 0
        for _ in range(3000):
            deep = [deep]
        payload["entries"] = [{"kind": "user", "text": "hi", "deep": deep}]
        path.write_text(json.dumps(payload))
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        for action in ("show", "export"):
            result = subprocess.run(
                [sys.executable, "-m", "nalar", "sessions", action,
                 self.session.id, "--format", "json"],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertNotIn("error", payload)
            entries = payload.get("entries", [])
            self.assertEqual(entries[0]["text"], "hi")
            self.assertEqual(entries[0]["kind"], "user")
            self.assertNotIn("deep", entries[0])

    def test_terminal_escapes_lone_surrogates(self):
        from nalar.session import _terminal

        self.assertEqual(_terminal("bad \ud800 name"), "bad \\ud800 name")

    def test_emit_falls_back_on_deep_values(self):
        from nalar.__main__ import _emit

        deep = 0
        for _ in range(20_000):
            deep = [deep]
        out = io.StringIO()
        with redirect_stdout(out):
            _emit({"deep": deep})
        self.assertEqual(json.loads(out.getvalue()),
                         {"error": "response_too_complex"})

    def test_non_finite_floats_are_dropped_on_load(self):
        session = self.store.create(self.repo, "fixture", "base")
        path = self.sessions / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"] = [{
            "kind": "user", "text": "hi",
            "score": float("nan"), "other": float("inf"),
        }]
        path.write_text(json.dumps(payload))
        loaded = self.store.load(session.id)
        self.assertEqual(loaded.entries[0]["text"], "hi")
        self.assertNotIn("score", loaded.entries[0])
        self.assertNotIn("other", loaded.entries[0])
        json.dumps(loaded.entries, allow_nan=False)

    def test_text_output_survives_ascii_stdout(self):
        self.session.model = "qwen-🌀"
        self.store.save(self.session)
        self.store.append(self.session, "user", "ok 🌀")
        env = {**os.environ, "PYTHONIOENCODING": "ascii",
               "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        for argv in (
            ["sessions", "show", self.session.id, "--format", "text"],
            ["sessions", "list", "--repo", str(self.repo), "--format", "text"],
            ["sessions", "stats", "--repo", str(self.repo), "--format", "text"],
            ["--help"],
        ):
            result = subprocess.run(
                [sys.executable, "-m", "nalar", *argv],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_terminal_strips_bidi_controls(self):
        from nalar.session import _terminal

        self.assertEqual(_terminal("a\u202eb"), "ab")
        self.assertEqual(_terminal("a\u200bb"), "ab")

    def test_clean_strips_zero_width_space(self):
        from nalar.__main__ import _clean

        self.assertEqual(_clean("a\u200bb"), "ab")

    def test_session_text_output_escapes_embedded_newlines(self):
        self.session.model = "evil\nmodels: forged"
        self.store.save(self.session)
        env = {**os.environ, "NALAR_SESSIONS": str(self.sessions),
               "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "sessions", "list",
             "--repo", str(self.repo), "--format", "text"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("evil\\nmodels: forged", result.stdout)
        self.assertNotIn("\nmodels: forged\n", "\n" + result.stdout)
