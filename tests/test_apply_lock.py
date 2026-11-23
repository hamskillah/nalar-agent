import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository, apply_reviewed


class ApplyLockTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def test_stale_lock_refuses_apply_and_is_removed_after_success(self):
        repo = Repository(self.root)
        repo.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        diff = repo.patch()
        lock = Path(repo._git("rev-parse", "--git-path", "nalar-apply.lock")
                    .decode().strip())
        if not lock.is_absolute():
            lock = self.root / lock
        lock.write_text("999999 stale")
        with self.assertRaises(ValueError) as caught:
            apply_reviewed(repo, diff)
        self.assertIn("Another apply", str(caught.exception))
        self.assertEqual((self.root / "app.py").read_text(), "answer = 41\n")
        lock.unlink()

        repo2 = Repository(self.root)
        repo2.call("replace", {"path": "app.py", "before": "41", "after": "42"})
        apply_reviewed(repo2, repo2.patch())
        self.assertEqual((self.root / "app.py").read_text(), "answer = 42\n")
        self.assertFalse(lock.exists())
