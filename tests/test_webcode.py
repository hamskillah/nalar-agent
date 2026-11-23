import contextlib
import http.client
import json
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

from nalar.__main__ import main
from nalar.core import Call, Reply
from nalar.webcode import make_server


class CodingWebTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="],
                       check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "fixture"],
                       check=True)
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def start_repo(self, repo, factory, **kwargs):
        server = self.enterContext(make_server(self.state, repo, port=0,
                                               chat_factory=factory, **kwargs))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_port

    def start(self, factory, **kwargs):
        self.port = self.start_repo(self.root, factory, **kwargs)

    def request_at(self, port, method, path, *, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            conn.request(method, path, body=data, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def request(self, method, path, *, data=None, headers=None):
        return self.request_at(self.port, method, path, data=data, headers=headers)

    def csrf_at(self, port):
        _, _, page = self.request_at(port, "GET", "/")
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def csrf(self):
        return self.csrf_at(self.port)

    def submit(self, fields, *, origin=None):
        return self.request("POST", "/runs", data=urlencode({"csrf": self.csrf(), **fields}),
                            headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Origin": origin or f"http://127.0.0.1:{self.port}"})

    def test_serve_repo_cli_wires_the_coding_server(self):
        class Fake:
            server_port = 1234

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def serve_forever(self):
                raise KeyboardInterrupt

        output = io.StringIO()
        with patch("nalar.webcode.make_server", return_value=Fake()) as made, \
                patch.dict(os.environ, {"NALAR_GLOBAL_CONFIG": str(self.root / "none.json"),
                                        "NALAR_API_KEY": ""}), \
                patch("sys.argv", ["nalar", "serve", "--repo", str(self.root),
                                   "--state", str(self.state), "--port", "0"]), \
                contextlib.redirect_stdout(output):
            code = main()
        self.assertEqual(code, 0)
        self.assertIn("Nalar Coding", output.getvalue())
        args, kwargs = made.call_args
        self.assertEqual(args[1], self.root)
        self.assertIsNone(kwargs["chat_factory"])
        self.assertIn("max_turns", kwargs["limits"])
        self.assertEqual(kwargs["verify_commands"], ())
        self.assertEqual(kwargs["secret_env"], ("NALAR_API_KEY",))
        self.assertEqual(kwargs["verify_timeout"], 300)

    def test_runs_are_scoped_to_the_served_repository(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "REPO-A-RUN", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        clone = Path(self.enterContext(tempfile.TemporaryDirectory())) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.root), str(clone)], check=True)
        port_b = self.start_repo(clone, factory)
        _, _, home = self.request_at(port_b, "GET", "/")
        self.assertNotIn("REPO-A-RUN", home)
        csrf = self.csrf_at(port_b)
        status, _, _ = self.request_at(
            port_b, "POST", location + "/apply",
            data=urlencode({"csrf": csrf}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{port_b}"})
        self.assertEqual(status, 404)
        self.assertEqual((clone / "app.py").read_text(), "answer = 41\n")

    def test_subdirectory_repo_is_resolved_to_toplevel(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(root), "init", "-q", "--template="], check=True)
        (root / "pkg").mkdir()
        (root / "pkg" / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "fixture"],
                       check=True)

        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "pkg/app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        port = self.start_repo(root / "pkg", factory)
        self.port = port
        status, headers, _ = self.submit({"prompt": "Subdir", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        applied = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": self.csrf()}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((root / "pkg" / "app.py").read_text(), "answer = 42\n")

    def test_unexpected_solve_error_returns_500_and_recovers(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        with patch("nalar.webcode.solve", side_effect=RuntimeError("boom")):
            status, _, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                        "mode": "recover"})
        self.assertEqual(status, 500)
        status, _, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                    "mode": "recover"})
        self.assertEqual(status, 303)

    def test_patch_post_is_not_an_apply_alias_and_second_apply_is_idempotent(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        csrf = self.csrf()
        post_patch = self.request("POST", location + "/patch",
                                  data=urlencode({"csrf": csrf}),
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(post_patch[0], 404)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        for _ in range(2):
            applied = self.request("POST", location + "/apply",
                                   data=urlencode({"csrf": csrf}),
                                   headers={"Content-Type": "application/x-www-form-urlencoded",
                                            "Origin": f"http://127.0.0.1:{self.port}"})
            self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

    def test_empty_patch_has_no_apply_action(self):
        self.start(lambda model: (lambda *_: Reply(text="nothing to do")))
        status, headers, _ = self.submit({"prompt": "Noop", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        page = self.request("GET", headers["Location"])[2]
        self.assertNotIn("Terapkan patch", page)
        self.assertIn("Tidak ada patch", page)
        applied = self.request("POST", headers["Location"] + "/apply",
                               data=urlencode({"csrf": self.csrf()}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_run_shows_patch_and_applies_it(self):
        def factory(model):
            self.assertEqual(model, "fixture")
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        status, _, page = self.request("GET", location)
        self.assertEqual(status, 200)
        self.assertIn("answer = 41", page)
        self.assertIn("answer = 42", page)
        self.assertIn("done", page)
        applied = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": self.csrf()}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        self.assertIn("Diterapkan", self.request("GET", location)[2])

    def test_patch_download_and_head_move_refusal(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        patch = self.request("GET", location + "/patch")
        self.assertEqual(patch[0], 200)
        self.assertIn("+answer = 42", patch[2])
        self.assertTrue(patch[1]["Content-Disposition"].startswith("attachment;"))
        (self.root / "other.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "moved"],
                       check=True)
        page = self.request("GET", location)[2]
        self.assertIn("Base commit sudah bergeser", page)
        self.assertNotIn("Terapkan patch", page)
        refused = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": self.csrf()}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(refused[0], 409)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_apply_can_run_configured_verification(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory, verify_commands=((sys.executable, "-c",
                                              "import app; assert app.answer == 42"),))
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        detail = self.request("GET", location)[2]
        self.assertIn("Jalankan verifikasi setelah apply", detail)
        self.assertIn("import app; assert app.answer == 42", detail)
        applied = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": self.csrf(), "verify": "1"}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        page = self.request("GET", location)[2]
        self.assertIn("Verification passed.", page)

    def test_secret_like_patches_need_explicit_web_confirmation(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {
                    "path": "app.py", "before": "41",
                    "after": "42\nAWS_KEY = \"AKIAABCDEFGHIJKLMNOP\""}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Secret", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        page = self.request("GET", location)[2]
        self.assertIn("CREDENTIALS", page)
        self.assertIn("allow_secrets", page)
        self.assertIn("aws_access_key", page)
        refused = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(refused[0], 409)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        applied = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf(), "allow_secrets": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertIn("AKIA", (self.root / "app.py").read_text())

    def test_fix_after_failed_verification_reverts_and_stores_a_new_patch(self):
        replies = iter([
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"}),)),
            Reply(text="done"),
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "43"}),)),
            Reply(text="fixed"),
        ])

        def factory(model):
            return lambda *_: next(replies)

        self.start(factory, verify_commands=(
            (sys.executable, "-c", "import app; assert app.answer == 43"),))
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        applied = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf(), "verify": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        page = self.request("GET", location)[2]
        self.assertIn("Verification failed", page)
        self.assertIn("usulkan perbaikan", page)

        fixed = self.request(
            "POST", location + "/fix",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(fixed[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        page = self.request("GET", location)[2]
        self.assertIn("+answer = 43", page)
        self.assertIn("Verification passed", page)
        self.assertIn("Terapkan patch", page)
        self.assertNotIn("usulkan perbaikan", page)
        applied_new = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied_new[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 43\n")

    def test_fix_failure_with_blocked_restore_keeps_the_db_truthful(self):
        replies = iter([
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"}),)),
            Reply(text="done"),
            Reply(text="cannot fix"),
        ])

        def factory(model):
            return lambda *_: next(replies)

        self.start(factory, verify_commands=(
            (sys.executable, "-c", "import app; assert app.answer == 43"),))
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        applied = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf(), "verify": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        (self.root / "blocker.txt").write_text("local\n", encoding="utf-8")
        fixed = self.request(
            "POST", location + "/fix",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(fixed[0], 409)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        self.assertTrue((self.root / "blocker.txt").exists())
        page = self.request("GET", location)[2]
        self.assertNotIn("Patch sudah diterapkan", page)
        self.assertIn("Terapkan patch", page)
        self.assertNotIn("usulkan perbaikan", page)

    def test_fix_refuses_a_dirty_tree_and_can_retry(self):
        (self.root / "other.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "other.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "other"],
                       check=True)
        replies = iter([
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "42"}),)),
            Reply(text="done"),
            Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                          "after": "43"}),)),
            Reply(text="fixed"),
        ])

        def factory(model):
            return lambda *_: next(replies)

        self.start(factory, verify_commands=(
            (sys.executable, "-c", "import app; assert app.answer == 43"),))
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        applied = self.request(
            "POST", location + "/apply",
            data=urlencode({"csrf": self.csrf(), "verify": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        (self.root / "other.py").write_text("value = 2\n", encoding="utf-8")
        refused = self.request(
            "POST", location + "/fix",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(refused[0], 409)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        (self.root / "other.py").write_text("value = 1\n", encoding="utf-8")
        fixed = self.request(
            "POST", location + "/fix",
            data=urlencode({"csrf": self.csrf()}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(fixed[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_apply_then_revert_restores_the_tree(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        csrf = self.csrf()
        applied = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": csrf}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        reverted = self.request("POST", location + "/revert",
                                data=urlencode({"csrf": csrf}),
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(reverted[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        page = self.request("GET", location)[2]
        self.assertIn("Terapkan patch", page)
        again = self.request("POST", location + "/revert",
                             data=urlencode({"csrf": csrf}),
                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                      "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(again[0], 303)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")

    def test_review_patch_from_web(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])

            def chat(messages, tools):
                if messages[0]["content"].startswith("You review code changes"):
                    return Reply(text=json.dumps([{
                        "file": "app.py", "line": 1, "severity": "high",
                        "title": "Bug", "detail": "Fix it."}]))
                return next(replies)

            return chat

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        reviewed = self.request("POST", location + "/review",
                                data=urlencode({"csrf": self.csrf()}),
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(reviewed[0], 303)
        page = self.request("GET", location)[2]
        self.assertIn("[high] app.py:1 Bug", page)
        self.assertIn("Fix it.", page)

    def test_review_can_use_a_separate_review_factory(self):
        seen = []

        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda messages, tools: next(replies)

        def reviewer():
            def chat(messages, tools):
                seen.append(messages[0]["content"])
                return Reply(text=json.dumps([{
                    "file": "app.py", "line": 1, "severity": "high",
                    "title": "Reviewer bug", "detail": "From the reviewer."}]))
            return chat

        self.start(factory, review_factory=reviewer, review_model="reviewer")
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        reviewed = self.request("POST", location + "/review",
                                data=urlencode({"csrf": self.csrf()}),
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(reviewed[0], 303)
        self.assertEqual(len(seen), 1)
        page = self.request("GET", location)[2]
        self.assertIn("[high] app.py:1 Reviewer bug", page)
        self.assertIn("From the reviewer.", page)

    def test_failed_verification_is_shown_after_apply(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory, verify_commands=((sys.executable, "-c", "raise SystemExit(3)"),))
        status, headers, _ = self.submit({"prompt": "Bump", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        location = headers["Location"]
        applied = self.request("POST", location + "/apply",
                               data=urlencode({"csrf": self.csrf(), "verify": "1"}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(applied[0], 303)
        page = self.request("GET", location)[2]
        self.assertIn("Verification failed.", page)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")

    def test_prompt_and_patch_are_html_escaped(self):
        (self.root / "app.py").write_text(
            "# <img src=x onerror=alert(1)>\nanswer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "xss"],
                       check=True)

        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="<script>never</script>"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({"prompt": "<svg onload=alert(1)>",
                                          "model": "fixture", "mode": "recover"})
        self.assertEqual(status, 303)
        page = self.request("GET", headers["Location"])[2]
        self.assertIn("&lt;svg", page)
        self.assertIn("&lt;img", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<svg onload", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>never", page)

    def test_query_strings_and_content_type_fail_closed(self):
        self.start(None)
        status, _, page = self.request("GET", "/?x=1")
        self.assertEqual(status, 200)
        self.assertIn('name="csrf"', page)
        _, _, page = self.request("GET", "/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        wrong = self.request(
            "POST", "/runs?ignored=1",
            data=json.dumps({"csrf": csrf}),
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(wrong[0], 415)

    def test_security_and_validation_fail_closed(self):
        self.start(lambda model: (lambda *_: Reply(text="ok")))
        self.assertEqual(self.request("GET", "/", headers={"Host": "bad.test"})[0], 403)
        self.assertEqual(self.submit({"prompt": "x", "model": "fixture",
                                      "mode": "recover"}, origin="https://bad.test")[0], 403)
        fields = {"prompt": "x", "model": "fixture", "mode": "recover"}
        no_csrf = self.request("POST", "/runs", data=urlencode(fields),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(no_csrf[0], 403)
        self.assertEqual(self.submit({**fields, "model": "bad model"})[0], 400)
        self.assertEqual(self.request("GET", "/runs/" + "a" * 32)[0], 404)

    def test_dirty_worktree_refuses_apply_and_no_key_never_runs(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"path": "app.py", "before": "41",
                                             "after": "42"}),)),
                Reply(text="done"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        (self.root / "local.txt").write_text("dirty\n", encoding="utf-8")
        status, headers, _ = self.submit({"prompt": "x", "model": "fixture",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        refused = self.request("POST", headers["Location"] + "/apply",
                               data=urlencode({"csrf": self.csrf()}),
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(refused[0], 409)
        self.start(None)
        self.assertIn("NALAR_API_KEY", self.request("GET", "/")[2])
        self.assertEqual(self.submit({"prompt": "x", "model": "fixture",
                                      "mode": "recover"})[0], 503)

    def test_home_search_filters_runs_and_escapes_the_query(self):
        def factory(model):
            state = {"n": 0}

            def chat(messages, tools):
                state["n"] += 1
                if state["n"] == 1:
                    return Reply(calls=(Call("replace", {
                        "path": "app.py", "before": "41", "after": "42"}),))
                return Reply(text="done")
            return chat

        self.start(factory)
        first = self.submit({"prompt": "Fix the parser", "model": "fixture",
                             "mode": "recover"})
        self.assertEqual(first[0], 303)
        second = self.submit({"prompt": "Update docs", "model": "fixture",
                              "mode": "recover"})
        self.assertEqual(second[0], 303)
        page = self.request("GET", "/?q=parser")[2]
        self.assertIn("Fix the parser", page)
        self.assertNotIn("Update docs", page)
        self.assertIn('value="parser"', page)
        evil = self.request("GET", "/?q=%3Cscript%3Ealert%281%29")[2]
        self.assertNotIn("<script>alert", evil)
        self.assertIn("&lt;script&gt;", evil)
