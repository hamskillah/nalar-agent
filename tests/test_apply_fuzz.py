"""Seeded differential test: a filtered apply changes exactly the selected files."""

import json
import random
import subprocess
import sys
import tempfile
import unittest
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ApplyFuzzTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)
        self.names = [f"src/file{index}.py" for index in range(8)]
        self.names += [f"dir b/data{index}.txt" for index in range(4)]
        self.names += [f"caf\u00e9{index}.md" for index in range(4)]
        self.names += ["notes.md", "keep.txt", "src/deep/nested.py"]
        for name in self.names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "prefix\n-- old\nsuffix\n" if name == "notes.md" else "value 0\n"
            path.write_text(text, encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "-qm", "base")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True).stdout

    def changed(self):
        out = self.git("-c", "core.quotePath=false", "status", "--porcelain=v1", "-z").decode()
        return sorted(entry[3:] for entry in out.split("\0") if entry)

    def edit_all(self):
        for name in self.names:
            path = self.repo / name
            text = "prefix\n++ new\nsuffix\n" if name == "notes.md" else "value 1\n"
            path.write_text(text, encoding="utf-8")

    def test_random_filters_apply_exactly_the_selected_files(self):
        self.edit_all()
        patch = self.git("diff", "--no-color", "--no-ext-diff").decode("utf-8")
        self.git("checkout", "--", ".")
        self.assertEqual(self.changed(), [])
        target = self.outside / "fuzz.patch"
        target.write_text(patch, encoding="utf-8")

        rng = random.Random(20261121)
        patterns = ["src/*", "dir b/*", "*.md", "caf*", "src/deep/*", "keep.txt", "*"]
        trials = 0
        for _ in range(8):
            include = [rng.choice(patterns)] if rng.random() < 0.5 else []
            exclude = [rng.choice(patterns)] if rng.random() < 0.5 else []
            selected = [name for name in self.names
                        if (not include or any(fnmatch(name, pattern) for pattern in include))
                        and not any(fnmatch(name, pattern) for pattern in exclude)]
            args = [sys.executable, "-m", "nalar", "apply", "--repo", str(self.repo)]
            for pattern in include:
                args += ["--include", pattern]
            for pattern in exclude:
                args += ["--exclude", pattern]
            args.append(str(target))
            result = subprocess.run(args, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            if not selected:
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.changed(), [])
                continue
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(sorted(payload["files"]), sorted(selected))
            self.assertEqual(self.changed(), sorted(selected))
            self.git("checkout", "--", ".")
            trials += 1
        self.assertGreater(trials, 0)
