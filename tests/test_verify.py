import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.config import ConfigError, load_config
from nalar.verify import run_commands

ROOT = Path(__file__).resolve().parents[1]


class VerifyConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_verify_commands_are_validated(self):
        self.write({"verify": [[sys.executable, "-c", "print('ok')"]]})
        self.assertEqual(load_config(self.root, self.root / "none.json").verify,
                         ((sys.executable, "-c", "print('ok')"),))
        for value in ({"verify": "echo"}, {"verify": [[""]]}, {"verify": [[]]},
                      {"verify": [["a"] * 9]}, {"verify": [["a"]] * 5}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    load_config(self.root, self.root / "none.json")


class VerifyRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_runs_commands_and_reports_output(self):
        result = run_commands([[sys.executable, "-c", "print('hello')"]], self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["commands"][0]["exit"], 0)
        self.assertIn("hello", result["commands"][0]["output"])

    def test_failure_and_missing_command_are_reported(self):
        result = run_commands([[sys.executable, "-c", "import sys; sys.exit(3)"],
                               ["/nonexistent/nalar-command"]], self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["commands"][0]["exit"], 3)
        self.assertIsNone(result["commands"][1]["exit"])

    def test_timeout_kills_the_process_group(self):
        import time
        start = time.monotonic()
        result = run_commands([[sys.executable, "-c", "import time; time.sleep(2)"]],
                              self.root, timeout=0.5)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["commands"][0]["output"])

    def test_a_lingering_child_cannot_hang_the_runner(self):
        import signal
        import time

        pidfile = self.root / "child.pid"
        script = (
            "import subprocess; from pathlib import Path; "
            "child = subprocess.Popen(['sleep', '3'], start_new_session=True); "
            f"Path({str(pidfile)!r}).write_text(str(child.pid)); print('started')")
        started = time.monotonic()
        try:
            result = run_commands([[sys.executable, "-c", script]], self.root, timeout=0.2)
        finally:
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        from nalar.verify import CLEANUP_TIMEOUT

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["commands"][0]["output"])
        self.assertIn("started", result["commands"][0]["output"])
        self.assertLess(time.monotonic() - started, 0.2 + CLEANUP_TIMEOUT + 1.0)

    def test_common_secret_variables_are_removed(self):
        result = run_commands(
            [[sys.executable, "-c",
              "import os; print(os.environ.get('GITHUB_TOKEN'), os.environ.get('PATH') is not None)"]],
            self.root)
        self.assertEqual(result["commands"][0]["output"].strip(), "None True")

    def test_more_secret_names_are_removed_but_plain_variables_survive(self):
        secrets = ("GH_TOKEN", "gh_token", "STRIPE_KEY", "FOO_CREDENTIAL",
                   "DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "MY_SECRET_KEY",
                   "SESSION_ID", "SESSION_COOKIE")
        values = {name: "secret" for name in secrets}
        values["SAFE_VAR"] = "plain"
        with patch.dict(os.environ, values):
            result = run_commands(
                [[sys.executable, "-c",
                  "import os; print(' '.join(str(os.environ.get(k)) "
                  f"for k in {secrets!r}), os.environ.get('SAFE_VAR'), "
                  "os.environ.get('PATH') is not None)"]],
                self.root)
        output = result["commands"][0]["output"].split()
        self.assertEqual(output[:-2], ["None"] * len(secrets))
        self.assertEqual(output[-2], "plain")
        self.assertEqual(output[-1], "True")

    def test_session_desktop_variables_are_not_over_scrubbed(self):
        values = {"XDG_SESSION_TYPE": "tty",
                  "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus"}
        with patch.dict(os.environ, values):
            result = run_commands(
                [[sys.executable, "-c",
                  "import os; print(os.environ.get('XDG_SESSION_TYPE'), "
                  "os.environ.get('DBUS_SESSION_BUS_ADDRESS'))"]],
                self.root)
        self.assertEqual(result["commands"][0]["output"].strip(),
                         "tty unix:path=/run/user/1/bus")

    def test_provider_key_is_not_inherited(self):
        result = run_commands(
            [[sys.executable, "-c", "import os; print(os.environ.get('NALAR_API_KEY'))"]],
            self.root, secret_names=("NALAR_API_KEY",))
        self.assertEqual(result["commands"][0]["output"].strip(), "None")


class VerifyCLITests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json"),
               "NALAR_API_KEY": "should-not-reach-child"}
        return subprocess.run([sys.executable, "-m", "nalar", "verify", "--repo", str(self.repo), *args],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def write(self, value):
        (self.repo / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_requires_confirmation_without_yes(self):
        self.write({"verify": [[sys.executable, "-c", "print('ran')"]]})
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--yes", result.stderr)

    def test_runs_configured_commands_with_yes(self):
        self.write({"verify": [[sys.executable, "-c",
                                "import os; print(os.environ.get('NALAR_API_KEY'))"]]})
        result = self.cli("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["commands"][0]["output"].strip(), "None")

    def test_text_format_is_human_readable(self):
        self.write({"verify": [[sys.executable, "-c", "print('ran')"]]})
        result = self.cli("--yes", "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("$ ", result.stdout)
        self.assertIn("ran", result.stdout)
        self.assertIn("Verification passed.", result.stdout)

    def test_text_reports_a_missing_command(self):
        self.write({"verify": [["/nonexistent/nalar-command"]]})
        result = self.cli("--yes", "--format", "text")
        self.assertEqual(result.returncode, 1)
        self.assertIn("command not found", result.stdout)

    def test_text_distinguishes_a_timeout_from_a_missing_command(self):
        self.write({"verify": [[sys.executable, "-c", "import time; time.sleep(4)"]]})
        result = self.cli("--yes", "--format", "text", "--timeout", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stdout)
        self.assertNotIn("failed to start", result.stdout)

    def test_missing_configuration_is_an_error(self):
        self.write({})
        result = self.cli("--yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("verify", result.stderr)

    def test_cli_approval_escapes_hostile_argv(self):
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch

        from nalar.__main__ import main

        self.write({"verify": [["true", "x\nINJECTED: passed"]]})
        err = io.StringIO()
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="no"), \
                patch("sys.argv", ["nalar", "verify", "--repo", str(self.repo)]), \
                redirect_stderr(err):
            code = main()
        self.assertEqual(code, 2)
        self.assertIn("x\\nINJECTED: passed", err.getvalue())
        self.assertNotIn("\nINJECTED: passed\n", err.getvalue())

    def test_status_text_covers_non_exit_states(self):
        from nalar.verify import status_text

        self.assertEqual(status_text({"status": "timeout"}), "timed out")
        self.assertEqual(status_text({"status": "not_found"}), "command not found")
        self.assertEqual(status_text({"status": "start_failed"}), "failed to start")
        self.assertEqual(status_text({"exit": None}), "did not run")
        self.assertEqual(status_text({"exit": 0}), "exit 0")
        self.assertEqual(status_text({"exit": -9}), "exit -9")
