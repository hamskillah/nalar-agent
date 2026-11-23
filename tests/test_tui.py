import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if importlib.util.find_spec("textual") is None:
    raise unittest.SkipTest("textual is not installed; install the project dependency")

from textual import events
from textual.widgets import Button, Input, Static, TextArea

from nalar.tui import CommandMenu, Composer, FilePicker

from nalar import hunks
from nalar.coding import Repository
from nalar.core import Call, Reply
from nalar.__main__ import main
from nalar.config import load_config
from nalar.session import MAX_PROMPT
from nalar.store import SessionStore
from nalar.tui import CredentialReview, NalarApp, PatchReview, PromptArguments


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_patch_review_toggles_a_mode_change_separately(self):
        diff = ("diff --git a/app.py b/app.py\n"
                "old mode 100644\nnew mode 100755\n"
                "index 1111111..2222222\n"
                "--- a/app.py\n+++ b/app.py\n"
                "@@ -1 +1 @@\n-answer = 41\n+answer = 42\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                screen = PatchReview(diff)
                app.push_screen(screen)
                await pilot.pause()
                self.assertIn((0, "mode"), screen.selected)
                self.assertIn((0, 0), screen.selected)
                screen.action_toggle_mode()
                await pilot.pause()
                self.assertNotIn((0, "mode"), screen.selected)
                self.assertIn((0, 0), screen.selected)
                screen.action_toggle_mode()
                await pilot.pause()
                self.assertIn((0, "mode"), screen.selected)
                screen.action_toggle_file()
                await pilot.pause()
                self.assertNotIn((0, "mode"), screen.selected)
                self.assertNotIn((0, 0), screen.selected)

    async def test_fix_action_proposes_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            config = replace(
                load_config(root, global_file=root / "none.json"),
                verify=((sys.executable, "-c",
                         "import app; assert app.answer == 42"),),
                token_budget=1_000_000)

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),))
                return Reply(text="fixed")

            app = NalarApp(Repository(root), "fixture", lambda model: chat,
                           verify_commands=config.verify, fix_config=config,
                           token_budget=1)
            app.budget.limit = 100
            app.budget.spent = 100
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_fix()
                await pilot.pause()
                await pilot.click("#confirm")
                await pilot.pause()
                async with asyncio.timeout(10):
                    while app.busy or not app.repo.patch():
                        await pilot.pause(0.03)
                self.assertIn("+answer = 42", app.repo.patch())
                self.assertEqual((root / "app.py").read_text(), "answer = 41\n")
                self.assertIn("FIX PROPOSED",
                              str(app.query_one("#status", Static).render()))
                self.assertEqual(app.budget.limit, 1_000_000)

    async def test_task_attributes_subagent_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            store = SessionStore(root / "sessions")
            session = store.create(str(root), "fixture", "base")
            extensions = SimpleNamespace(
                belt=SimpleNamespace(names=(), specs=()), specs=[], clients=(), system=None,
                skills=(), prompts=(), warnings=(), instructions=(),
                subagent_usage=[])

            def chat(messages, _):
                return Reply(text="done", tokens=20)

            app = NalarApp(Repository(root), "fixture", lambda model: chat,
                           extensions=extensions, store=store, session=session)
            async with app.run_test(size=(100, 30)) as pilot:
                app.extensions.subagent_usage.append(
                    {"model": "vendor/tiny", "tokens": 7})
                app.query_one("#composer", TextArea).load_text("Do it")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
            loaded = store.load(session.id)
            assistant = [entry for entry in loaded.entries
                         if entry["kind"] == "assistant"][-1]
            subagent = [entry for entry in loaded.entries
                        if entry["kind"] == "subagent"][-1]
            self.assertEqual((assistant["tokens"], subagent["tokens"],
                              subagent["model"]), ("13", "7", "vendor/tiny"))
            self.assertEqual(extensions.subagent_usage, [])

    async def test_fix_attributes_and_clears_subagent_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            config = replace(
                load_config(root, global_file=root / "none.json"),
                verify=((sys.executable, "-c",
                         "import app; assert app.answer == 42"),))

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),),
                        tokens=10)
                return Reply(text="fixed", tokens=20)

            from nalar.extensions import build_extensions
            store = SessionStore(root / "sessions")
            session = store.create(str(root), "fixture", "base")
            extensions = build_extensions(
                Repository(root), config, start_mcp=False, chat_factory=lambda: chat)
            self.addCleanup(extensions.close)
            extensions.subagent_usage.append({"model": "vendor/tiny", "tokens": 5})
            app = NalarApp(Repository(root), "fixture", lambda model: chat,
                           extensions=extensions, verify_commands=config.verify,
                           fix_config=config, store=store, session=session)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_fix()
                await pilot.pause()
                await pilot.click("#confirm")
                async with asyncio.timeout(10):
                    while app.busy:
                        await pilot.pause(0.03)
                spent = app.budget.spent
            loaded = store.load(session.id)
            fix = [entry for entry in loaded.entries
                   if entry["kind"] == "fix"][-1]
            subagent = [entry for entry in loaded.entries
                        if entry["kind"] == "subagent"][-1]
            self.assertEqual((subagent["tokens"], subagent["model"]),
                             ("5", "vendor/tiny"))
            self.assertEqual(int(fix["tokens"]) + 5, spent)
            self.assertEqual(loaded.summary()["tokens"], spent)
            self.assertEqual(extensions.subagent_usage, [])

    async def test_failed_task_still_clears_and_records_subagent_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            store = SessionStore(root / "sessions")
            session = store.create(str(root), "fixture", "base")
            extensions = SimpleNamespace(
                belt=SimpleNamespace(names=(), specs=()), specs=[], clients=(),
                system=None, skills=(), prompts=(), warnings=(), instructions=(),
                subagent_usage=[])
            app = NalarApp(Repository(root), "fixture",
                           lambda model: (lambda messages, tools: Reply(text="ok")),
                           extensions=extensions, store=store, session=session)
            with patch("nalar.tui.solve", side_effect=OSError("boom")):
                async with app.run_test(size=(100, 30)) as pilot:
                    app.extensions.subagent_usage.append(
                        {"model": "vendor/tiny", "tokens": 5})
                    app.query_one("#composer", TextArea).load_text("Do it")
                    await pilot.press("enter")
                    async with asyncio.timeout(5):
                        while app.busy:
                            await pilot.pause(0.03)
            loaded = store.load(session.id)
            subagent = [entry for entry in loaded.entries
                        if entry["kind"] == "subagent"][-1]
            self.assertEqual((subagent["tokens"], subagent["model"]),
                             ("5", "vendor/tiny"))
            self.assertEqual(extensions.subagent_usage, [])

    async def test_fix_action_refuses_to_mix_with_a_staged_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            config = replace(
                load_config(root, global_file=root / "none.json"),
                verify=((sys.executable, "-c", "import app"),))
            repo = Repository(root)
            repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
            app = NalarApp(repo, "fixture", lambda model: None,
                           verify_commands=config.verify, fix_config=config)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_fix()
                await pilot.pause()
                self.assertIs(app.screen, app.screen_stack[0])
                self.assertIn("+answer = 42", repo.patch())

    async def test_credential_apply_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            path = root / "app.py"
            path.write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41",
                        "after": "42\nAWS_KEY = \"AKIAABCDEFGHIJKLMNOP\""}),))
                return Reply(text="done")

            app = NalarApp(Repository(root), "fixture", lambda model: chat,
                           permission="auto")
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Secret")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while not isinstance(app.screen, CredentialReview):
                        await pilot.pause(0.03)
                await pilot.click("#cancel")
                await pilot.pause()
                self.assertEqual(path.read_text(), "answer = 41\n")
                self.assertIn("AKIA", app.repo.patch())
                self.assertFalse(app.read_only)

                app._reviewed(True)
                await pilot.pause()
                self.assertIsInstance(app.screen, CredentialReview)
                await pilot.click("#confirm")
                async with asyncio.timeout(5):
                    while app.busy or not app.read_only:
                        await pilot.pause(0.03)
                self.assertIn("AKIA", path.read_text())

    async def test_auto_apply_mode_applies_without_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            path = root / "app.py"
            path.write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41", "after": "43"}),))
                return Reply(text="Applied.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat, permission="auto")
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Bump")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while not app.read_only and app.busy or not path.read_text().startswith("answer = 43"):
                        await pilot.pause(0.03)
                        if not app.busy and not app.read_only:
                            break
                self.assertEqual(path.read_text(), "answer = 43\n")

    async def test_undo_apply_restores_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            path = root / "app.py"
            path.write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                         "after": "43"}),))
                return Reply(text="Applied.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat, permission="auto")
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Bump")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy or not app.read_only:
                        await pilot.pause(0.03)
                self.assertEqual(path.read_text(), "answer = 43\n")
                self.assertTrue(app.applied)
                await pilot.press("ctrl+u")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(path.read_text(), "answer = 41\n")
                self.assertFalse(app.applied)
                self.assertFalse(app.read_only)
                self.assertIn("UNDONE", str(app.query_one("#status", Static).render()))

    async def test_session_is_recorded_and_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            from nalar.store import SessionStore
            store = SessionStore(root / "sessions")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41", "after": "42"}),))
                return Reply(text="Proposed change.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat, store=store)
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Bump the answer")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertTrue(app.session.id)
            listed = store.list(root)
            self.assertEqual(len(listed), 1)
            saved = store.load(listed[0]["id"])
            self.assertEqual([entry["kind"] for entry in saved.entries], ["user", "assistant"])

            resumed = NalarApp(Repository(root), "fixture", lambda model: chat, store=store, session=saved)
            async with resumed.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)
                self.assertEqual(resumed.previous, ["Bump the answer"])
                self.assertEqual(resumed.session.id, saved.id)

    async def test_escape_clears_idle_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            app = NalarApp(Repository(tmp), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                composer = app.query_one("#composer", TextArea)
                composer.load_text("draft task")
                await pilot.press("escape")
                self.assertEqual(composer.text, "")

    async def test_model_picker_uses_the_provider_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            app = NalarApp(Repository(tmp), "fixture", None,
                           models_fetcher=lambda: ["alpha", "beta"])
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_model()
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.models_cache, ["alpha", "beta"])
                await pilot.press("enter")
                self.assertEqual(app.model, "alpha")

    async def test_model_can_be_changed_for_the_next_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            seen = []

            def factory(model):
                seen.append(model)
                return lambda messages, _: Reply(text="ok")

            app = NalarApp(Repository(tmp), "old-model", factory)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_model()
                await pilot.pause(0.05)
                await pilot.press(*"new-model")
                await pilot.press("enter")
                self.assertEqual(app.model, "new-model")
                app.query_one("#composer", TextArea).load_text("task")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
            self.assertEqual(seen, ["new-model"])

    async def test_configured_verification_runs_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            import sys
            app = NalarApp(Repository(root), "fixture", None,
                           verify_commands=([sys.executable, "-c", "print('checks pass')"],))
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_verify()
                await pilot.pause(0.05)
                await pilot.click("#cancel")
                self.assertIsNone(app.last_verify)
                app.action_verify()
                await pilot.pause(0.05)
                await pilot.click("#confirm")
                async with asyncio.timeout(5):
                    while app.busy or app.last_verify is None:
                        await pilot.pause(0.03)
                self.assertTrue(app.last_verify["ok"])
                self.assertIn("checks pass", app.last_verify["commands"][0]["output"])

    async def test_model_list_is_fetched_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            app = NalarApp(Repository(tmp), "fixture", None,
                           models_fetcher=lambda: ["alpha", "beta"])
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_models()
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.last_models, ["alpha", "beta"])

    async def test_mcp_prompt_is_inserted_into_the_composer(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")

            class Belt:
                names = set()

            class Prompt:
                name = "demo__review"
                description = "Review"
                arguments = ()

                @staticmethod
                def fetch(arguments):
                    return "Review the change carefully."

            class Extensions:
                belt = Belt()
                specs = []
                skills = ()
                warnings = ()
                system = ""
                prompts = (Prompt(),)

            app = NalarApp(Repository(tmp), "fixture", None, extensions=Extensions())
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_prompts()
                await pilot.pause(0.05)
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertIn("Review the change carefully.",
                              app.query_one("#composer", TextArea).text)

    async def test_auto_apply_refuses_a_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            path = root / "app.py"
            path.write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            (root / "untracked.txt").write_text("local work")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41", "after": "43"}),))
                return Reply(text="Proposed.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat, permission="auto")
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Bump")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(path.read_text(), "answer = 41\n")
                self.assertFalse(app.read_only)
                self.assertIn("NOT APPLIED", str(app.query_one("#status", Static).render()))

    async def test_run_stats_and_token_totals_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            from nalar.store import SessionStore
            store = SessionStore(root / "sessions")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                        "after": "42"}),), tokens=7)
                return Reply(text="Done.", tokens=3)

            app = NalarApp(Repository(root), "fixture", lambda model: chat, store=store)
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Bump")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.last_run.tokens, 10)
                self.assertIn("10 tokens", str(app.query_one("#run-stats", Static).render()))
                self.assertIn("10 tokens", str(app.query_one("#status", Static).render()))
            self.assertEqual(store.list(root)[0]["tokens"], 10)

    async def test_plan_is_shown_in_the_sidebar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            (root / "app.py").write_text("answer = 41\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c",
                            "user.email=f@example.test", "commit", "-qm", "fixture"],
                           check=True)
            app = NalarApp(Repository(root), "fixture", lambda model: lambda messages, tools: None)
            async with app.run_test(size=(100, 30)):
                app.plan = [{"step": "Read", "status": "completed"},
                            {"step": "Edit", "status": "in_progress"}]
                app._show_plan()
                shown = str(app.query_one("#plan", Static).render())
                self.assertIn("[x] Read", shown)
                self.assertIn("[>] Edit", shown)
                app._state("\x1b[2Jspoof")
                status = str(app.query_one("#status", Static).render())
                self.assertNotIn("\x1b", status)

    async def test_tool_approval_callback_uses_the_modal_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            app = NalarApp(Repository(tmp), "fixture", None)
            async with app.run_test(size=(100, 30)):
                app.push_screen = lambda screen, callback=None: callback(True)
                self.assertTrue(await asyncio.to_thread(app.approve_tool, "fixture__write", {}))
                app.push_screen = lambda screen, callback=None: callback(False)
                self.assertFalse(await asyncio.to_thread(app.approve_tool, "fixture__write", {}))

    async def test_failed_turn_does_not_replace_the_visible_plan(self):
        from nalar.core import ProviderError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            (root / "app.py").write_text("answer = 41\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c",
                            "user.email=f@example.test", "commit", "-qm", "fixture"],
                           check=True)

            def chat(messages, tools):
                if len(messages) == 2:
                    return Reply(calls=(Call("update_plan", {"items": [
                        {"step": "new plan", "status": "in_progress"}]}),))
                raise ProviderError("invalid_response")

            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app.plan = [{"step": "old plan", "status": "pending"}]
                app.query_one("#composer", TextArea).load_text("task")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.plan,
                                 [{"step": "old plan", "status": "pending"}])

    async def test_fork_session_branches_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            (root / "app.py").write_text("answer = 41\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c",
                            "user.email=f@example.test", "commit", "-qm", "fixture"],
                           check=True)
            head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            store = SessionStore(root / "sessions")
            session = store.create(root, "fixture", head)
            app = NalarApp(Repository(root), "fixture", None, store=store,
                           session=session)
            async with app.run_test(size=(100, 30)):
                app.action_fork_session()
                self.assertNotEqual(app.session.id, session.id)
                self.assertEqual(len(store.list(root)), 2)
                self.assertEqual(str(app.query_one("#session-id", Static).render()),
                                 app.session.id)

    async def test_fork_failure_keeps_the_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            (root / "app.py").write_text("answer = 41\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c",
                            "user.email=f@example.test", "commit", "-qm", "fixture"],
                           check=True)
            head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            store = SessionStore(root / "sessions")
            session = store.create(root, "fixture", head)
            app = NalarApp(Repository(root), "fixture", None, store=store,
                           session=session)
            async with app.run_test(size=(100, 30)):
                with patch.object(store, "fork",
                                  side_effect=ValueError("session_not_found")):
                    app.action_fork_session()
                self.assertEqual(app.session.id, session.id)
                self.assertEqual(str(app.query_one("#session-id", Static).render()),
                                 session.id)

    async def test_resume_latest_aborts_if_a_task_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="],
                           check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-qm", "fixture"],
                           check=True)
            base = SessionStore(root / "sessions")
            repo = Repository(root)
            first = base.create(root, "fixture", repo.commit.decode())
            base.append(first, "user", "first task")
            second = base.create(root, "fixture", repo.commit.decode())
            base.append(second, "user", "latest task")

            class SlowStore(SessionStore):
                def list(self, *args, **kwargs):
                    time.sleep(0.25)
                    return super().list(*args, **kwargs)

            app = NalarApp(repo, "fixture", None, store=SlowStore(root / "sessions"),
                           session=first)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_resume_latest()
                app.busy = True
                await pilot.pause(0.4)
                self.assertEqual(app.session.id, first.id)
                app.busy = False

    async def test_resume_latest_aborts_after_a_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="],
                           check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-qm", "fixture"],
                           check=True)
            base = SessionStore(root / "sessions")
            repo = Repository(root)
            first = base.create(root, "fixture", repo.commit.decode())
            base.append(first, "user", "first task")
            second = base.create(root, "fixture", repo.commit.decode())
            base.append(second, "user", "second task")
            release = threading.Event()

            class StaleStore(SessionStore):
                def list(self, *args, **kwargs):
                    stale = super().list(*args, **kwargs)
                    release.wait(5)
                    return stale

            app = NalarApp(repo, "fixture",
                           lambda model: (lambda messages, tools: Reply(text="done")),
                           store=StaleStore(root / "sessions"), session=first)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_resume_latest()
                await pilot.pause(0.05)
                app.query_one("#composer", TextArea).load_text("Run this")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                release.set()
                await pilot.pause(0.2)
                self.assertEqual(app.session.id, first.id)
                self.assertNotEqual(app.session.id, second.id)

    async def test_resume_latest_survives_a_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="],
                           check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-qm", "fixture"],
                           check=True)
            base = SessionStore(root / "sessions")
            repo = Repository(root)
            session = base.create(root, "fixture", repo.commit.decode())

            class BrokenStore(SessionStore):
                def list(self, *args, **kwargs):
                    raise OSError("broken store")

            app = NalarApp(repo, "fixture", None, store=BrokenStore(root / "sessions"),
                           session=session)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_resume_latest()
                await pilot.pause(0.2)
                self.assertEqual(app.session.id, session.id)
                self.assertTrue(app.is_running)

    async def test_resume_latest_runs_off_the_ui_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="],
                           check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-qm", "fixture"],
                           check=True)
            base = SessionStore(root / "sessions")
            repo = Repository(root)
            first = base.create(root, "fixture", repo.commit.decode())
            base.append(first, "user", "first task")
            second = base.create(root, "fixture", repo.commit.decode())
            base.append(second, "user", "latest task")

            class SlowStore(SessionStore):
                def list(self, *args, **kwargs):
                    time.sleep(0.25)
                    return super().list(*args, **kwargs)

            store = SlowStore(root / "sessions")
            app = NalarApp(repo, "fixture", None, store=store, session=first)
            async with app.run_test(size=(100, 30)) as pilot:
                start = time.monotonic()
                app.action_resume_latest()
                self.assertLess(time.monotonic() - start, 0.15)
                async with asyncio.timeout(5):
                    while app.session.id != second.id:
                        await pilot.pause(0.03)
            self.assertEqual(app.session.id, second.id)

    async def test_resume_latest_session_switches_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            from nalar.store import SessionStore
            store = SessionStore(root / "sessions")
            repo = Repository(root)
            first = store.create(root, "fixture", repo.commit.decode())
            store.append(first, "user", "first task")
            second = store.create(root, "fixture", repo.commit.decode())
            store.append(second, "user", "latest task")
            app = NalarApp(repo, "fixture", None, store=store, session=first)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_resume_latest()
                await pilot.pause(0.05)
                self.assertEqual(app.session.id, second.id)
                self.assertEqual(app.previous, ["latest task"])

    async def test_slash_commands_control_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")

            def chat(messages, tools):
                if messages[0]["content"].startswith("You review code changes"):
                    return Reply(text='[{"file":"app.py","line":1,"severity":"high",'
                                      '"title":"Wrong","detail":"Fix it"}]')
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                        "after": "42"}),))
                return Reply(text="Done.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                composer = app.query_one("#composer", TextArea)
                composer.load_text("Bump")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertTrue(app.repo.patch())

                composer.load_text("/review")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.last_review["findings"][0]["severity"], "high")

                composer.load_text("/model other-model")
                await pilot.press("enter")
                self.assertEqual(app.model, "other-model")

                composer.load_text("/reset")
                await pilot.press("enter")
                self.assertEqual(app.repo.patch(), "")

    async def test_diff_viewer_jumps_between_hunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                    "@@ -1 +1 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d\n")
            app = NalarApp(Repository(tmp), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.push_screen(type(app.screen)(diff) if False else __import__("nalar.tui", fromlist=["PatchReview"]).PatchReview(diff))
                await pilot.pause(0.05)
                await pilot.press("]")
                self.assertEqual(app.screen.hunk_index, 0)
                await pilot.press("]")
                self.assertEqual(app.screen.hunk_index, 1)
                await pilot.press("[")
                self.assertEqual(app.screen.hunk_index, 0)

    async def test_hunkless_section_can_be_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c",
                            "user.email=f@example.test", "commit", "--allow-empty", "-qm",
                            "fixture"], check=True)
            diff = ("diff --git a/blob.bin b/blob.bin\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n+++ b/blob.bin\n"
                    "GIT binary patch\nliteral 3\nKcmZ?0Y00000\n")
            app = NalarApp(Repository(tmp), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.push_screen(PatchReview(diff))
                await pilot.pause(0.05)
                screen = app.screen
                self.assertIn((0, None), screen.selected)
                await pilot.press("space")
                self.assertNotIn((0, None), screen.selected)
                self.assertEqual(hunks.render(screen.files, screen.selected), "")

    async def test_file_picker_hides_staged_deleted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            (root / "README.md").write_text("demo\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            repo = Repository(root)
            repo.call("delete_file", {"path": "README.md"})
            app = NalarApp(repo, "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_files()
                await pilot.pause(0.05)
                self.assertNotIn("README.md", app.screen.paths)
                self.assertIn("app.py", app.screen.paths)

    async def test_file_reference_is_inserted_from_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_files()
                await pilot.pause(0.05)
                await pilot.press("enter")
                composer = app.query_one("#composer", TextArea)
                self.assertIn("@app.py", composer.text)

    async def test_undo_reverts_the_last_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            count = {"value": 0}

            def chat(messages, _):
                if len(messages) == 2:
                    count["value"] += 1
                    before = "41" if count["value"] == 1 else "42"
                    after = "42" if count["value"] == 1 else "43"
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": before,
                                                        "after": after}),))
                return Reply(text="ok")

            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                composer = app.query_one("#composer", TextArea)
                for task in ("one", "two"):
                    composer.load_text(task)
                    await pilot.press("enter")
                    async with asyncio.timeout(5):
                        while app.busy:
                            await pilot.pause(0.03)
                self.assertIn("+answer = 43", app.repo.patch())
                self.assertEqual(app.previous, ["one", "two"])
                composer.load_text("/undo")
                await pilot.press("enter")
                self.assertIn("+answer = 42", app.repo.patch())
                self.assertNotIn("+answer = 43", app.repo.patch())
                self.assertEqual(app.previous, ["one"])

    async def test_typing_at_opens_the_file_picker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                composer = app.query_one("#composer", Composer)
                composer.insert("look at ")
                self.assertTrue(composer._at_word_start())
                await composer._on_key(events.Key("at", "@"))
                await pilot.pause(0.05)
                self.assertIsInstance(app.screen, FilePicker)
                await pilot.press("enter")
                self.assertIn("@app.py", composer.text)

    async def test_review_can_apply_only_selected_hunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            lines = [f"line {index}\n" for index in range(1, 31)]
            (root / "app.py").write_text("".join(lines))
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            repo = Repository(root)
            repo.call("replace", {"path": "app.py", "before": "line 3\n", "after": "LINE 3\n"})
            repo.call("replace", {"path": "app.py", "before": "line 25\n",
                                  "after": "LINE 25\n"})
            diff = repo.patch()
            self.assertEqual(len(diff.split("@@ ")) - 1, 2)

            app = NalarApp(repo, "fixture", None)
            captured = {}
            async with app.run_test(size=(120, 40)) as pilot:
                app.push_screen(PatchReview(diff),
                                lambda value: captured.setdefault("value", value))
                await pilot.pause(0.1)
                await pilot.press("]")
                await pilot.press("space")
                await pilot.pause(0.1)
                await pilot.click("#selected")
                await pilot.pause(0.1)
            value = captured["value"]
            self.assertIsInstance(value, dict)
            self.assertEqual((value["kept"], value["total"]), (1, 2))
            patch = value["patch"]
            self.assertIn("+LINE 25", patch)
            self.assertNotIn("+LINE 3\n", patch)

    def test_applied_message_reports_a_partial_selection(self):
        from nalar.tui import _applied_message

        self.assertIn("1 of 2", _applied_message(1, 2))
        self.assertNotIn("discarded", _applied_message(2, 2))
        self.assertIn("Commit", _applied_message(None, None))

    async def test_oversized_mcp_prompts_are_refused_not_truncated(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            prompt = Prompt("srv__huge", "Huge prompt", (),
                            lambda args: "x" * (MAX_PROMPT + 5))
            app = NalarApp(Repository(root), "fixture",
                           lambda model: (lambda messages, tools: Reply(text="ok")))
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                self.assertEqual(app.query_one("#composer", TextArea).text, "")

    def test_tui_command_reaches_the_app_with_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            (root / "nalar.json").write_text(json.dumps({"model": "fixture"}))
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")

            class Stream(io.StringIO):
                def isatty(self):
                    return True

            class Stub:
                captured = None

                def __init__(self, *args, **kwargs):
                    Stub.captured = (args, kwargs)

                def run(self):
                    return None

            env = {"NALAR_API_KEY": "fixture-key",
                   "NALAR_GLOBAL_CONFIG": str(root / "missing.json")}
            with patch.dict(os.environ, env), patch("nalar.tui.NalarApp", Stub), \
                    patch("sys.stdin", Stream()), patch("sys.stdout", Stream()), \
                    patch("sys.argv", ["nalar", "tui", "--repo", str(root),
                                       "--no-sessions", "--max-turns", "3"]):
                code = main()
            self.assertEqual(code, 0)
            self.assertEqual(Stub.captured[1]["max_turns"], 3)

    def test_tui_continue_resumes_the_latest_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            def git(*args):
                return subprocess.run(["git", "-C", tmp, *args], check=True,
                                      capture_output=True).stdout
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            (root / "nalar.json").write_text(json.dumps({"model": "fixture"}))
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            head = git("rev-parse", "HEAD").decode().strip()
            store = SessionStore(sessions)
            session = store.create(root, "fixture", head)

            class Stream(io.StringIO):
                def isatty(self):
                    return True

            class Stub:
                captured = None

                def __init__(self, *args, **kwargs):
                    Stub.captured = (args, kwargs)

                def run(self):
                    return None

            env = {"NALAR_API_KEY": "fixture-key", "NALAR_SESSIONS": str(sessions),
                   "NALAR_GLOBAL_CONFIG": str(root / "missing.json")}
            with patch.dict(os.environ, env), patch("nalar.tui.NalarApp", Stub), \
                    patch("sys.stdin", Stream()), patch("sys.stdout", Stream()), \
                    patch("sys.argv", ["nalar", "tui", "--repo", str(root), "--continue"]):
                code = main()
            self.assertEqual(code, 0)
            self.assertEqual(Stub.captured[1]["session"].id, session.id)

            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "later")
            with patch.dict(os.environ, env), patch("nalar.tui.NalarApp", Stub), \
                    patch("sys.stdin", Stream()), patch("sys.stdout", Stream()), \
                    patch("sys.argv", ["nalar", "tui", "--repo", str(root), "--continue"]):
                code = main()
            self.assertEqual(code, 0)
            self.assertIsNone(Stub.captured[1]["session"])

            for argv in (["--no-sessions", "--continue"],
                         ["--resume", session.id, "--continue"]):
                with patch.dict(os.environ, env), patch("nalar.tui.NalarApp", Stub), \
                        patch("sys.stdin", Stream()), patch("sys.stdout", Stream()), \
                        patch("sys.argv", ["nalar", "tui", "--repo", str(root), *argv]), \
                        self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)

    async def test_prompt_text_is_not_dispatched_as_a_command_in_the_tui(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            repo = Repository(root)
            repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
            calls = []

            def chat(messages, tools):
                calls.append(messages[-1]["content"])
                return Reply(text="ok")

            prompt = Prompt("srv__reset", "Reset", (), lambda args: "/reset")
            app = NalarApp(repo, "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                self.assertEqual(app.query_one("#composer", TextArea).text, "/reset")
                app.action_send_task()
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
            self.assertEqual(calls[0], "/reset")
            self.assertIn("app.py", repo.patch())

    async def test_cancelling_a_prompt_clears_its_provenance(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            calls = []

            def chat(messages, tools):
                calls.append(1)
                return Reply(text="ok")

            prompt = Prompt("srv__reset", "Reset", (), lambda args: "/reset")
            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                app.action_cancel()
                self.assertEqual(app.query_one("#composer", TextArea).text, "")
                app.query_one("#composer", TextArea).load_text("/help")
                app.action_send_task()
                await pilot.pause(0.2)
            self.assertEqual(calls, [])

    async def test_a_file_reference_keeps_prompt_provenance(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            calls = []

            def chat(messages, tools):
                calls.append(messages[-1]["content"])
                return Reply(text="ok")

            prompt = Prompt("srv__reset", "Reset", (), lambda args: "/reset ")
            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                app._file_selected("app.py")
                app.action_send_task()
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
            self.assertEqual(calls, ["/reset @app.py"])

    async def test_a_rejected_submit_keeps_prompt_provenance(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            calls = []

            def chat(messages, tools):
                calls.append(messages[-1]["content"])
                return Reply(text="ok")

            prompt = Prompt("srv__reset", "Reset", (), lambda args: "/reset ")
            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                app.query_one("#composer", TextArea).insert("x" * MAX_PROMPT)
                app.action_send_task()
                self.assertTrue(app.composer_from_prompt)
                self.assertEqual(calls, [])
                app.query_one("#composer", TextArea).load_text("/reset")
                app.action_send_task()
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
            self.assertEqual(calls, ["/reset"])

    async def test_manual_edits_drop_prompt_provenance(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            calls = []

            def chat(messages, tools):
                calls.append(messages[-1]["content"])
                return Reply(text="ok")

            prompt = Prompt("srv__reset", "Reset", (), lambda args: "/reset ")
            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.3)
                app.query_one("#composer", TextArea).load_text("/help")
                app.action_send_task()
                await pilot.pause(0.2)
            self.assertEqual(calls, [])

    async def test_prompt_arguments_are_collected_before_rendering(self):
        from nalar.extensions import Prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")

            received = []
            prompt = Prompt("srv__review", "Review one file",
                            ({"name": "focus", "required": True},),
                            lambda args: received.append(dict(args)) or f"Review {args['focus']}.")

            app = NalarApp(Repository(root), "fixture",
                           lambda model: (lambda messages, tools: Reply(text="ok")))
            async with app.run_test(size=(100, 30)) as pilot:
                app._prompt_chosen(prompt)
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, PromptArguments)
                app.screen.query_one("#arg-0", Input).value = "security"
                app.screen.query_one("#confirm").press()
                composer = app.query_one("#composer", TextArea)
                for _ in range(60):
                    if "Review security." in composer.text:
                        break
                    await pilot.pause(0.05)
                self.assertIn("Review security.", composer.text)
                self.assertEqual(received, [{"focus": "security"}])

    async def test_prompt_commands_never_run_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            from nalar.commands import Command

            command = Command("audit", "Audit", "Audit $ARGUMENTS.", root / "audit.md")
            app = NalarApp(Repository(root), "fixture", None, commands=(command,))
            async with app.run_test(size=(100, 30)) as pilot:
                app._slash("/audit src/app.py")
                await pilot.pause(0.1)
                self.assertFalse(app.busy)
                self.assertIsNone(app.cancel_event)

    async def test_discard_clears_undo_and_revision_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                                        "after": "42"}),))
                return Reply(text="ok")

            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("one")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertTrue(app.history)
                app._command_selected("discard")
                self.assertEqual((app.repo.patch(), app.history, app.previous), ("", [], []))

    async def test_small_terminal_opens_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)

            git("init", "-q", "--template=")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "--allow-empty", "-qm", "fixture")
            app = NalarApp(Repository(tmp), "fixture", None)
            async with app.run_test(size=(64, 21)) as pilot:
                self.assertFalse(app.query_one("#sidebar").display)
                self.assertTrue(app.query_one("#send", Button).disabled)
                await pilot.press("ctrl+g")
                self.assertFalse(app.busy)

    async def test_submit_review_cancel_then_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            git("init", "-q", "--template=")
            path = root / "app.py"
            path.write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")

            def chat(messages, _):
                if len(messages) == 2:
                    return Reply(calls=(
                        Call("replace", {"path": "app.py", "before": "41", "after": "42"}),
                        Call("create_file", {"path": "notes.txt", "text": "Reviewed\n"}),
                    ))
                return Reply(text="Proposed.")

            app = NalarApp(Repository(root), "fixture", lambda model: chat)
            async with app.run_test(size=(100, 30)) as pilot:
                app.query_one("#composer", TextArea).load_text("Fix the answer")
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertIn("+answer = 42", app.repo.patch())
                await pilot.press("ctrl+p")
                self.assertTrue(app.screen.query_one("#commands"))
                await pilot.press("escape")
                await pilot.press("ctrl+b")
                self.assertTrue(app.query_one("#sidebar").display)
                await pilot.press("ctrl+r")
                self.assertIn("app.py", str(app.screen.query_one("#file-status").render()))
                await pilot.press("n")
                self.assertIn("notes.txt", str(app.screen.query_one("#file-status").render()))
                await pilot.press("p")
                await pilot.press("escape")
                self.assertEqual(path.read_text(), "answer = 41\n")
                await pilot.click("#review")
                await pilot.click("#confirm")
                self.assertEqual(path.read_text(), "answer = 42\n")
                self.assertEqual(app.repo.patch(), "")

    async def test_review_proposal_uses_the_configured_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            seen = []

            def reviewer():
                def chat(messages, tools):
                    seen.append(messages[0]["content"])
                    return Reply(text=json.dumps([{
                        "file": "app.py", "line": 1, "severity": "high",
                        "title": "Reviewer", "detail": "From the reviewer."}]))
                return chat

            app = NalarApp(
                Repository(root), "fixture",
                lambda model: (lambda messages, tools: Reply(text="primary")),
                review_factory=reviewer, review_model="reviewer")
            async with app.run_test(size=(100, 30)) as pilot:
                app.repo.call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"})
                app.action_review_proposal()
                async with asyncio.timeout(5):
                    while app.last_review is None:
                        await pilot.pause(0.03)
            self.assertTrue(seen)
            self.assertEqual(app.last_review["model"], "reviewer")
            self.assertEqual(app.last_review["findings"][0]["title"], "Reviewer")

    async def test_search_and_resume_a_saved_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            repository = Repository(root)
            store = SessionStore(root / "sessions")
            saved = store.create(repository.root, "fixture",
                                 repository.commit.decode())
            store.append(saved, "user", "Fix the parser")
            app = NalarApp(
                repository, "fixture",
                lambda model: (lambda messages, tools: Reply(text="ok")),
                store=store)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_search_sessions("parser")
                async with asyncio.timeout(5):
                    while app.busy or app.last_search is None:
                        await pilot.pause(0.03)
                self.assertEqual([item["id"] for item in app.last_search],
                                 [saved.id])
                self.assertIn("Fix the parser",
                              app.last_search[0]["matches"][0]["snippet"])
                app.action_resume(saved.id)
                for _ in range(40):
                    if app.session is not None and app.session.id == saved.id:
                        break
                    await pilot.pause(0.03)
            self.assertEqual(app.session.id, saved.id)
            self.assertIn("Fix the parser", app.session.entries[0]["text"])

    async def test_review_uses_the_configured_model_after_a_model_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            seen = []

            def factory(model):
                seen.append(model)

                def chat(messages, tools):
                    return Reply(text=json.dumps([{
                        "file": "app.py", "line": 1, "severity": "high",
                        "title": "Reviewer", "detail": "From fixture."}]))
                return chat

            app = NalarApp(Repository(root), "primary", factory,
                           review_model="fixture")
            async with app.run_test(size=(100, 30)) as pilot:
                app.repo.call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"})
                app.model = "other"
                app.action_review_proposal()
                async with asyncio.timeout(5):
                    while app.last_review is None:
                        await pilot.pause(0.03)
            self.assertEqual(seen, ["fixture"])
            self.assertEqual(app.last_review["model"], "fixture")

    async def test_command_menu_search_prefills_the_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            app = NalarApp(Repository(root), "fixture",
                           lambda model: (lambda messages, tools: Reply(text="ok")))
            async with app.run_test(size=(100, 30)) as pilot:
                app._command_selected("search")
                await pilot.pause(0.05)
                composer = app.query_one("#composer", TextArea)
                self.assertEqual(composer.text, "/search ")
                composer.insert("parser")
                self.assertEqual(composer.text, "/search parser")

    async def test_resume_rejects_foreign_stale_and_invalid_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            repository = Repository(root)
            store = SessionStore(root / "sessions")
            head = repository.commit.decode()
            foreign = store.create("/other/repo", "fixture", head)
            stale = store.create(repository.root, "fixture", "deadbeef")
            app = NalarApp(
                repository, "fixture",
                lambda model: (lambda messages, tools: Reply(text="ok")),
                store=store)
            async with app.run_test(size=(100, 30)) as pilot:
                current = app.session.id
                for session_id in (foreign.id, stale.id, "not-a-session"):
                    app.action_resume(session_id)
                    await pilot.pause(0.05)
                    self.assertEqual(app.session.id, current)

    async def test_model_switch_updates_the_subagent_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True)
            git("init", "-q", "--template=")
            (root / "app.py").write_text("answer = 41\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            holder = {"value": "launch"}
            app = NalarApp(Repository(root), "launch",
                           lambda model: (lambda messages, tools: Reply(text="ok")),
                           subagent_model=holder)
            async with app.run_test(size=(100, 30)) as pilot:
                app._set_model("other")
                await pilot.pause(0.05)
            self.assertEqual(holder["value"], "other")

    def test_review_fields_escape_newlines_and_default_ignorables(self):
        from nalar.tui import _one

        text = _one("a\nb\u2028c\U000e0041d\u00ade")
        self.assertIn("a\\nb", text)
        for bad in ("\u2028", "\U000e0041", "\u00ad"):
            self.assertNotIn(bad, text)

    async def test_prompt_picker_does_not_parse_rich_markup(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            extensions = SimpleNamespace(
                belt=SimpleNamespace(names=(), specs=()), specs=[], clients=(),
                system=None, skills=(), prompts=(
                    SimpleNamespace(name="srv__p[/b]", description="x[/b]", arguments=()),),
                warnings=(), instructions=(), subagent_usage=[])
            app = NalarApp(Repository(root), "fixture", None, extensions=extensions)
            async with app.run_test(size=(100, 30)) as pilot:
                app.action_prompts()
                await pilot.pause()
                self.assertTrue(app.is_running)

    async def test_prompt_arguments_escape_metadata_and_required_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30), notifications=True) as pilot:
                prompt = SimpleNamespace(
                    name="p\x1b]0;pwn\x07\nFORGED",
                    description="d\nFORGED",
                    arguments=({"name": "x[/b]", "required": True},))
                screen = PromptArguments(prompt)
                app.push_screen(screen)
                await pilot.pause()
                shown = " ".join(str(widget.render()) for widget in screen.query(Static))
                self.assertNotIn("\x1b", shown)
                self.assertIn("\\n", shown)
                await pilot.click("#confirm")
                await pilot.pause()
                self.assertTrue(app.is_running)

    async def test_prompt_content_is_sanitized_in_the_composer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                prompt = SimpleNamespace(
                    name="p\nFORGED",
                    fetch=lambda arguments: "task\x1b]0;pwn\x07\nline\u202e",
                    arguments=())
                app._fetch_prompt(prompt, {})
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                text = app.query_one("#composer", TextArea).text
                self.assertNotIn("\x1b", text)
                self.assertNotIn("\u202e", text)
                self.assertIn("task", text)

    async def test_command_menu_and_model_picker_are_blocked_while_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", lambda model: None,
                           models_fetcher=lambda: ["m"])
            async with app.run_test(size=(100, 30)) as pilot:
                app.busy = True
                app.action_commands()
                await pilot.pause()
                self.assertIs(app.screen, app.screen_stack[0])
                app.action_model()
                await pilot.pause()
                self.assertIs(app.screen, app.screen_stack[0])
                self.assertTrue(app.busy)

    async def test_task_is_blocked_under_a_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            calls = []
            app = NalarApp(Repository(root), "fixture",
                           lambda model: (lambda messages, tools: (calls.append(1),
                                                                  Reply(text="ok"))[1]))
            async with app.run_test(size=(100, 30)) as pilot:
                app.push_screen(CommandMenu())
                await pilot.pause()
                app.query_one("#composer", TextArea).load_text("task")
                app.action_send_task()
                await pilot.pause()
                self.assertFalse(app.busy)
                self.assertEqual(calls, [])

    async def test_undo_is_blocked_while_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.history = [({}, [])]
                app.busy = True
                app.action_undo()
                await pilot.pause()
                self.assertEqual(len(app.history), 1)

    async def test_control_heavy_prompt_is_refused_after_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                prompt = SimpleNamespace(name="p", fetch=lambda arguments: "\x01" * 2000,
                                         arguments=())
                app._fetch_prompt(prompt, {})
                async with asyncio.timeout(5):
                    while app.busy:
                        await pilot.pause(0.03)
                self.assertEqual(app.query_one("#composer", TextArea).text, "")

    async def test_model_change_and_undo_apply_are_blocked_under_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q", "--template="], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=F", "-c", "user.email=f@e.test",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
            app = NalarApp(Repository(root), "fixture", None)
            async with app.run_test(size=(100, 30)) as pilot:
                app.busy = True
                self.assertFalse(app._set_model("beta"))
                self.assertEqual(app.model, "fixture")
                app.busy = False
                app.applied = True
                app.push_screen(CommandMenu())
                await pilot.pause()
                with patch.object(app, "_undo_apply") as worker:
                    app.action_undo_apply()
                    worker.assert_not_called()
                self.assertTrue(app.applied)
