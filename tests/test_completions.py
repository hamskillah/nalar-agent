"""Shell completion scripts stay in sync with the real CLI."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.completions import (FLAGS, FLAG_VALUES, GLOBAL_FLAGS, SUBCOMMANDS, VALUES,
                               bash, fish, zsh, flag_values)

ROOT = Path(__file__).resolve().parents[1]


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "--template="], check=True)

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "missing.json")}
        return subprocess.run([sys.executable, "-m", "nalar", *args], cwd=ROOT, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def test_every_script_lists_every_subcommand(self):
        for shell in (bash, zsh, fish):
            script = shell()
            for name in SUBCOMMANDS:
                self.assertIn(name, script)

    def test_subcommands_match_the_help_output(self):
        help_text = self.cli("--help").stdout
        for name in SUBCOMMANDS:
            self.assertIn(name, help_text, f"{name} is missing from --help")

    def test_help_flags_are_all_in_the_table(self):
        import re

        self.assertEqual(set(FLAGS), set(SUBCOMMANDS))
        for name, flags in FLAGS.items():
            output = self.cli(name, "--help").stdout
            declared = set()
            for line in output.splitlines():
                if not line.startswith(("  -", "  --")):
                    continue
                declared.update(re.findall(r"--[a-z0-9][a-z0-9-]*", line))
            declared.discard("--help")
            self.assertEqual(declared - set(flags), set(), name)

    def test_flags_match_the_help_output(self):
        for name, flags in FLAGS.items():
            if not flags:
                continue
            result = self.cli(name, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            for flag in flags:
                with self.subTest(command=name, flag=flag):
                    self.assertIn(flag, result.stdout)

    def test_global_flags_are_offered(self):
        help_text = self.cli("--help").stdout
        for flag in GLOBAL_FLAGS:
            self.assertIn(flag, help_text)
        for shell in (bash, zsh, fish):
            script = shell()
            for flag in GLOBAL_FLAGS:
                self.assertIn(flag.lstrip("-"), script)

    def test_scripts_complete_flags(self):
        for shell in (bash, zsh, fish):
            script = shell()
            self.assertIn("include-untracked", script)
            self.assertIn("review-fix-attempts", script)
            self.assertIn("max-turns", script)

    def test_positional_values_match_the_help_output(self):
        import re

        pattern = re.compile(r"\{([a-z0-9,]+)\}")
        for name, values in VALUES.items():
            result = self.cli(name, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            groups = {frozenset(group.split(",")) for group in pattern.findall(result.stdout)}
            self.assertIn(frozenset(values), groups, name)

    def test_scripts_offer_positional_values(self):
        for shell in (bash, zsh, fish):
            script = shell()
            self.assertIn("prune", script)
            self.assertIn("resources", script)

    def test_flag_values_match_the_help_output(self):
        import re

        pattern = re.compile(r"\{([a-z0-9,]+)\}")
        cases = {"--scope": "review", "--fail-on": "review", "--format": "review",
                 "--mode": "code", "--review-fail-on": "code", "--review-fix-on": "code"}
        for flag, command in cases.items():
            result = self.cli(command, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            groups = {frozenset(group.split(",")) for group in pattern.findall(result.stdout)}
            self.assertIn(frozenset(flag_values(command, flag)), groups, flag)

    def test_review_format_can_offer_sarif_markdown_and_github(self):
        self.assertEqual(flag_values("review", "--format"),
                         ("json", "text", "sarif", "markdown", "github"))
        self.assertEqual(flag_values("scan", "--format"),
                         ("json", "text", "sarif", "markdown", "github"))
        self.assertEqual(flag_values("apply", "--format"), ("json", "text"))
        for shell in (bash, zsh, fish):
            self.assertIn("sarif", shell())
            self.assertIn("markdown", shell())
            self.assertIn("github", shell())

    def test_scripts_offer_flag_values(self):
        for shell in (bash, zsh, fish):
            script = shell()
            self.assertIn("worktree", script)
            self.assertIn("recover", script)
        self.assertNotIn("run:--format", bash())
        self.assertNotIn("apply:--mode", zsh())

    def test_bash_script_parses(self):
        bash_path = shutil.which("bash")
        if bash_path is None:
            self.skipTest("bash is unavailable")
        script = Path(self.enterContext(tempfile.TemporaryDirectory())) / "nalar.bash"
        script.write_text(bash(), encoding="utf-8")
        result = subprocess.run([bash_path, "-n", str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_zsh_script_only_runs_inside_a_completion(self):
        script = zsh()
        self.assertIn('if [ "$funcstack[1]" = "_nalar" ]; then', script)
        self.assertIn('elif (( $+functions[compdef] )); then', script)
        self.assertIn("compdef _nalar nalar", script)
        zsh_path = shutil.which("zsh")
        if zsh_path is None:
            self.skipTest("zsh is unavailable")
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "nalar.zsh"
        target.write_text(script, encoding="utf-8")
        result = subprocess.run([zsh_path, "-f", "-c", f"source {target}"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_prints_a_script_and_rejects_unknown_shells(self):
        for shell in ("bash", "zsh", "fish"):
            result = self.cli("completions", shell)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nalar", result.stdout)
        bad = self.cli("completions", "tcsh")
        self.assertEqual(bad.returncode, 2)
