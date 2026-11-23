import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository, apply_reviewed
from nalar.workspace import ToolError


class RenameToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("answer = 41\n", encoding="utf-8")
        (self.root / "README.md").write_text("demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_rename_stages_a_deletion_and_a_new_file(self):
        result = self.repo.call("rename_file", {"path": "src/app.py",
                                                "new_path": "src/main.py"})
        self.assertEqual(result, {"renamed": {"from": "src/app.py",
                                             "to": "src/main.py"}})
        self.assertIsNone(self.repo.changed["src/app.py"])
        self.assertEqual(self.repo.changed["src/main.py"], "answer = 41\n")
        patch = self.repo.patch()
        self.assertIn("rename from src/app.py", patch)
        self.assertIn("rename to src/main.py", patch)
        self.assertNotIn("deleted file mode", patch)
        self.assertNotIn("new file mode", patch)
        paths = self.repo.call("list_files", {"prefix": "src", "offset": 0})["paths"]
        self.assertEqual(paths, ["src/main.py"])
        self.assertIn("answer = 41",
                      self.repo.call("read_lines",
                                     {"path": "src/main.py", "start": 1, "end": 1})["lines"][0])

    def test_rename_uses_earlier_staged_content(self):
        self.repo.call("replace", {"path": "src/app.py", "before": "41", "after": "42"})
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        self.assertIn("answer = 42", self.repo.patch())

    def test_renaming_a_created_file_just_moves_it(self):
        self.repo.call("create_file", {"path": "src/new.py", "text": "x = 1\n"})
        self.repo.call("rename_file", {"path": "src/new.py", "new_path": "src/old.py"})
        self.assertEqual(self.repo.changed, {"src/old.py": "x = 1\n"})
        self.assertEqual(self.repo.originals, {"src/old.py": None})
        self.assertIn("new file mode", self.repo.patch())

    def test_collisions_and_bad_paths_are_refused(self):
        with self.assertRaisesRegex(ToolError, "already_exists"):
            self.repo.call("rename_file", {"path": "src/app.py",
                                           "new_path": "README.md"})
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("rename_file", {"path": "src/app.py",
                                           "new_path": "src/app.py"})
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("rename_file", {"path": "../outside",
                                           "new_path": "src/main.py"})
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("rename_file", {"path": "src/app.py",
                                           "new_path": "../outside"})
        self.assertEqual(self.repo.changed, {})

    def test_rename_respects_the_edit_limit(self):
        self.repo.max_edits = 1
        with self.assertRaisesRegex(ToolError, "edit_limit"):
            self.repo.call("rename_file", {"path": "src/app.py",
                                           "new_path": "src/main.py"})
        self.assertEqual(self.repo.changed, {})

    def test_executable_mode_survives_rename_apply_and_revert(self):
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(script, 0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "run.sh"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "exec"],
                       check=True)
        repo = Repository(self.root)
        repo.call("rename_file", {"path": "run.sh", "new_path": "bin.sh"})
        patch = repo.patch()
        self.assertIn("rename from run.sh", patch)
        self.assertIn("rename to bin.sh", patch)
        apply_reviewed(repo, patch)
        target = self.root / "bin.sh"
        self.assertTrue(target.exists())
        self.assertTrue(target.stat().st_mode & 0o111)
        apply_reviewed(repo, patch, reverse=True)
        self.assertTrue(script.exists())
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertFalse(target.exists())
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                capture_output=True).stdout
        self.assertEqual(status, b"")

    def test_rename_is_one_section_for_review_and_filters(self):
        from nalar import hunks
        from nalar.apply import filter_patch

        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        patch = self.repo.patch()
        self.assertEqual(len(hunks.split(patch)), 1)
        for selected in (["src/main.py"], ["src/app.py"]):
            kept = filter_patch(patch, include=selected)
            self.assertIn("rename from src/app.py", kept)
            self.assertIn("rename to src/main.py", kept)
        applied = apply_reviewed(self.repo, filter_patch(patch, include=["src/main.py"]))
        self.assertTrue(applied["applied"])
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertTrue((self.root / "src" / "main.py").exists())

    def test_rename_chain_exports_one_final_move(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/b.py"})
        self.repo.call("rename_file", {"path": "src/b.py", "new_path": "src/c.py"})
        patch = self.repo.patch()
        self.assertIn("rename from src/app.py", patch)
        self.assertIn("rename to src/c.py", patch)
        self.assertNotIn("src/b.py", patch)
        applied = apply_reviewed(self.repo, patch)
        self.assertTrue(applied["applied"])
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertFalse((self.root / "src" / "b.py").exists())
        self.assertEqual((self.root / "src" / "c.py").read_text(), "answer = 41\n")

    def test_rename_then_delete_target_leaves_a_deletion(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        self.repo.call("delete_file", {"path": "src/main.py"})
        patch = self.repo.patch()
        self.assertIn("deleted file mode 100644", patch)
        self.assertNotIn("rename from", patch)
        self.assertTrue(apply_reviewed(self.repo, patch)["applied"])
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertFalse((self.root / "src" / "main.py").exists())

    def test_modified_rename_applies_and_reverts(self):
        self.repo.call("replace", {"path": "src/app.py", "before": "41", "after": "42"})
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        patch = self.repo.patch()
        self.assertIn("rename from src/app.py", patch)
        self.assertIn("+answer = 42", patch)
        self.assertTrue(apply_reviewed(self.repo, patch)["applied"])
        self.assertEqual((self.root / "src" / "main.py").read_text(), "answer = 42\n")
        self.assertTrue(apply_reviewed(self.repo, patch, reverse=True)["reversed"])
        self.assertEqual((self.root / "src" / "app.py").read_text(), "answer = 41\n")
        self.assertFalse((self.root / "src" / "main.py").exists())

    def test_rename_then_create_the_old_path(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        self.repo.call("create_file", {"path": "src/app.py", "text": "fresh = True\n"})
        patch = self.repo.patch()
        self.assertIn("+fresh = True", patch)
        self.assertIn("new file mode 100644", patch)
        self.assertIn("+answer = 41", patch)
        self.assertNotIn("rename to src/main.py", patch)
        self.assertTrue(apply_reviewed(self.repo, patch)["applied"])
        self.assertEqual((self.root / "src" / "app.py").read_text(), "fresh = True\n")
        self.assertEqual((self.root / "src" / "main.py").read_text(), "answer = 41\n")

    def test_snapshot_restore_preserves_rename_and_executable_mode(self):
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(script, 0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "run.sh"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "exec"],
                       check=True)
        repo = Repository(self.root)
        repo.call("rename_file", {"path": "run.sh", "new_path": "bin.sh"})
        snapshot = repo.snapshot()
        repo.call("delete_file", {"path": "bin.sh"})
        repo.restore(snapshot)
        patch = repo.patch()
        self.assertIn("rename from run.sh", patch)
        self.assertTrue(apply_reviewed(repo, patch)["applied"])
        target = self.root / "bin.sh"
        self.assertTrue(target.exists())
        self.assertTrue(target.stat().st_mode & 0o111)

    def test_delete_after_rename_removes_the_file(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        result = self.repo.call("delete_file", {"path": "src/app.py"})
        self.assertEqual(result, {"deleted": "src/app.py"})
        patch = self.repo.patch()
        self.assertIn("deleted file mode", patch)
        self.assertNotIn("rename from", patch)
        self.assertTrue(apply_reviewed(self.repo, patch)["applied"])
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertFalse((self.root / "src" / "main.py").exists())

    def test_rename_back_undoes_the_move(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        self.repo.call("rename_file", {"path": "src/main.py", "new_path": "src/app.py"})
        self.assertEqual(self.repo.changed, {})
        self.assertEqual(self.repo.patch(), "")

    def test_rename_to_unrelated_staged_deletion_is_explained(self):
        self.repo.call("delete_file", {"path": "README.md"})
        with self.assertRaisesRegex(ToolError, "path_staged_deleted"):
            self.repo.call("rename_file", {"path": "src/app.py",
                                           "new_path": "README.md"})

    def test_apply_and_revert_a_rename(self):
        self.repo.call("rename_file", {"path": "src/app.py", "new_path": "src/main.py"})
        patch = self.repo.patch()
        applied = apply_reviewed(self.repo, patch)
        self.assertTrue(applied["applied"])
        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertEqual((self.root / "src" / "main.py").read_text(), "answer = 41\n")
        reverted = apply_reviewed(self.repo, patch, reverse=True)
        self.assertTrue(reverted["reversed"])
        self.assertEqual((self.root / "src" / "app.py").read_text(), "answer = 41\n")
        self.assertFalse((self.root / "src" / "main.py").exists())
