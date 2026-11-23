import json
import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from nalar.coding import Repository
from nalar.agent import Budget
from nalar.core import Call, ProviderError, Reply
from nalar.session import interactive
from nalar.code_benchmark import evaluate
from nalar.config import load_config


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q")
        self.file = self.root / "app.py"
        self.file.write_text("answer = 41\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def session(self, commands, edits):
        def factory():
            calls, final = next(edits)
            return lambda messages, _: Reply(calls=calls) if len(messages) == 2 else final

        out = io.StringIO()
        interactive(Repository(self.root), factory, io.StringIO(commands), out)
        return out.getvalue()

    @staticmethod
    def edit(before, after):
        return Call("replace", {"path": "app.py", "before": before, "after": after})

    def test_fix_command_verifies_and_stages_without_writing(self):
        config = replace(
            load_config(self.root, global_file=self.root / "none.json"),
            verify=((sys.executable, "-c",
                     "import app; assert app.answer == 42"),))
        edits = iter([((self.edit("41", "42"),), Reply(text="fixed"))])

        def factory():
            calls, final = next(edits)
            return lambda messages, _: (Reply(calls=calls) if len(messages) == 2
                                        else final)

        out = io.StringIO()
        interactive(Repository(self.root), factory, io.StringIO("/fix\nyes\n/quit\n"),
                    out, fix_config=config)
        text = out.getvalue()
        self.assertIn("Verifying the committed snapshot", text)
        self.assertIn("+answer = 42", text)
        self.assertIn("Patch ready", text)
        self.assertEqual(self.file.read_text(), "answer = 41\n")

    def test_fix_command_accepts_guidance(self):
        config = replace(
            load_config(self.root, global_file=self.root / "none.json"),
            verify=((sys.executable, "-c",
                     "import app; assert app.answer == 42"),))
        tasks = []
        edits = iter([((self.edit("41", "42"),), Reply(text="fixed"))])

        def factory():
            calls, final = next(edits)

            def chat(messages, _):
                tasks.append(messages[-1]["content"])
                return Reply(calls=calls) if len(messages) == 2 else final

            return chat

        out = io.StringIO()
        interactive(Repository(self.root), factory,
                    io.StringIO("/fix keep it small\nyes\n/quit\n"), out,
                    fix_config=config)
        self.assertIn("keep it small", tasks[0])

    def test_apply_requires_explicit_secret_confirmation(self):
        repository = Repository(self.root)
        repository.call("replace", {
            "path": "app.py", "before": "41",
            "after": "42\nAWS_KEY = \"AKIAABCDEFGHIJKLMNOP\""})
        out = io.StringIO()
        interactive(repository, lambda: None,
                    io.StringIO("/apply\nyes\n/quit\n"), out)
        self.assertIn("--allow-secrets", out.getvalue())
        self.assertEqual(self.file.read_text(), "answer = 41\n")
        self.assertIn("AKIA", repository.patch())
        out = io.StringIO()
        interactive(repository, lambda: None,
                    io.StringIO("/apply --allow-secrets\nyes\n/quit\n"), out)
        self.assertIn("AKIA", self.file.read_text())

    def test_fix_command_needs_verify_configuration(self):
        out = io.StringIO()
        interactive(Repository(self.root), lambda: None,
                    io.StringIO("/fix\n/quit\n"), out)
        self.assertIn("Add a verify list", out.getvalue())

    def test_revisions_and_failed_turn_rollback(self):
        output = self.session("fix\nrevise\nabort\n/diff\n/apply\nyes\n/quit\n", iter([
            ((self.edit("41", "42"),), Reply(text="first")),
            ((self.edit("42", "43"),), Reply(text="second")),
            ((self.edit("43", "99"),), Reply()),
        ]))
        self.assertIn("+answer = 43", output)
        self.assertNotIn("+answer = 99", output)
        self.assertEqual(self.file.read_text(), "answer = 43\n")

    def test_declined_or_dirty_apply_keeps_original_files(self):
        commands = "fix\n/apply\nno\n/apply\nyes\n/quit\n"
        (self.root / "local.txt").write_text("untracked")
        output = self.session(commands, iter([((self.edit("41", "42"),), Reply(text="proposed"))]))
        self.assertIn("Cancelled", output)
        self.assertIn("not clean", output)
        self.assertEqual(self.file.read_text(), "answer = 41\n")

    def test_failed_tasks_show_the_provider_reason(self):
        def factory():
            def chat(messages, _):
                raise ProviderError("http_500")

            return chat

        out = io.StringIO()
        interactive(Repository(self.root), factory, io.StringIO("fix\n/quit\n"), out,
                    retries=0)
        self.assertIn("reason: http_500", out.getvalue())

    def test_extension_warnings_are_shown_at_start(self):
        out = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/quit\n"), out,
                    warnings=("mcp srv: unavailable",))
        self.assertIn("warning: mcp srv: unavailable", out.getvalue())

    def test_mcp_prompts_can_be_listed_and_run(self):
        from nalar.extensions import Prompt

        prompts = (Prompt("srv__review", "Review one file", ({"name": "focus"},),
                          lambda args: f"Review {args.get('focus', 'all')}."),)
        tasks = []

        def factory():
            def chat(messages, _):
                tasks.append(messages[-1]["content"])
                return Reply(text="ok")

            return chat

        listed = io.StringIO()
        interactive(Repository(self.root), factory, io.StringIO("/prompts\n/quit\n"),
                    listed, prompts=prompts)
        self.assertIn("srv__review", listed.getvalue())
        self.assertIn("focus", listed.getvalue())

        out = io.StringIO()
        interactive(Repository(self.root), factory,
                    io.StringIO("/prompt srv__review focus=security\n/quit\n"),
                    out, prompts=prompts)
        self.assertEqual(tasks[0], "Review security.")

    def test_prompt_text_is_never_dispatched_as_a_command(self):
        from nalar.commands import Command
        from nalar.extensions import Prompt

        calls = []

        def factory():
            def chat(messages, _):
                calls.append(messages[-1]["content"])
                return Reply(text="ok")

            return chat

        illegal = (Prompt("srv__quit", "Quit", (), lambda args: "/quit"),)
        interactive(Repository(self.root), factory,
                    io.StringIO("/prompt srv__quit\n/quit\n"), io.StringIO(),
                    prompts=illegal)
        self.assertEqual(calls, ["/quit"])

        command = Command("audit", "Audit", "Audit $ARGUMENTS.", self.root / "audit.md")
        shadow = (Prompt("srv__shadow", "Shadow", (), lambda args: "/audit src"),)
        calls.clear()
        interactive(Repository(self.root), factory,
                    io.StringIO("/prompt srv__shadow\n/quit\n"), io.StringIO(),
                    prompts=shadow, commands=(command,))
        self.assertEqual(calls, ["/audit src"])

    def test_prompt_arguments_can_be_quoted(self):
        from nalar.extensions import Prompt

        seen = {}
        prompts = (Prompt("srv__review", "Review", ({"name": "focus"},),
                          lambda args: seen.update(args) or "Review it."),)
        out = io.StringIO()
        interactive(Repository(self.root), lambda: (lambda messages, _: Reply(text="ok")),
                    io.StringIO('/prompt srv__review focus="two words"\n/quit\n'), out,
                    prompts=prompts)
        self.assertEqual(seen, {"focus": "two words"})

        seen.clear()
        interactive(Repository(self.root), lambda: (lambda messages, _: Reply(text="ok")),
                    io.StringIO("/prompt srv__review pattern=\\d+ path=C:\\repo\\file.txt\n"
                                "/quit\n"), io.StringIO(), prompts=prompts)
        self.assertEqual(seen, {"pattern": "\\d+", "path": "C:\\repo\\file.txt"})

    def test_an_empty_quoted_prompt_argument_is_rejected(self):
        from nalar.extensions import Prompt

        seen = {}
        prompts = (Prompt("srv__review", "Review", (),
                          lambda args: seen.update(args) or "Review."),)
        out = io.StringIO()
        interactive(Repository(self.root), lambda: (lambda *_: Reply(text="ok")),
                    io.StringIO('/prompt srv__review ""\n/quit\n'), out, prompts=prompts)
        self.assertEqual(seen, {})
        self.assertIn("key=value", out.getvalue())

    def test_mcp_prompt_errors_are_explained(self):
        from nalar.extensions import Prompt

        prompts = (Prompt("srv__review", "Review", (), lambda args: ""),)
        out = io.StringIO()
        interactive(Repository(self.root), lambda: None,
                    io.StringIO("/prompt missing\n/prompt srv__review\n"
                                "/prompt srv__review bad\n/quit\n"),
                    out, prompts=prompts)
        text = out.getvalue()
        self.assertIn("Unknown MCP prompt", text)
        self.assertIn("no text", text)
        self.assertIn("key=value", text)

    def test_skills_command_lists_project_skills(self):
        from pathlib import Path as P

        from nalar.skills import Skill

        skills = (Skill("review", "Review a diff", "body", P("SKILL.md")),)
        out = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/skills\n/quit\n"),
                    out, skills=skills)
        self.assertIn("review: Review a diff", out.getvalue())

        empty = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/skills\n/quit\n"),
                    empty)
        self.assertIn("No skills", empty.getvalue())

    def test_models_command_lists_provider_models(self):
        out = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/models\n/quit\n"), out,
                    models_fetcher=lambda: ["alpha", "beta"])
        self.assertIn("alpha", out.getvalue())
        self.assertIn("beta", out.getvalue())

    def test_models_command_explains_a_missing_key_or_failure(self):
        no_key = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/models\n/quit\n"), no_key)
        self.assertIn("API key", no_key.getvalue())

        def broken():
            raise ValueError("boom")

        failed = io.StringIO()
        interactive(Repository(self.root), lambda: None, io.StringIO("/models\n/quit\n"), failed,
                    models_fetcher=broken)
        self.assertIn("could not be loaded", failed.getvalue())

    def test_progress_lines_only_appear_on_a_terminal(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        def factory():
            state = {"turn": 0}

            def chat(messages, _):
                state["turn"] += 1
                if state["turn"] == 1:
                    return Reply(calls=(Call("list_files", {"prefix": "", "offset": 0}),))
                return Reply(text="done")

            return chat

        tty = TTY()
        interactive(Repository(self.root), factory, io.StringIO("look\n/quit\n"), tty)
        self.assertIn("· list_files ok", tty.getvalue())

        plain = io.StringIO()
        interactive(Repository(self.root), factory, io.StringIO("look\n/quit\n"), plain)
        self.assertNotIn("· list_files", plain.getvalue())

    def test_changed_base_is_rejected_even_when_clean(self):
        repo = Repository(self.root)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "--allow-empty", "-qm", "new HEAD")
        output = io.StringIO()
        edit = self.edit("41", "42")
        interactive(repo, lambda: lambda messages, _: (
            Reply(calls=(edit,)) if len(messages) == 2 else Reply(text="ready")),
            io.StringIO("fix\n/apply\nyes\n/quit\n"), output)
        self.assertIn("HEAD changed", output.getvalue())
        self.assertEqual(self.file.read_text(), "answer = 41\n")

    def test_apply_refuses_repositories_with_local_git_filters(self):
        self.git("config", "filter.local.clean", "cat")
        output = self.session("fix\n/apply\nyes\n/quit\n",
                              iter([((self.edit("41", "42"),), Reply(text="proposed"))]))
        self.assertIn("Git filters are configured", output)
        self.assertEqual(self.file.read_text(), "answer = 41\n")

    def test_resumed_tasks_are_sent_as_previous_revisions(self):
        seen = []

        def chat(messages, _):
            seen.append(messages)
            return Reply(text="ok")

        output = io.StringIO()
        interactive(Repository(self.root), lambda: chat, io.StringIO("new task\n/quit\n"), output,
                    prior=["old task"])
        self.assertIn("Previous user revisions", seen[0][1]["content"])
        self.assertIn("old task", seen[0][1]["content"])

    def test_tool_approval_denial_in_chat(self):
        from nalar.tools import ToolBelt

        executed = []
        belt = ToolBelt()
        belt.add({"type": "function", "function": {
            "name": "fixture__write", "description": "write",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}},
                 lambda args: executed.append(1) or "ok")
        belt.apply_policy(None, (), ["fixture__*"])

        def chat(messages, _):
            if len(messages) == 2:
                return Reply(calls=(Call("fixture__write", {}),))
            return Reply(text="done")

        output = io.StringIO()
        interactive(Repository(self.root), lambda: chat, io.StringIO("task\nn\n/quit\n"), output,
                    belt=belt)
        self.assertEqual(executed, [])
        self.assertIn("Approval required", output.getvalue())

    def test_chat_slash_commands_model_review_and_verify(self):
        seen_models = []

        def chat(messages, tools):
            if messages[0]["content"].startswith("You review code changes"):
                return Reply(text='[{"file":"app.py","line":1,"severity":"high",'
                                  '"title":"Wrong","detail":"Fix it"}]')
            if len(messages) == 2:
                return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                    "after": "42"}),))
            return Reply(text="Done.")

        import sys

        output = io.StringIO()
        commands = "task\n/review\n/model other\n/verify\nyes\n/quit\n"
        interactive(Repository(self.root), lambda: chat, io.StringIO(commands), output,
                    model="fixture", set_model=seen_models.append,
                    verify_commands=((sys.executable, "-c", "print('checks pass')"),),
                    secret_env=())
        text = output.getvalue()
        self.assertIn("[high] app.py:1 Wrong", text)
        self.assertEqual(seen_models, ["other"])
        self.assertIn("checks pass", text)

    def test_undo_reverts_the_last_successful_step(self):
        count = {"value": 0}

        def chat(messages, _):
            if len(messages) == 2:
                count["value"] += 1
                if count["value"] == 1:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                        "after": "42"}),))
                return Reply(calls=(Call("replace", {"path": "app.py", "before": "42",
                                                    "after": "43"}),))
            return Reply(text="ok")

        output = io.StringIO()
        repository = Repository(self.root)
        interactive(repository, lambda: chat,
                    io.StringIO("one\ntwo\n/undo\n/undo\n/undo\n/quit\n"), output)
        self.assertIn("Restored the previous proposal state.", output.getvalue())
        self.assertIn("Nothing to undo.", output.getvalue())
        self.assertEqual(repository.patch(), "")

    def test_undo_removes_the_revision_from_later_prompts(self):
        seen = {}

        def chat(messages, _):
            if len(messages) > 2:
                return Reply(text="done")
            content = messages[1]["content"]
            if content.rstrip().endswith("three"):
                seen["prompt"] = content
                return Reply(text="done")
            if content.rstrip().endswith("one"):
                return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                    "after": "42"}),))
            return Reply(calls=(Call("replace", {"path": "app.py", "before": "42",
                                                "after": "43"}),))

        output = io.StringIO()
        interactive(Repository(self.root), lambda: chat,
                    io.StringIO("one\ntwo\n/undo\nthree\n/quit\n"), output)
        self.assertIn("Previous user revisions", seen["prompt"])
        self.assertIn("one", seen["prompt"])
        self.assertNotIn("two", seen["prompt"])

    def test_chat_resets_the_token_budget_between_tasks(self):
        attempts = []

        def chat(messages, _):
            attempts.append(1)
            return Reply(text="ok", tokens=6)

        interactive(Repository(self.root), lambda: chat, io.StringIO("one\ntwo\n/quit\n"),
                    io.StringIO(), token_budget=5, budget=Budget(5))
        self.assertEqual(len(attempts), 2)

    def test_coding_benchmark_rejects_claims_without_patches(self):
        report = evaluate(lambda: lambda *_: Reply(text="Fixed."), model="test")
        self.assertGreaterEqual(len(report["runs"]), 2)
        self.assertTrue(all(not row["passed"] for row in report["runs"]))

    def test_coding_benchmark_checks_all_files_after_git_apply(self):
        def factory():
            def chat(messages, _):
                if len(messages) > 2:
                    return Reply(text="Done.")
                if "Delete src/old.py" in messages[1]["content"]:
                    return Reply(calls=(Call("delete_file", {"path": "src/old.py"}),))
                if "Rename src/a.py" in messages[1]["content"]:
                    return Reply(calls=(Call("rename_file", {
                        "path": "src/a.py", "new_path": "src/b.py"}),))
                if "mean" in messages[1]["content"]:
                    return Reply(calls=(Call("replace", {
                        "path": "src/stats.py", "before": "    return",
                        "after": "    if not xs:\n        return 0\n    return",
                    }),))
                if "divide" in messages[1]["content"]:
                    return Reply(calls=(Call("replace", {
                        "path": "src/math.py", "before": "    return a / b",
                        "after": "    if b == 0:\n        return None\n    return a / b",
                    }),))
                if "Make scripts/run.sh executable" in messages[1]["content"]:
                    return Reply(calls=(Call("chmod_file", {
                        "path": "scripts/run.sh", "executable": True}),))
                if "flag0" in messages[1]["content"]:
                    return Reply(calls=(
                        Call("replace", {"path": "src/flags.py",
                                         "before": "flag0 = False", "after": "flag0 = True"}),
                        Call("replace", {"path": "src/flags.py",
                                         "before": "flag19 = False", "after": "flag19 = True"}),
                    ))
                return Reply(calls=(Call("replace", {
                    "path": "src/version.py", "before": "1.0.0", "after": "1.1.0",
                }),))
            return chat

        report = evaluate(factory, model="fixture")
        self.assertEqual([row["grade"] for row in report["runs"]],
                         ["exact", "exact", "artifact_mismatch", "artifact_mismatch",
                          "exact", "exact", "exact", "exact", "exact", "exact",
                          "exact", "exact", "exact", "exact"])
        self.assertEqual({row["case"] for row in report["runs"]},
                         {"empty_mean", "sync_version", "guard_divide",
                          "remove_obsolete", "rename_module", "two_flags",
                          "make_executable"})

    def test_coding_benchmark_grades_a_deleted_mode_artifact(self):
        def factory():
            def chat(messages, _):
                if len(messages) > 2:
                    return Reply(text="done")
                if "Make scripts/run.sh executable" in messages[1]["content"]:
                    return Reply(calls=(Call("delete_file", {
                        "path": "scripts/run.sh"}),))
                return Reply(text="skip")
            return chat

        report = evaluate(factory, model="fixture")
        rows = [row for row in report["runs"] if row["case"] == "make_executable"]
        self.assertEqual([row["grade"] for row in rows],
                         ["artifact_mismatch", "artifact_mismatch"])

    def test_fix_records_delegated_usage_and_clears_it(self):
        from nalar.store import SessionStore

        config = replace(
            load_config(self.root, global_file=self.root / "none.json"),
            verify=((sys.executable, "-c",
                     "import app; assert app.answer == 42"),))
        edits = iter([((self.edit("41", "42"),), Reply(text="fixed", tokens=20))])

        def factory():
            calls, final = next(edits)
            return lambda messages, _: (Reply(calls=calls, tokens=10)
                                        if len(messages) == 2 else final)

        usage = [{"model": "vendor/tiny", "tokens": 5}]
        budget = Budget(0)
        store = SessionStore(self.root / "sessions")
        session = store.create(self.root, "fixture", "base")
        interactive(Repository(self.root), factory,
                    io.StringIO("/fix\nyes\n/quit\n"), io.StringIO(),
                    store=store, session=session, fix_config=config,
                    budget=budget, subagent_usage=usage)
        loaded = store.load(session.id)
        fix = [entry for entry in loaded.entries
               if entry["kind"] == "fix"][-1]
        subagent = [entry for entry in loaded.entries
                    if entry["kind"] == "subagent"][-1]
        self.assertEqual((subagent["tokens"], subagent["model"]),
                         ("5", "vendor/tiny"))
        self.assertEqual(int(fix["tokens"]) + 5, budget.spent)
        self.assertEqual(loaded.summary()["tokens"], budget.spent)
        self.assertEqual(usage, [])

    def test_chat_review_escapes_provider_newlines(self):
        from types import SimpleNamespace

        repo = Repository(self.root)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        payload = json.dumps([{
            "file": "app.py\n[high] FORGED.py:1",
            "line": 1,
            "severity": "high",
            "title": "real\n[critical] FORGED",
            "detail": "ok\x1b[31m",
        }])

        def reviewer():
            return lambda messages, tools: Reply(text=payload)

        out = io.StringIO()
        interactive(repo, lambda: None, io.StringIO("/review\n/quit\n"), out,
                    review_factory=reviewer)
        text = out.getvalue()
        self.assertIn("real\\n[critical] FORGED", text)
        self.assertNotIn("\n[critical] FORGED", text)
        self.assertNotIn("\u001b", text)

    def test_chat_prompts_and_sessions_escape_newlines(self):
        from types import SimpleNamespace
        from nalar.store import SessionStore

        repo = Repository(self.root)
        store = SessionStore(self.root / "sessions")
        session = store.create(self.root, "fixture", "base")
        store.append(session, "user", "task\n[high] FORGED")
        prompts = (SimpleNamespace(name="p\n[high] FORGED", description="d\n[high] FORGED",
                                   arguments=()),)
        out = io.StringIO()
        interactive(repo, lambda: None, io.StringIO("/prompts\n/sessions\n/quit\n"), out,
                    store=store, session=session, prompts=prompts)
        text = out.getvalue()
        self.assertIn("p\\n[high] FORGED", text)
        self.assertIn("task\\n[high] FORGED", text)
        self.assertNotIn("\n[high] FORGED", text)
