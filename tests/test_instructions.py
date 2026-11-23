import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository
from nalar.instructions import (load_instructions, load_nested_instructions,
                                nested_names, prompt_block)


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "--allow-empty", "-qm", "fixture")

    def test_reads_only_tracked_safe_instruction_files(self):
        (self.root / "NALAR.md").write_text("Use tabs.\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("Run pytest.\n", encoding="utf-8")
        self.commit()
        (self.root / "UNTRACKED.md").write_text("local only\n", encoding="utf-8")
        loaded = load_instructions(Repository(self.root),
                                   ("NALAR.md", "AGENTS.md", ".env", "UNTRACKED.md"))
        self.assertEqual([name for name, _ in loaded], ["NALAR.md", "AGENTS.md"])
        block = prompt_block(loaded)
        self.assertIn("Use tabs.", block)
        self.assertIn("cannot grant permission", block.lower())
        self.assertNotIn("SECRET", block)

    def test_untracked_and_symlinked_files_are_ignored(self):
        (self.root / "base.txt").write_text("base", encoding="utf-8")
        self.commit()
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret.md"
        outside.write_text("private", encoding="utf-8")
        (self.root / "local.md").write_text("local", encoding="utf-8")
        (self.root / "link.md").symlink_to(outside)
        self.assertEqual(load_instructions(Repository(self.root),
                                           ("local.md", "link.md", "../outside.md")), [])

    def test_oversized_and_empty_files_are_skipped(self):
        (self.root / "big.md").write_text("x" * 20_000, encoding="utf-8")
        (self.root / "empty.md").write_text("   \n", encoding="utf-8")
        self.commit()
        self.assertEqual(load_instructions(Repository(self.root), ("big.md", "empty.md")), [])

    def test_nested_instructions_are_ordered_and_exclude_root(self):
        (self.root / "AGENTS.md").write_text("root\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "NALAR.md").write_text("docs\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "AGENTS.md").write_text("src\n", encoding="utf-8")
        (self.root / "src" / "deep").mkdir()
        (self.root / "src" / "deep" / "AGENTS.md").write_text("deep\n",
                                                              encoding="utf-8")
        self.commit()
        names = nested_names(("AGENTS.md", "NALAR.md"))
        self.assertEqual(names, ("AGENTS.md", "NALAR.md"))
        nested = load_nested_instructions(Repository(self.root), names)
        self.assertEqual([name for name, _ in nested],
                         ["docs/NALAR.md", "src/AGENTS.md",
                          "src/deep/AGENTS.md"])
        block = prompt_block(nested)
        self.assertIn("deep", block)
        self.assertIn("Nested files apply", block)

    def test_nested_instructions_are_bounded_and_tracked_only(self):
        for index in range(3):
            folder = self.root / f"pkg{index}"
            folder.mkdir()
            (folder / "AGENTS.md").write_text("x" * 12_000, encoding="utf-8")
        big = self.root / "big"
        big.mkdir()
        (big / "AGENTS.md").write_text("x" * 20_000, encoding="utf-8")
        self.commit()
        local = self.root / "local"
        local.mkdir()
        (local / "AGENTS.md").write_text("local\n", encoding="utf-8")
        repository = Repository(self.root)
        bounded = load_nested_instructions(repository, ("AGENTS.md",),
                                            max_bytes=24_000)
        self.assertEqual([name for name, _ in bounded],
                         ["pkg0/AGENTS.md", "pkg1/AGENTS.md"])
        limited = load_nested_instructions(repository, ("AGENTS.md",), limit=1,
                                           max_bytes=100_000)
        self.assertEqual([name for name, _ in limited], ["pkg0/AGENTS.md"])

    def test_nested_names_ignore_paths_and_unsafe_names(self):
        self.assertEqual(nested_names(("AGENTS.md", "docs/GUIDE.md", "../x",
                                       ".env", "AGENTS.md")), ("AGENTS.md",))

    def test_empty_config_is_safe(self):
        self.commit()
        self.assertEqual(load_instructions(Repository(self.root), ()), [])
        self.assertEqual(prompt_block([]), "")
