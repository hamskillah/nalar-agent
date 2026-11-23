import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository, apply_reviewed
from nalar.workspace import ToolError


class DeleteToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("answer = 41\n", encoding="utf-8")
        (self.root / "README.md").write_text("demo\nrun\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_delete_is_staged_and_exported_as_a_deletion(self):
        result = self.repo.call("delete_file", {"path": "README.md"})
        self.assertEqual(result, {"deleted": "README.md"})
        self.assertIsNone(self.repo.changed["README.md"])
        patch = self.repo.patch()
        self.assertIn("deleted file mode 100644", patch)
        self.assertIn("--- a/README.md", patch)
        self.assertIn("+++ /dev/null", patch)
        self.assertIn("-demo", patch)
        self.assertIn("-run", patch)

    def test_deleted_files_disappear_from_reads_and_listings(self):
        self.repo.call("delete_file", {"path": "README.md"})
        paths = self.repo.call("list_files", {"prefix": "", "offset": 0})["paths"]
        self.assertNotIn("README.md", paths)
        self.assertIn("src/app.py", paths)
        with self.assertRaises(ToolError):
            self.repo.call("read_lines", {"path": "README.md", "start": 1, "end": 1})

    def test_deleting_a_created_file_unstages_it(self):
        self.repo.call("create_file", {"path": "src/new.py", "text": "x = 1\n"})
        result = self.repo.call("delete_file", {"path": "src/new.py"})
        self.assertEqual(result, {"unstaged": "src/new.py"})
        self.assertEqual(self.repo.changed, {})
        self.assertEqual(self.repo.originals, {})
        self.assertEqual(self.repo.patch(), "")

    def test_delete_undo_keeps_earlier_staged_edits(self):
        self.repo.call("replace", {"path": "src/app.py", "before": "41", "after": "42"})
        self.repo.call("delete_file", {"path": "src/app.py"})
        restored = self.repo.call("delete_file", {"path": "src/app.py"})
        self.assertEqual(restored, {"restored": "src/app.py"})
        self.assertEqual(self.repo.changed["src/app.py"], "answer = 42\n")
        self.assertIn("+answer = 42", self.repo.patch())

    def test_delete_can_be_undone_or_recreated(self):
        self.repo.call("delete_file", {"path": "README.md"})
        restored = self.repo.call("delete_file", {"path": "README.md"})
        self.assertEqual(restored, {"restored": "README.md"})
        self.assertEqual(self.repo.changed, {})
        self.assertEqual(self.repo.patch(), "")
        self.repo.call("delete_file", {"path": "README.md"})
        staged = self.repo.call("create_file", {"path": "README.md",
                                               "text": "new\n"})
        self.assertEqual(staged, {"staged": "README.md"})
        self.assertEqual(self.repo.changed["README.md"], "new\n")
        self.assertIn("+new", self.repo.patch())

    def test_delete_uses_the_edit_limit_and_path_validation(self):
        self.repo.max_edits = 0
        with self.assertRaisesRegex(ToolError, "edit_limit"):
            self.repo.call("delete_file", {"path": "README.md"})
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("delete_file", {"path": "../outside"})

    def test_apply_and_revert_restore_a_deleted_file(self):
        self.repo.call("delete_file", {"path": "README.md"})
        patch = self.repo.patch()
        applied = apply_reviewed(self.repo, patch)
        self.assertTrue(applied["applied"])
        self.assertFalse((self.root / "README.md").exists())
        reverted = apply_reviewed(self.repo, patch, reverse=True)
        self.assertTrue(reverted["reversed"])
        self.assertEqual((self.root / "README.md").read_text(), "demo\nrun\n")

    def test_executable_mode_survives_apply_and_revert(self):
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(script, 0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "run.sh"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "exec"],
                       check=True)
        repo = Repository(self.root)
        repo.call("delete_file", {"path": "run.sh"})
        patch = repo.patch()
        self.assertIn("deleted file mode 100755", patch)
        apply_reviewed(repo, patch)
        self.assertFalse(script.exists())
        apply_reviewed(repo, patch, reverse=True)
        self.assertTrue(script.exists())
        self.assertTrue(script.stat().st_mode & 0o111)
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                capture_output=True).stdout
        self.assertEqual(status, b"")

    def test_restore_invalidates_the_symbol_cache(self):
        (self.root / "src" / "mod.py").write_text(
            "def unique_helper():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "module"],
                       check=True)
        repo = Repository(self.root)
        self.assertTrue(repo.call("find_symbol", {"query": "unique_helper"})["symbols"])
        repo.call("delete_file", {"path": "src/mod.py"})
        self.assertEqual(repo.call("find_symbol", {"query": "unique_helper"})["symbols"], [])
        repo.restore({}, {})
        self.assertTrue(repo.call("find_symbol", {"query": "unique_helper"})["symbols"])

    def test_apply_rejects_a_deletion_when_the_file_changed_after_the_snapshot(self):
        self.repo.call("delete_file", {"path": "README.md"})
        patch = self.repo.patch()
        (self.root / "README.md").write_text("local edit\n")
        with self.assertRaises(ValueError):
            apply_reviewed(self.repo, patch)
        self.assertTrue((self.root / "README.md").exists())
