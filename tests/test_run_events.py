import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.core import Call, ProviderError, Reply


class RunEventTests(unittest.TestCase):
    def test_run_honours_the_token_budget(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            return Reply(text="done", tokens=2)

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(workspace / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "run", "--model", "fixture", "--workspace",
                                   str(workspace), "--token-budget", "1", "task"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        payload = json.loads(out.getvalue())
        self.assertEqual((code, payload["status"]), (1, "token_limit"))
        self.assertEqual(len(attempts), 1)

    def test_run_honours_the_retry_limit(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            raise ProviderError("transport_unavailable")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key",
                                     "NALAR_GLOBAL_CONFIG": str(workspace / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "run", "--model", "fixture", "--workspace",
                                   str(workspace), "--retries", "1", "task"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 1)
        self.assertEqual(len(attempts), 2)

    def test_run_writes_events_and_keeps_json_on_stdout(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        events = workspace / "events.ndjson"

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("create", {"name": "note.txt", "text": "hi"}),))
            return Reply(text="Done.")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "fixture-key"}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "run", "--model", "fixture", "--workspace",
                                   str(workspace), "--events", str(events), "task"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "ok")
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertIn({"type": "tool", "name": "create", "status": "ok"}, lines)
        self.assertEqual(lines[-1]["type"], "final")
        self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)
