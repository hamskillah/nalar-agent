import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository
from nalar.workspace import ToolError


def _diff(*sections):
    return "".join(section.strip("\n") + "\n" for section in sections)


class PatchToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "def answer():\n    return 41\n", encoding="utf-8")
        (self.root / "README.md").write_text("demo\nrun\n", encoding="utf-8")
        (self.root / "dup.py").write_text(
            "value = 1\nvalue = 1\nvalue = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm",
                        "fixture"], check=True)
        self.repo = Repository(self.root)

    def test_multi_file_patch_stages_both_files(self):
        patch = _diff(
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42""",
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 demo
+extra
 run""")
        result = self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(result["staged"], ["src/app.py", "README.md"])
        self.assertEqual(result["hunks"], 2)
        self.assertEqual(self.repo.changed["src/app.py"],
                         "def answer():\n    return 42\n")
        self.assertEqual(self.repo.changed["README.md"], "demo\nextra\nrun\n")
        self.assertIn("+    return 42", self.repo.patch())
        self.assertIn("+extra", self.repo.patch())

    def test_conflicting_context_fails_closed(self):
        patch = _diff(
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 99
+    return 42""")
        with self.assertRaisesRegex(ToolError, "patch_conflict"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_ambiguous_context_is_refused(self):
        patch = _diff(
            """diff --git a/dup.py b/dup.py
--- a/dup.py
+++ b/dup.py
@@ -9 +9 @@
-value = 1
+value = 2""")
        with self.assertRaisesRegex(ToolError, "patch_ambiguous"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_insertion_hunk_adds_lines_after_the_declared_line(self):
        patch = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,0 +2,1 @@
+inserted""")
        self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed["README.md"], "demo\ninserted\nrun\n")

    def test_deletion_hunk_removes_lines(self):
        patch = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1 @@
 demo
-run""")
        self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed["README.md"], "demo\n")

    def test_new_file_and_rename_sections_are_refused(self):
        new_file = _diff(
            """diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello""")
        rename = _diff(
            """diff --git a/README.md b/docs/README.md
similarity index 100%
rename from README.md
rename to docs/README.md""")
        for patch in (new_file, rename):
            with self.subTest(patch=patch.splitlines()[0]), \
                    self.assertRaisesRegex(ToolError, "invalid_patch"):
                self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_unsafe_and_duplicate_paths_are_refused(self):
        unsafe = _diff(
            """diff --git a/../outside.txt b/../outside.txt
--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-a
+b""")
        with self.assertRaisesRegex(ToolError, "invalid_path"):
            self.repo.call("apply_patch", {"patch": unsafe})

        duplicate = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-demo
+DEMO""",
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -2 +2 @@
-run
+ran""")
        with self.assertRaisesRegex(ToolError, "invalid_patch"):
            self.repo.call("apply_patch", {"patch": duplicate})
        self.assertEqual(self.repo.changed, {})

    def test_bad_hunk_counts_are_refused(self):
        patch = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
 demo
-run""")
        with self.assertRaisesRegex(ToolError, "invalid_patch"):
            self.repo.call("apply_patch", {"patch": patch})

    def test_crlf_endings_are_preserved(self):
        (self.root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        subprocess.run(["git", "-C", str(self.root), "add", "crlf.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm",
                        "crlf"], check=True)
        repo = Repository(self.root)
        patch = ("diff --git a/crlf.txt b/crlf.txt\r\n"
                 "--- a/crlf.txt\r\n"
                 "+++ b/crlf.txt\r\n"
                 "@@ -1,2 +1,2 @@\r\n"
                 " one\r\n"
                 "-two\r\n"
                 "+three\r\n")
        repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed["crlf.txt"].encode(), b"one\r\nthree\r\n")

    def test_inserted_lines_keep_the_patch_line_ending(self):
        (self.root / "crlf.txt").write_bytes(b"demo\r\nrun\r\n")
        subprocess.run(["git", "-C", str(self.root), "add", "crlf.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm",
                        "crlf insert"], check=True)
        repo = Repository(self.root)
        patch = ("diff --git a/crlf.txt b/crlf.txt\r\n"
                 "--- a/crlf.txt\r\n"
                 "+++ b/crlf.txt\r\n"
                 "@@ -1,2 +1,3 @@\r\n"
                 " demo\r\n"
                 "+extra\r\n"
                 " run\r\n")
        repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed["crlf.txt"].encode(),
                         b"demo\r\nextra\r\nrun\r\n")

    def test_crlf_patch_adds_lf_lines_to_an_lf_file(self):
        (self.root / "lf2.txt").write_text("demo\nrun\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "lf2.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm",
                        "lf crlf"], check=True)
        repo = Repository(self.root)
        patch = ("diff --git a/lf2.txt b/lf2.txt\r\n"
                 "--- a/lf2.txt\r\n"
                 "+++ b/lf2.txt\r\n"
                 "@@ -1,2 +1,3 @@\r\n"
                 " demo\r\n"
                 "+extra\r\n"
                 " run\r\n")
        repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed["lf2.txt"].encode(),
                         b"demo\nextra\nrun\n")

    def test_lf_patch_adds_crlf_lines_to_a_crlf_file(self):
        (self.root / "crlf2.txt").write_bytes(b"demo\r\nrun\r\n")
        subprocess.run(["git", "-C", str(self.root), "add", "crlf2.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm",
                        "crlf lf"], check=True)
        repo = Repository(self.root)
        patch = _diff(
            """diff --git a/crlf2.txt b/crlf2.txt
--- a/crlf2.txt
+++ b/crlf2.txt
@@ -1,2 +1,3 @@
 demo
+extra
 run""")
        repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed["crlf2.txt"].encode(),
                         b"demo\r\nextra\r\nrun\r\n")

    def test_missing_final_newline_marker_is_honoured(self):
        (self.root / "none.txt").write_text("alpha\nbeta", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "none.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm",
                        "none"], check=True)
        repo = Repository(self.root)
        patch = _diff(
            """diff --git a/none.txt b/none.txt
--- a/none.txt
+++ b/none.txt
@@ -1,2 +1,2 @@
 alpha
-beta
+gamma
\\ No newline at end of file""")
        repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed["none.txt"], "alpha\ngamma")

    def test_interior_no_newline_marker_is_refused(self):
        (self.root / "src" / "app.py").write_text(
            "def answer():\n    return 41\n    # end\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "marker"],
                       check=True)
        repo = Repository(self.root)
        patch = _diff(
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -2 +2 @@
-    return 41
+    return 42
\\ No newline at end of file""")
        with self.assertRaisesRegex(ToolError, "patch_conflict"):
            repo.call("apply_patch", {"patch": patch})
        self.assertEqual(repo.changed, {})

    def test_headers_without_hunks_are_refused(self):
        patch = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md""")
        with self.assertRaisesRegex(ToolError, "invalid_patch"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_two_hunks_in_one_file_apply_bottom_up(self):
        (self.root / "many.txt").write_text(
            "".join(f"line {index}\n" for index in range(10)), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "many.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "many"],
                       check=True)
        repo = Repository(self.root)
        patch = _diff(
            """diff --git a/many.txt b/many.txt
--- a/many.txt
+++ b/many.txt
@@ -1,2 +1,2 @@
 line 0
-line 1
+LINE 1
@@ -9,2 +9,2 @@
 line 8
-line 9
+LINE 9""")
        repo.call("apply_patch", {"patch": patch})
        value = repo.changed["many.txt"]
        self.assertIn("LINE 1\n", value)
        self.assertIn("LINE 9\n", value)
        self.assertIn("line 5\n", value)

    def test_second_file_conflict_leaves_the_first_unstaged(self):
        patch = _diff(
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-demo
+DEMO""",
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -2 +2 @@
-    return 99
+    return 42""")
        with self.assertRaisesRegex(ToolError, "patch_conflict"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_patch_size_limit_has_its_own_error(self):
        patch = "diff --git a/x b/x\n" + "+x\n" * 70_000
        with self.assertRaisesRegex(ToolError, "patch_too_large"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})

    def test_edit_limit_and_size_limits_still_apply(self):
        self.repo.max_edits = 1
        patch = _diff(
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -2 +2 @@
-    return 41
+    return 42""",
            """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-demo
+DEMO""")
        with self.assertRaisesRegex(ToolError, "edit_limit"):
            self.repo.call("apply_patch", {"patch": patch})
        self.assertEqual(self.repo.changed, {})
