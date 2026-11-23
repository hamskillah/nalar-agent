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
from nalar.review import ReviewError, collect_diff, review, sarif


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args, env=None):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, env=env).stdout

    def findings(self, severity="high"):
        return json.dumps([{"file": "app.py", "line": 1, "severity": severity,
                            "title": "Wrong answer", "detail": "Return 42."}])

    def test_untracked_files_are_opt_in_and_skip_secrets(self):
        (self.root / "app.py").write_text("answer = 0\n")
        (self.root / "new.py").write_text("value = 1\n")
        (self.root / ".env").write_text("SECRET=hidden\n")
        _, tracked = collect_diff(self.root)
        self.assertNotIn("new.py", tracked)
        _, with_new = collect_diff(self.root, include_untracked=True)
        self.assertIn("+value = 1", with_new)
        self.assertIn("new.py", with_new)
        self.assertNotIn("SECRET", with_new)

    def test_untracked_only_changes_are_available_on_request(self):
        (self.root / "new.py").write_text("value = 1\n")
        with self.assertRaises(ReviewError):
            collect_diff(self.root)
        scope, diff = collect_diff(self.root, include_untracked=True)
        self.assertEqual(scope, "worktree")
        self.assertIn("+value = 1", diff)

    def test_untracked_hardlinks_are_skipped(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret.txt"
        outside.write_text("PRIVATE-KEY-MATERIAL\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("value = 1\n", encoding="utf-8")
        os.link(outside, self.root / "linked.txt")
        _, diff = collect_diff(self.root, include_untracked=True)
        self.assertIn("+value = 1", diff)
        self.assertNotIn("PRIVATE-KEY-MATERIAL", diff)

    def test_collect_diff_and_parse_fenced_json(self):
        (self.root / "app.py").write_text("answer = 0\n")
        scope, diff = collect_diff(self.root)
        self.assertEqual(scope, "worktree")
        self.assertIn("-answer = 41", diff)
        reply = f"Here you go:\n```json\n{self.findings()}\n```"
        result = review(lambda messages, tools: type("R", (), {"text": reply, "tokens": 12, "calls": ()})(),
                        diff, model="fixture")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["findings"][0]["severity"], "high")
        self.assertEqual(result["tokens"], 12)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 0\n")

    def test_unparseable_output_is_reported_not_dropped(self):
        result = review(lambda *_: type("R", (), {"text": "looks fine", "tokens": None, "calls": ()})(),
                        "diff --git a/x b/x\n", model="fixture")
        self.assertEqual((result["status"], result["findings"]), ("unparsed", []))
        self.assertIn("looks fine", result["text"])

    def test_duplicate_findings_are_collapsed(self):
        finding = {"file": "a.py", "line": 1, "severity": "high",
                   "title": "Bug", "detail": "Fix."}
        payload = json.dumps([finding, finding,
                              {**finding, "title": "Other"}])
        result = review(lambda *_: type("R", (), {"text": payload, "tokens": None,
                                                  "calls": ()})(),
                        "diff --git a/x b/x\n", model="fixture")
        self.assertEqual([item["title"] for item in result["findings"]],
                         ["Bug", "Other"])

    def test_malformed_findings_make_the_result_unparsed(self):
        for payload in ('[{"file":"a","line":1,"severity":"critical","title":"t","detail":"d"}]',
                        '[{"file":"a","line":"one","severity":"high","title":"t","detail":"d"}]',
                        '[{"file":"a","line":1,"severity":"high","detail":"d"}]'):
            with self.subTest(payload=payload):
                result = review(lambda *_: type("R", (), {"text": payload, "tokens": None,
                                                          "calls": ()})(),
                                "diff --git a/x b/x\n", model="fixture")
                self.assertEqual((result["status"], result["findings"]), ("unparsed", []))

    def test_textconv_and_external_diff_are_not_executed(self):
        marker = self.root / "executed"
        (self.root / ".gitattributes").write_text("*.py diff=evil\n", encoding="utf-8")
        self.git("config", "diff.evil.textconv", "sh -c 'touch executed; cat'")
        (self.root / "app.py").write_text("answer = 0\n")
        scope, diff = collect_diff(self.root)
        self.assertIn("answer = 0", diff)
        self.assertFalse(marker.exists())

    def test_retry_after_and_review_budget_are_used(self):
        from nalar.agent import Budget
        from nalar.core import ProviderError

        attempts = []
        sleeps = []

        def chat(messages, tools):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("http_429", retry_after=2)
            return type("R", (), {"text": self.findings(), "tokens": 5, "calls": ()})()

        budget = Budget(10)
        result = review(chat, "diff --git a/x b/x\n", model="fixture",
                        sleep=sleeps.append, budget=budget)
        self.assertEqual((result["status"], sleeps, budget.spent), ("ok", [2], 5))

    def test_transient_provider_errors_are_retried(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            if len(attempts) == 1:
                from nalar.core import ProviderError
                raise ProviderError("transport_unavailable")
            return type("R", (), {"text": self.findings(), "tokens": None, "calls": ()})()

        result = review(chat, "diff --git a/x b/x\n", model="fixture", sleep=lambda _: None)
        self.assertEqual((result["status"], len(attempts)), ("ok", 2))

    def test_empty_diff_is_rejected(self):
        with self.assertRaises(ReviewError):
            collect_diff(self.root)

    def run_cli(self, *args, chat):
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "review", "--repo", str(self.root), *args]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_returns_structured_findings_and_fail_on(self):
        (self.root / "app.py").write_text("answer = 0\n")

        def chat(messages, tools):
            assert "answer = 0" in messages[-1]["content"]
            assert tools == []
            return type("R", (), {"text": self.findings(), "tokens": 9, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", chat=chat)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["status"], len(payload["findings"])), ("ok", 1))

        code, _, _ = self.run_cli("--model", "fixture", "--fail-on", "high", chat=chat)
        self.assertEqual(code, 1)

    def test_cli_markdown_escapes_findings(self):
        (self.root / "app.py").write_text("answer = 0\n")
        findings = json.dumps([{
            "file": "app.py", "line": 1, "severity": "high",
            "title": "Wrong <b>answer</b>", "detail": "Return 42 | keep going."}])

        def chat(messages, tools):
            return type("R", (), {"text": findings, "tokens": 2, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--format", "markdown",
                                    chat=chat)
        self.assertEqual(code, 0)
        self.assertIn("## Nalar review", out)
        self.assertIn("| high | app.py:1 |", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("Return 42 &#124; keep going.", out)
        self.assertNotIn("<b>", out)

    def test_cli_uses_a_separate_review_model(self):
        (self.root / "app.py").write_text("answer = 0\n")
        seen = []

        def fake_client(provider, model, fallbacks=()):
            seen.append(model)

            def chat(messages, tools):
                return type("R", (), {"text": self.findings(), "tokens": 3,
                                      "calls": ()})()
            return chat

        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "review", "--repo", str(self.root),
                                   "--model", "primary",
                                   "--review-model", "vendor/reviewer"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(seen, ["primary", "vendor/reviewer"])
        self.assertEqual(json.loads(out.getvalue())["model"], "vendor/reviewer")

    def test_cli_rejects_an_invalid_review_model(self):
        (self.root / "app.py").write_text("answer = 0\n")
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "review", "--repo", str(self.root),
             "--model", "fixture", "--review-model", "bad model!"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "NALAR_API_KEY": "fixture-key",
                 "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("review-model", result.stderr)

    def test_code_review_model_requires_review(self):
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "code", "--repo", str(self.root),
             "--model", "fixture", "--review-model", "vendor/reviewer", "task"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "NALAR_API_KEY": "fixture-key",
                 "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--review-model requires --review", result.stderr)

    def test_cli_fails_closed_on_unparsed_output_when_fail_on_is_set(self):
        (self.root / "app.py").write_text("answer = 0\n")
        code, _, _ = self.run_cli("--model", "fixture", "--fail-on", "high",
                                  chat=lambda *_: type("R", (), {"text": "no json",
                                                                 "tokens": None, "calls": ()})())
        self.assertEqual(code, 2)

    def test_branch_scope_requires_a_base_ref(self):
        code, _, err = self.run_cli("--model", "fixture", "--scope", "branch",
                                    chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("--ref", err)

    def test_cli_include_untracked_is_worktree_only(self):
        code, _, err = self.run_cli("--model", "fixture", "--include-untracked",
                                    "--scope", "staged", chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("worktree", err)
        patch = Path(self.enterContext(tempfile.TemporaryDirectory())) / "saved.patch"
        patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
        code, _, err = self.run_cli("--model", "fixture", "--include-untracked",
                                    "--file", str(patch), chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("worktree", err)

    def test_cli_includes_untracked_text_files_on_request(self):
        (self.root / "new.py").write_text("value = 1\n")
        seen = {}

        def chat(messages, tools):
            seen["diff"] = messages[-1]["content"]
            return type("R", (), {"text": "[]", "tokens": None, "calls": ()})()

        code, _, _ = self.run_cli("--model", "fixture", "--include-untracked", chat=chat)
        self.assertEqual(code, 0)
        self.assertIn("+value = 1", seen["diff"])

    def test_review_file_reads_a_saved_patch(self):
        (self.root / "app.py").write_text("answer = 0\n")
        patch = Path(self.enterContext(tempfile.TemporaryDirectory())) / "saved.patch"
        patch.write_text("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                         "@@ -1 +1 @@\n-answer = 41\n+answer = 0\n", encoding="utf-8")

        def chat(messages, tools):
            assert "answer = 0" in messages[-1]["content"]
            return type("R", (), {"text": self.findings(), "tokens": None, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--file", str(patch), chat=chat)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["scope"], payload["findings"][0]["severity"]),
                         ("file", "high"))

    def test_review_file_respects_the_diff_bound(self):
        patch = Path(self.enterContext(tempfile.TemporaryDirectory())) / "huge.patch"
        patch.write_text("diff --git a/x b/x\n" + "+x\n" * 100_001, encoding="utf-8")
        code, _, err = self.run_cli("--model", "fixture", "--file", str(patch),
                                    chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("too large", err)

    def test_review_file_rejects_symlinks_and_bad_combinations(self):
        patch_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        patch = patch_dir / "real.patch"
        patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
        link = patch_dir / "link.patch"
        link.symlink_to(patch)
        code, _, err = self.run_cli("--model", "fixture", "--file", str(link),
                                    chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        code, _, err = self.run_cli("--model", "fixture", "--file", str(patch),
                                    "--scope", "staged", chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", err)

    def test_sarif_maps_findings_and_line_fallbacks(self):
        report = sarif({"status": "ok", "findings": [
            {"file": "app.py", "line": 3, "severity": "high",
             "title": "Bug", "detail": "Fix it."},
            {"file": "b.py", "line": 0, "severity": "low",
             "title": "Note", "detail": ""},
        ]}, model="m", scope="worktree")
        self.assertEqual(report["version"], "2.1.0")
        run = report["runs"][0]
        self.assertTrue(run["invocations"][0]["executionSuccessful"])
        self.assertEqual([item["level"] for item in run["results"]],
                         ["error", "note"])
        self.assertEqual(run["results"][0]["ruleId"], "nalar/high")
        self.assertEqual(run["results"][1]["locations"][0]["physicalLocation"]
                         ["region"]["startLine"], 1)
        self.assertEqual({item["id"] for item in run["tool"]["driver"]["rules"]},
                         {"nalar/high", "nalar/low"})

    def test_sarif_marks_an_unparsed_review(self):
        report = sarif({"status": "unparsed", "findings": []})
        invocation = report["runs"][0]["invocations"][0]
        self.assertFalse(invocation["executionSuccessful"])
        self.assertEqual(invocation["toolExecutionNotifications"][0]["level"],
                         "error")

    def test_cli_sarif_format(self):
        (self.root / "app.py").write_text("answer = 0\n")

        def chat(messages, tools):
            return type("R", (), {"text": self.findings(), "tokens": None,
                                  "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--format", "sarif",
                                    chat=chat)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["version"], "2.1.0")
        result = payload["runs"][0]["results"][0]
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                         "app.py")

    def test_cli_text_format_is_human_readable(self):
        (self.root / "app.py").write_text("answer = 0\n")

        def chat(messages, tools):
            return type("R", (), {"text": self.findings(), "tokens": None, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--format", "text", chat=chat)
        self.assertEqual(code, 0)
        self.assertIn("[high] app.py:1 Wrong answer", out)
        self.assertIn("Review · worktree · fixture · ok", out)

    def test_text_findings_are_sanitized(self):
        (self.root / "app.py").write_text("answer = 0\n")

        def chat(messages, tools):
            payload = json.dumps([{
                "file": "app.py\x1b[2J", "line": 1, "severity": "high",
                "title": "Wrong\x1b[31m answer", "detail": "Fix\x1b[0m it."}])
            return type("R", (), {"text": payload, "tokens": None, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--format", "text", chat=chat)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("Wrong", out)

    def test_cli_rejects_empty_diff(self):
        code, _, err = self.run_cli("--model", "fixture", chat=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("no changes", err)

    def test_code_review_uses_the_configured_reviewer(self):
        seen = []

        def fake_client(provider, model, fallbacks=()):
            seen.append(model)
            if model == "vendor/reviewer":
                return lambda messages, tools: Reply(
                    text=self.findings(), tokens=2, calls=())
            state = {"n": 0}

            def primary(messages, tools):
                state["n"] += 1
                if state["n"] == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),),
                                 tokens=3)
                return Reply(text="done", tokens=4)
            return primary

        sessions = self.root / "recorded"
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json"),
                                     "NALAR_SESSIONS": str(sessions)}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--review",
                                   "--review-model", "vendor/reviewer",
                                   "--record", "Bump the answer"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(seen, ["primary", "vendor/reviewer"])
        self.assertIn("+answer = 42", out.getvalue())
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual(metadata["review_model"], "vendor/reviewer")
        self.assertEqual(metadata["review"]["findings"][0]["title"],
                         "Wrong answer")
        self.assertEqual(metadata["review"]["tokens"], 2)
        self.assertEqual(metadata["tokens"], 9)
        self.assertGreaterEqual(metadata["turns"], 2)
        self.assertGreaterEqual(metadata["calls"], 1)
        from nalar.store import SessionStore

        summary = SessionStore(sessions).load(metadata["session"]).summary()
        self.assertEqual(summary["tokens"], 9)
        self.assertEqual(summary["by_model"],
                         {"primary": 7, "vendor/reviewer": 2})

    def test_code_omits_review_model_when_no_review_was_attempted(self):
        def fake_client(provider, model, fallbacks=()):
            return lambda messages, tools: Reply(text="nothing to do")

        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--review",
                                   "--review-model", "vendor/reviewer",
                                   "Do nothing"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 1)
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual(metadata["review"]["status"], "no_patch")
        self.assertEqual(metadata["review"]["model"], "vendor/reviewer")
        self.assertNotIn("review_model", metadata)

    def test_code_review_fix_reviews_again_with_the_reviewer(self):
        seen = []
        reviewer_calls = []
        findings = [
            [{"file": "app.py", "line": 1, "severity": "high",
              "title": "Still wrong", "detail": "Try again."}],
            [],
        ]

        def fake_client(provider, model, fallbacks=()):
            seen.append(model)
            if model == "vendor/reviewer":
                def reviewer(messages, tools):
                    reviewer_calls.append(messages[-1]["content"])
                    item = findings[min(len(reviewer_calls) - 1, 1)]
                    return Reply(text=json.dumps(item), tokens=2, calls=())
                return reviewer
            state = {"n": 0}

            def primary(messages, tools):
                state["n"] += 1
                if state["n"] == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),))
                if state["n"] == 2:
                    return Reply(text="done")
                if state["n"] == 3:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "42", "after": "43"}),))
                return Reply(text="fixed")
            return primary

        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--review",
                                   "--review-model", "vendor/reviewer",
                                   "--review-fix-attempts", "1",
                                   "Fix the answer"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(seen, ["primary", "vendor/reviewer"])
        self.assertEqual(len(reviewer_calls), 2)
        self.assertIn("+answer = 43", out.getvalue())
        metadata = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual(metadata["review_model"], "vendor/reviewer")
        self.assertEqual(metadata["review_fix_attempts"], 1)
        self.assertEqual(metadata["review"]["findings"], [])

    def test_code_json_emits_one_document_with_the_patch(self):
        def fake_client(provider, model, fallbacks=()):
            state = {"n": 0}

            def chat(messages, tools):
                state["n"] += 1
                if state["n"] == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),),
                                 tokens=3)
                return Reply(text="done", tokens=4)
            return chat

        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}), \
                patch("nalar.__main__._client", side_effect=fake_client), \
                patch("sys.argv", ["nalar", "code", "--repo", str(self.root),
                                   "--model", "primary", "--json",
                                   "Bump the answer"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertIn("+answer = 42", payload["patch"])
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("+answer = 42", err.getvalue())

    def test_cli_github_annotations_escape_model_text(self):
        (self.root / "app.py").write_text("answer = 0\n")
        findings = json.dumps([{
            "file": "app.py", "line": 1, "severity": "high",
            "title": "A: B, C\nD", "detail": "x\ny%"}])

        def chat(messages, tools):
            return type("R", (), {"text": findings, "tokens": 1, "calls": ()})()

        code, out, _ = self.run_cli("--model", "fixture", "--format", "github",
                                    chat=chat)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(
            "::error file=app.py,line=1,"
            "title=Nalar high%3A A%3A B%2C C D::"))
        self.assertIn("x y%25", out)
        self.assertNotIn("\nD", out)

    def test_review_includes_explicit_context(self):
        seen = []

        def chat(messages, tools):
            seen.append(messages[-1]["content"])
            return type("R", (), {"text": self.findings(), "tokens": 2,
                                  "calls": ()})()

        result = review(chat, "diff --git a/x b/x\n", model="fixture",
                        context="helpers.py returns 42")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Review this diff", seen[0])
        self.assertIn("helpers.py returns 42", seen[0])

    def test_cli_context_is_attached_and_validated(self):
        (self.root / "app.py").write_text("answer = 0\n")
        context = self.root / "notes.md"
        context.write_text("The helper returns 42.\n", encoding="utf-8")
        seen = []

        def chat(messages, tools):
            seen.append(messages[-1]["content"])
            return type("R", (), {"text": self.findings(), "tokens": 1,
                                  "calls": ()})()

        code, _, _ = self.run_cli("--model", "fixture", "--context",
                                  str(context), chat=chat)
        self.assertEqual(code, 0)
        self.assertIn("The helper returns 42.", seen[0])
        missing = self.run_cli("--model", "fixture", "--context",
                               str(self.root / "missing.md"), chat=chat)
        self.assertEqual(missing[0], 2)
        self.assertIn("Context file", missing[2])

    def test_review_text_keeps_findings_on_one_line(self):
        from nalar.__main__ import _review_text

        lines = _review_text({"findings": [{
            "severity": "high", "file": "app.py", "line": 1,
            "title": "real\n[high] app.py:1 forged"}]})
        self.assertEqual(len(lines), 1)
        self.assertIn("\\n", lines[0])

    def test_review_text_output_escapes_forged_lines(self):
        (self.root / "app.py").write_text("answer = 0\n")
        result = {"status": "ok", "tokens": 1, "findings": [{
            "severity": "high", "file": "app.py", "line": 1,
            "title": "benign\n[high] app.py:1 FORGED",
            "detail": "ok\n[high] app.py:9 also forged"}]}
        out = StringIO()
        with patch("nalar.review.review", return_value=result), \
                patch.dict(os.environ, {"NALAR_API_KEY": "k"}), \
                patch.object(sys, "argv", [
                    "nalar", "review", "--repo", str(self.root),
                    "--model", "fixture", "--format", "text"]), \
                redirect_stdout(out), redirect_stderr(StringIO()):
            code = main()
        self.assertEqual(code, 0)
        self.assertIn("benign\\n[high] app.py:1 FORGED", out.getvalue())
        self.assertNotIn("\n[high] app.py:1 FORGED", out.getvalue())
