import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository, apply_reviewed
from nalar.workspace import ToolError


class ChmodToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        (self.root / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(self.root / "run.sh", 0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_mode_only_patch_applies_and_reverts(self):
        result = self.repo.call("chmod_file", {"path": "app.py",
                                               "executable": True})
        self.assertEqual(result["mode"], "100755")
        patch = self.repo.patch()
        self.assertIn("old mode 100644", patch)
        self.assertIn("new mode 100755", patch)
        self.assertEqual(self.repo.call("staged_diff", {})["files"], ["app.py"])
        self.assertTrue(apply_reviewed(self.repo, patch)["applied"])
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        self.assertTrue(apply_reviewed(self.repo, patch, reverse=True)["reversed"])
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_chmod_can_clear_the_executable_bit(self):
        self.repo.call("chmod_file", {"path": "run.sh", "executable": False})
        patch = self.repo.patch()
        self.assertIn("old mode 100755", patch)
        self.assertIn("new mode 100644", patch)
        apply_reviewed(self.repo, patch)
        self.assertFalse((self.root / "run.sh").stat().st_mode & 0o111)
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertTrue((self.root / "run.sh").stat().st_mode & 0o111)

    def test_mode_and_content_changes_share_one_patch(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        patch = self.repo.patch()
        self.assertIn("new mode 100755", patch)
        self.assertIn("+answer = 42", patch)
        apply_reviewed(self.repo, patch)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_chmod_then_rename_keeps_the_mode(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("rename_file", {"path": "app.py", "new_path": "main.py"})
        patch = self.repo.patch()
        self.assertIn("rename from app.py", patch)
        self.assertIn("new mode 100755", patch)
        apply_reviewed(self.repo, patch)
        target = self.root / "main.py"
        self.assertTrue(target.exists())
        self.assertTrue(target.stat().st_mode & 0o111)
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertTrue((self.root / "app.py").exists())
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_invalid_and_noop_mode_requests(self):
        self.assertEqual(self.repo.call("chmod_file", {
            "path": "app.py", "executable": False}), {"unchanged": "app.py"})
        with self.assertRaisesRegex(ToolError, "invalid_arguments"):
            self.repo.call("chmod_file", {"path": "app.py", "executable": "yes"})
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("chmod_file", {"path": "../outside", "executable": True})

    def test_chmod_respects_the_edit_limit(self):
        self.repo.max_edits = 0
        with self.assertRaisesRegex(ToolError, "edit_limit"):
            self.repo.call("chmod_file", {"path": "app.py", "executable": True})

    def test_toggling_the_mode_back_removes_every_staged_trace(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        result = self.repo.call("chmod_file", {"path": "app.py", "executable": False})
        self.assertEqual(result, {"unchanged": "app.py"})
        self.assertEqual(self.repo.patch(), "")
        self.assertEqual(self.repo.call("staged_diff", {})["files"], [])
        self.assertEqual(self.repo.mode_changes, {})
        self.repo.call("replace", {"path": "run.sh", "before": "#!/bin/sh",
                                   "after": "#!/bin/sh\n# run"})
        patch = self.repo.patch()
        self.assertNotIn("old mode", patch)
        self.assertIn("+# run", patch)
        apply_reviewed(self.repo, patch)

    def test_toggle_back_does_not_burn_an_edit_slot(self):
        self.repo.max_edits = 1
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("chmod_file", {"path": "app.py", "executable": False})
        self.repo.call("replace", {"path": "run.sh", "before": "#!/bin/sh",
                                   "after": "#!/bin/sh\n# run"})
        self.assertIn("+# run", self.repo.patch())

    def test_toggling_back_after_an_edit_keeps_the_text_change(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        self.repo.call("chmod_file", {"path": "app.py", "executable": False})
        patch = self.repo.patch()
        self.assertNotIn("old mode", patch)
        self.assertIn("+answer = 42", patch)
        apply_reviewed(self.repo, patch)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_delete_and_restore_preserve_a_staged_mode(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("delete_file", {"path": "app.py"})
        self.repo.call("delete_file", {"path": "app.py"})
        patch = self.repo.patch()
        self.assertIn("new mode 100755", patch)
        self.assertEqual(self.repo.call("staged_diff", {})["files"], ["app.py"])
        apply_reviewed(self.repo, patch)
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_rename_back_preserves_a_staged_mode(self):
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("rename_file", {"path": "app.py", "new_path": "main.py"})
        self.repo.call("rename_file", {"path": "main.py", "new_path": "app.py"})
        patch = self.repo.patch()
        self.assertIn("old mode 100644", patch)
        self.assertIn("new mode 100755", patch)
        self.assertNotIn("rename from", patch)
        self.assertEqual(self.repo.call("staged_diff", {})["files"], ["app.py"])
        apply_reviewed(self.repo, patch)
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_snapshot_and_restore_keep_and_clear_the_mode(self):
        snapshot = self.repo.snapshot()
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.restore(snapshot)
        self.assertEqual(self.repo.patch(), "")
        self.assertEqual(self.repo.mode_changes, {})
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.restore({}, {})
        self.assertEqual(self.repo.patch(), "")
        self.assertEqual(self.repo.mode_changes, {})

    def test_an_already_staged_path_may_be_chmodded_at_the_limit(self):
        self.repo.max_edits = 1
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.assertIn("new mode 100755", self.repo.patch())
        with self.assertRaisesRegex(ToolError, "edit_limit"):
            self.repo.call("chmod_file", {"path": "run.sh", "executable": False})

    def test_a_mode_only_selection_applies_without_the_content_hunk(self):
        from nalar import hunks

        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        files = hunks.split(self.repo.patch())
        patch = hunks.render(files, {(0, "mode")})
        self.assertNotIn("+answer = 42", patch)
        apply_reviewed(self.repo, patch)
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        apply_reviewed(self.repo, patch, reverse=True)
        self.assertFalse((self.root / "app.py").stat().st_mode & 0o111)

    def test_mode_only_sections_survive_include_and_exclude(self):
        from nalar.apply import filter_patch

        self.repo.call("chmod_file", {"path": "app.py", "executable": True})
        self.repo.call("replace", {"path": "run.sh", "before": "#!/bin/sh",
                                   "after": "#!/bin/sh\n# run"})
        patch = self.repo.patch()
        only_app = filter_patch(patch, include=["app.py"])
        self.assertIn("new mode 100755", only_app)
        self.assertNotIn("# run", only_app)
        apply_reviewed(self.repo, only_app)
        self.assertTrue((self.root / "app.py").stat().st_mode & 0o111)
        self.assertNotIn("# run", (self.root / "run.sh").read_text())
