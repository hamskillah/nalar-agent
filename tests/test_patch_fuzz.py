"""Deterministic property test for the staging state machine.

Every staged proposal must be a patch Git accepts, and the files reported by
`staged_diff` must match the patch. Random operation sequences exercise the
interactions between replace/create/delete/rename/chmod and their undo paths.
"""

import random
import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.apply import patch_paths
from nalar.coding import Repository, apply_reviewed
from nalar.workspace import ToolError

SEEDS = range(40)
OPERATIONS = ("replace", "create", "delete", "rename", "chmod", "apply_patch")


class PatchStateFuzzTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.py").write_text("alpha = 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("beta = 2\n", encoding="utf-8")
        (self.root / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        (self.root / "run.sh").chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "fixture"],
                       check=True)

    def check(self, repo, ops):
        patch = repo.patch()
        files = repo.call("staged_diff", {})["files"]
        if not patch:
            self.assertEqual(files, [], ops)
            return
        self.assertEqual(sorted(files), sorted(patch_paths(patch)), ops)
        result = subprocess.run(
            ["git", "-C", str(self.root), "apply", "--check", "-"],
            input=patch.encode("utf-8"), capture_output=True)
        self.assertEqual(result.returncode, 0,
                         f"{ops}\n{patch}\n{result.stderr.decode()}")

    def apply_patch_tool(self, repo, rng):
        patch = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            f"-alpha = {rng.choice([1, 10, 11])}\n"
            f"+alpha = {rng.choice([2, 12, 13])}\n")
        return repo.call("apply_patch", {"patch": patch})

    def test_random_sequences_produce_valid_patches(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            repo = Repository(self.root)
            ops = []
            for _ in range(rng.randint(3, 8)):
                name = rng.choice(OPERATIONS)
                paths = repo.paths()
                if not paths and name != "create":
                    continue
                try:
                    if name == "replace":
                        path = rng.choice(repo.paths())
                        lines = repo.call("read_lines", {"path": path, "start": 1,
                                                         "end": 120})["lines"]
                        line = rng.choice([item for item in lines if item])
                        ops.append((name, path, line))
                        repo.call("replace", {"path": path, "before": line,
                                               "after": line + " "})
                    elif name == "create":
                        path = f"new{rng.randrange(3)}_{rng.randrange(3)}.py"
                        ops.append((name, path))
                        repo.call("create_file", {"path": path,
                                                  "text": f"value = {rng.randrange(9)}\n"})
                    elif name == "delete":
                        path = rng.choice(paths)
                        ops.append((name, path))
                        repo.call("delete_file", {"path": path})
                    elif name == "rename":
                        path = rng.choice(paths)
                        target = f"moved{rng.randrange(4)}.py"
                        ops.append((name, path, target))
                        repo.call("rename_file", {"path": path, "new_path": target})
                    elif name == "chmod":
                        path = rng.choice(paths)
                        flag = rng.choice([True, False])
                        ops.append((name, path, flag))
                        repo.call("chmod_file", {"path": path, "executable": flag})
                    else:
                        ops.append((name,))
                        self.apply_patch_tool(repo, rng)
                except ToolError:
                    pass
                self.check(repo, ops)
            if seed % 8 == 0 and (patch := repo.patch()):
                apply_reviewed(repo, patch)
                dirty = subprocess.run(
                    ["git", "-C", str(self.root), "status", "--porcelain"],
                    capture_output=True, text=True).stdout
                self.assertNotEqual(dirty.strip(), "", ops)
                apply_reviewed(repo, patch, reverse=True,
                               allowed_files=patch_paths(patch))
                clean = subprocess.run(
                    ["git", "-C", str(self.root), "status", "--porcelain"],
                    capture_output=True, text=True).stdout
                self.assertEqual(clean.strip(), "", ops)
