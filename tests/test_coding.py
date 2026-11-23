import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.coding import Repository, solve
from nalar.core import Call, ProviderError, Reply
from nalar.workspace import ToolError


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q")
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("answer = 41")
        (self.root / "README.md").write_text("demo\n")
        (self.root / ".env").write_text("SECRET=tracked-test\n")
        (self.root / "alias").symlink_to("src/app.py")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args, **options):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, **options).stdout

    def test_cli_produces_reviewable_patch_without_touching_dirty_checkout(self):
        (self.root / "src/app.py").write_text("answer = 12")
        (self.root / "untracked.txt").write_text("local")
        out = io.StringIO()

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("search", {"query": "answer", "prefix": ""}),))
            if len(messages) == 4:
                self.assertIn("src/app.py", messages[-1]["content"])
                return Reply(calls=(
                    Call("replace", {"path": "src/app.py", "before": "41", "after": "42"}),
                    Call("create_file", {"path": "tests/check.py", "text": "assert True\n"}),
                ))
            return Reply(text="Fixed.", tokens=12)

        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)
        diff = out.getvalue()
        self.assertIn("No newline at end of file", diff)
        self.assertEqual((self.root / "src/app.py").read_text(), "answer = 12")
        self.assertFalse((self.root / "tests").exists())
        self.git("apply", "--cached", "--check", "-", input=diff.encode())

    def test_new_file_without_final_newline_produces_applicable_patch(self):
        repo = Repository(self.root)
        repo.call("create_file", {"path": "new.txt", "text": "new"})
        self.git("apply", "--check", "-", input=repo.patch().encode())

    def test_cli_writes_newline_delimited_events(self):
        events = self.root / "events.ndjson"
        out = io.StringIO()

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("search", {"query": "answer", "prefix": ""}),))
            return Reply(text="Done.")

        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--events", str(events), "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            main()
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertIn({"type": "tool", "name": "search", "status": "ok"}, lines)
        self.assertEqual(lines[-1]["type"], "final")
        self.assertEqual(events.stat().st_mode & 0o777, 0o600)

    def test_cli_saves_the_patch_for_apply(self):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {"path": "src/app.py", "before": "41",
                                                     "after": "42"}),))
            return Reply(text="done")

        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "saved.patch"
        out, err = io.StringIO(), io.StringIO()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--patch-out", str(target), "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        saved = target.read_text()
        self.assertEqual(saved, out.getvalue())
        self.assertIn("+answer = 42", saved)
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual(metadata["patch_path"], str(target))

        checked = subprocess.run([sys.executable, "-m", "nalar", "apply", "--check",
                                  "--repo", str(self.root), str(target)],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_patch_out_is_validated_before_the_model_runs(self):
        called = []

        def chat(messages, tools):
            called.append(1)
            return Reply(text="done")

        missing = Path(self.enterContext(tempfile.TemporaryDirectory())) / "absent" / "x.patch"
        err = io.StringIO()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--patch-out", str(missing), "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(called, [])
        self.assertIn("directory does not exist", err.getvalue())

    def test_patch_out_refuses_symlinks_and_writes_nothing_without_a_patch(self):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {"path": "src/app.py", "before": "41",
                                                     "after": "42"}),))
            return Reply(text="done")

        def no_change(messages, tools):
            return Reply(text="No change needed.")

        symlink = Path(self.enterContext(tempfile.TemporaryDirectory())) / "link.patch"
        symlink.symlink_to(self.root / "src" / "app.py")
        err = io.StringIO()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--patch-out", str(symlink), "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("symlink", err.getvalue())
        self.assertEqual((self.root / "src" / "app.py").read_text(), "answer = 41")

        missing = Path(self.enterContext(tempfile.TemporaryDirectory())) / "none.patch"
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--patch-out", str(missing), "No change"]
        err = io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=no_change), \
                patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 1)
        self.assertFalse(missing.exists())
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertIsNone(metadata["patch_path"])

    def test_code_shows_tool_progress_on_a_terminal(self):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("list_files", {"prefix": "", "offset": 0}),))
            if len(messages) == 4:
                return Reply(calls=(Call("replace", {"path": "src/app.py", "before": "41",
                                                     "after": "42"}),))
            return Reply(text="done")

        class TTY(io.StringIO):
            def isatty(self):
                return True

        out, err = io.StringIO(), TTY()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("+answer = 42", out.getvalue())
        self.assertIn("· list_files ok", err.getvalue())
        self.assertIn("· replace ok", err.getvalue())

    def test_review_findings_are_printed_on_a_terminal(self):
        def chat(messages, tools):
            if messages[0]["content"].startswith("You review code changes"):
                return Reply(text=json.dumps([{"file": "src/app.py", "line": 1,
                                               "severity": "high", "title": "Bug",
                                               "detail": "Fix it."}]))
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {"path": "src/app.py", "before": "41",
                                                     "after": "42"}),))
            return Reply(text="done")

        class TTY(io.StringIO):
            def isatty(self):
                return True

        out, err = io.StringIO(), TTY()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--review", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("done", err.getvalue())
        self.assertIn("[high] src/app.py:1 Bug", err.getvalue())

    def test_plan_mode_succeeds_without_a_patch(self):
        def chat(messages, tools):
            return Reply(text="Plan: edit src/app.py and add a guard.")

        out, err = io.StringIO(), io.StringIO()
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--plan", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertFalse(metadata["patch_created"])
        self.assertIn("Plan:", metadata["text"])

    def test_plan_mode_refuses_patch_flags(self):
        for flag in (["--verify"], ["--review"], ["--patch-out", str(self.root / "p.diff")]):
            with self.subTest(flag=flag):
                args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                        "--plan", *flag, "Fix answer"]
                with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                        patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)

    def test_cli_retries_are_passed_to_the_loop(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            raise ProviderError("transport_unavailable")

        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--retries", "0", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main()
        self.assertEqual(code, 1)
        self.assertEqual(len(attempts), 1)

    def test_repository_cache_is_bounded(self):
        for index in range(60):
            (self.root / "src" / f"file{index}.py").write_text(f"value = {index}\n")
        self.git("add", "src")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "many")
        repo = Repository(self.root)
        for index in range(60):
            repo.call("read_lines", {"path": f"src/file{index}.py", "start": 1, "end": 1})
        self.assertLessEqual(len(repo._cache), 50)

    def test_cli_limit_overrides_are_enforced(self):
        out, err = io.StringIO(), io.StringIO()

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("search", {"query": "answer", "prefix": ""}),))
            return Reply(text="done")

        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--max-calls", "0", "Fix answer"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.getvalue().strip().splitlines()[-1])["status"],
                         "call_limit")

    def test_cli_rejects_out_of_range_limits(self):
        for flag, value in (("--max-turns", "0"), ("--max-calls", "1000"),
                            ("--token-budget", "-1"), ("--retries", "9")):
            with self.subTest(flag=flag):
                args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                        flag, value, "Task"]
                with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                        patch("sys.argv", args), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                self.assertEqual(caught.exception.code, 2)

    def test_plan_mode_rejects_disabling_extensions(self):
        args = ["nalar", "code", "--repo", str(self.root), "--model", "fixture",
                "--plan", "--no-extensions", "Task"]
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("sys.argv", args), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)

    def test_repository_cache_reflects_staged_changes(self):
        repo = Repository(self.root)
        first = repo.call("read_lines", {"path": "src/app.py", "start": 1, "end": 1})
        self.assertEqual(first["lines"], ["answer = 41"])
        repo.call("replace", {"path": "src/app.py", "before": "41", "after": "42"})
        second = repo.call("read_lines", {"path": "src/app.py", "start": 1, "end": 1})
        self.assertEqual(second["lines"], ["answer = 42"])

    def test_read_lines_suggests_the_next_range(self):
        repo = Repository(self.root)
        first = repo.call("read_lines", {"path": "src/app.py", "start": 1, "end": 1})
        self.assertEqual(first["next_start"], None if first["total_lines"] <= 1 else 2)
        whole = repo.call("read_lines", {"path": "src/app.py", "start": 1,
                                         "end": first["total_lines"]})
        self.assertIsNone(whole["next_start"])

    def test_find_symbol_query_bound(self):
        repo = Repository(self.root)
        self.assertEqual(repo.call("find_symbol", {"query": "x" * 200})["symbols"], [])
        with self.assertRaises(ToolError):
            repo.call("find_symbol", {"query": "x" * 201})

    def test_search_reports_when_results_are_truncated(self):
        (self.root / "src" / "many.py").write_text("hit\n" * 40, encoding="utf-8")
        self.git("add", "src/many.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "many")
        found = Repository(self.root).call("search", {"query": "hit", "prefix": "src/"})
        self.assertTrue(found["truncated"])
        self.assertEqual(len(found["matches"]), 30)
        self.assertIn("Narrow", found["hint"])

    def test_symbol_search_finds_python_and_fallback_definitions(self):
        (self.root / "src/lib.py").write_text(
            "class Parser:\n    def parse(self):\n        pass\n\ndef helper():\n    pass\n")
        (self.root / "src/app.js").write_text("function loadData() {}\n")
        self.git("add", "src/lib.py", "src/app.js")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "symbols")
        repo = Repository(self.root)
        matches = repo.call("find_symbol", {"query": "pars"})["symbols"]
        self.assertEqual({(item["name"], item["kind"]) for item in matches},
                         {("Parser", "class"), ("parse", "function")})
        self.assertEqual(repo.call("find_symbol", {"query": "load"})["symbols"][0]["path"],
                         "src/app.js")
        with self.assertRaises(ToolError):
            repo.call("find_symbol", {"query": ""})

    def test_symbol_index_is_invalidated_by_staged_files(self):
        repo = Repository(self.root)
        self.assertEqual(repo.call("find_symbol", {"query": "Brand"})["symbols"], [])
        repo.call("create_file", {"path": "src/new.py", "text": "class BrandNew:\n    pass\n"})
        self.assertEqual(repo.call("find_symbol", {"query": "Brand"})["symbols"][0]["name"],
                         "BrandNew")
        repo.changed.clear()
        repo.originals.clear()
        repo.invalidate()
        self.assertEqual(repo.call("find_symbol", {"query": "Brand"})["symbols"], [])

    def test_concurrent_reads_share_the_cache_safely(self):
        import threading

        for index in range(40):
            (self.root / "src" / f"c{index}.py").write_text(f"v{index} = {index}\n")
        self.git("add", "src")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "many")
        repo = Repository(self.root)
        errors, values = [], []

        def worker(start):
            try:
                for index in range(start, 40, 8):
                    values.append(repo.call("read_lines",
                                            {"path": f"src/c{index}.py", "start": 1, "end": 1}))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(values), 40)
        self.assertLessEqual(len(repo._cache), 50)
        self.assertLessEqual(repo._cache_bytes, 2_000_000)

    def test_git_show_returns_a_bounded_commit_diff(self):
        repo = Repository(self.root)
        shown = repo.call("git_show", {"sha": repo.commit.decode(), "path": "src/app.py"})
        self.assertIn("commit ", shown["text"])
        self.assertIn("+answer = 41", shown["text"])
        self.assertFalse(shown["truncated"])
        for args in ({"sha": "zzzzzz", "path": ""},
                     {"sha": repo.commit.decode(), "path": "../outside"}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                repo.call("git_show", args)
        (self.root / "big.txt").write_text("x" * 20_000, encoding="utf-8")
        self.git("add", "big.txt")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "big")
        repo = Repository(self.root)
        large = repo.call("git_show", {"sha": repo.commit.decode(), "path": "big.txt"})
        self.assertTrue(large["truncated"])
        self.assertLessEqual(len(large["text"]), 16_000)

    def test_git_log_is_bounded_and_path_filtered(self):
        repo = Repository(self.root)
        commits = repo.call("git_log", {"path": "src/app.py", "limit": 5})["commits"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["subject"], "fixture")
        self.assertEqual(set(commits[0]), {"sha", "author", "date", "subject"})
        self.assertGreaterEqual(len(repo.call("git_log", {"path": "", "limit": 5})["commits"]), 1)
        for args in ({"path": "", "limit": 0}, {"path": "", "limit": 21},
                     {"path": "../outside", "limit": 1}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                repo.call("git_log", args)

    def test_repository_rejects_excluded_paths_and_ambiguous_edits(self):
        repo = Repository(self.root)
        for path in (".env", "alias", "../README.md", "untracked.txt"):
            with self.subTest(path=path), self.assertRaises(ToolError):
                repo.call("read_lines", {"path": path, "start": 1, "end": 1})
        repo.call("create_file", {"path": "src/words.txt", "text": "word word\n"})
        with self.assertRaisesRegex(ToolError, "ambiguous_match"):
            repo.call("replace", {"path": "src/words.txt", "before": "word", "after": "no"})
        self.assertIn("+word word", repo.patch())

    def test_final_claim_without_edits_is_not_a_patch(self):
        result, diff = solve(lambda *_: Reply(text="Fixed."), Repository(self.root), "Fix the app")
        self.assertEqual(result.status, "ok")
        self.assertEqual(diff, "")

    def test_rejected_run_does_not_export_partial_edits(self):
        reply = Reply(calls=(Call("create_file", {"path": "new.txt", "text": "new\n"}),))
        result, diff = solve(lambda *_: reply, Repository(self.root), "Keep calling")
        self.assertNotEqual(result.status, "ok")
        self.assertEqual(diff, "")
