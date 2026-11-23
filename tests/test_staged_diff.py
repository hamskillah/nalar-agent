import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository


class StagedDiffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_empty_proposal(self):
        result = self.repo.call("staged_diff", {})
        self.assertEqual(result["diff"], "")
        self.assertEqual(result["files"], [])
        self.assertFalse(result["truncated"])

    def test_reports_replace_and_delete(self):
        self.repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        self.repo.call("delete_file", {"path": "app.py"})
        result = self.repo.call("staged_diff", {})
        self.assertEqual(result["files"], ["app.py"])
        self.assertIn("deleted file mode", result["diff"])

    def test_large_proposal_is_truncated(self):
        text = "".join(f"line {index}\n" for index in range(2_000))
        self.repo.call("delete_file", {"path": "app.py"})
        self.repo.call("create_file", {"path": "big.txt", "text": text})
        result = self.repo.call("staged_diff", {})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"]), 16_000)
        self.assertTrue(result["diff"].endswith("\n"))
        self.assertIn("note", result)
