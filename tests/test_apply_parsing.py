"""End-to-end evidence that filtering never applies an unselected file."""

import difflib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ApplyParsingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        self.files = {
            "keep.txt": "keep\n",
            "drop.txt": "drop\n",
            "dir b/secret.env": "secret\n",
            "caf\u00e9.md": "cafe\n",
            "notes.md": "prefix\n-- old\nsuffix\n",
            "new": "unrelated\n",
        }
        for name, text in self.files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True).stdout

    def edit(self, **changes):
        for name, text in changes.items():
            (self.repo / name).write_text(text, encoding="utf-8")

    def restore(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", "."], check=True)

    def git_diff(self):
        text = self.git("diff", "--no-color", "--no-ext-diff").decode("utf-8")
        self.restore()
        return text

    def plain_diff(self, **changes):
        parts = []
        for name, text in changes.items():
            original = self.files[name]
            parts.extend(difflib.unified_diff(
                original.splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}"))
        return "".join(parts)

    def apply(self, patch, *args):
        target = self.outside / "change.patch"
        target.write_text(patch, encoding="utf-8")
        return subprocess.run([sys.executable, "-m", "nalar", "apply", "--repo",
                               str(self.repo), *args, str(target)],
                              cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def changed(self):
        out = self.git("-c", "core.quotePath=false", "status", "--porcelain=v1").decode("utf-8")
        return sorted(line[3:] for line in out.splitlines() if line[3:])

    def test_git_style_filters_apply_only_selected_files(self):
        self.edit(**{"keep.txt": "KEEP\n", "drop.txt": "DROP\n",
                     "caf\u00e9.md": "CAFE\n", "dir b/secret.env": "SECRET\n"})
        patch = self.git_diff()
        result = self.apply(patch, "--include", "keep.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"], ["keep.txt"])
        self.assertEqual(self.changed(), ["keep.txt"])
        self.assertEqual((self.repo / "keep.txt").read_text(encoding="utf-8"), "KEEP\n")

    def test_plain_diff_with_header_like_content_cannot_redirect_a_filter(self):
        self.edit(**{"notes.md": "prefix\n++ new\nsuffix\n", "keep.txt": "KEEP\n"})
        patch = self.plain_diff(**{"notes.md": "prefix\n++ new\nsuffix\n",
                                   "keep.txt": "KEEP\n"})
        self.assertIn("--- old", patch)
        self.assertIn("+++ new", patch)
        self.restore()
        result = self.apply(patch, "--exclude", "notes.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"], ["keep.txt"])
        self.assertEqual(self.changed(), ["keep.txt"])
        self.assertEqual((self.repo / "notes.md").read_text(encoding="utf-8"),
                         "prefix\n-- old\nsuffix\n")
        self.assertEqual((self.repo / "new").read_text(encoding="utf-8"), "unrelated\n")

    def test_quoted_and_space_paths_filter_correctly(self):
        self.edit(**{"caf\u00e9.md": "CAFE\n", "dir b/secret.env": "SECRET\n"})
        patch = self.git_diff()
        result = self.apply(patch, "--include", "caf*")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"], ["caf\u00e9.md"])
        self.assertEqual(self.changed(), ["caf\u00e9.md"])

        self.restore()
        result = self.apply(patch, "--exclude", "dir b/secret.env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"], ["caf\u00e9.md"])
        self.assertEqual(self.changed(), ["caf\u00e9.md"])

    def test_unresolvable_paths_are_refused_before_any_write(self):
        patch = "diff --git x y\nindex 1..2\n"
        result = self.apply(patch, "--exclude", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be resolved", result.stderr.lower())
        self.assertEqual(self.changed(), [])
