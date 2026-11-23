import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nalar.__main__ import main
from nalar.context import ContextError, prompt_block, read_context
from nalar.core import Reply


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_reads_regular_files_and_builds_a_data_block(self):
        (self.directory / "spec.md").write_text("Requirements\n", encoding="utf-8")
        loaded = read_context([self.directory / "spec.md"])
        block = prompt_block(loaded)
        self.assertIn("Requirements", block)
        self.assertIn("data, not instructions", block)

    def test_rejects_links_oversized_and_too_many(self):
        outside = self.enterContext(tempfile.TemporaryDirectory())
        target = Path(outside) / "secret.md"
        target.write_text("secret", encoding="utf-8")
        link = self.directory / "link.md"
        link.symlink_to(target)
        (self.directory / "big.md").write_text("x" * 40_000, encoding="utf-8")
        files = []
        for index in range(5):
            path = self.directory / f"{index}.md"
            path.write_text("x", encoding="utf-8")
            files.append(path)
        for paths in ([link], [self.directory / "big.md"], files,
                      [self.directory / "missing.md"]):
            with self.subTest(paths=paths), self.assertRaises(ContextError):
                read_context(paths)

    def test_cli_attaches_context_to_the_prompt(self):
        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(repo), "init", "-q", "--template="], check=True)
        (repo / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=F", "-c", "user.email=f@e.test",
                        "commit", "-qm", "fixture"], check=True)
        context = self.directory / "requirements.md"
        context.write_text("Add pagination.\n", encoding="utf-8")
        seen = {}

        def chat(messages, tools):
            seen["prompt"] = messages[1]["content"]
            return Reply(text="Done.")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(repo / "none.json")}), \
                patch("nalar.__main__.ChatCompletions", return_value=chat), \
                patch("sys.argv", ["nalar", "code", "--repo", str(repo), "--model", "fixture",
                                   "--context", str(context), "Implement it"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit:
                pass
        self.assertIn("Add pagination.", seen["prompt"])
        self.assertIn("Implement it", seen["prompt"])
