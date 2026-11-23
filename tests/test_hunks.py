"""Hunk splitting and selection used by the TUI review screen."""

import unittest

from nalar.hunks import all_hunks, render, split

DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n-two\n+TWO\n"
    " three\n"
    "@@ -20,3 +20,3 @@\n"
    " twenty\n-old\n+NEW\n"
    " end\n"
    "diff --git a/docs.md b/docs.md\n"
    "--- a/docs.md\n+++ b/docs.md\n"
    "@@ -1 +1 @@\n"
    "-before\n+after\n"
)


class HunkTests(unittest.TestCase):
    def test_split_finds_paths_and_hunks(self):
        files = split(DIFF)
        self.assertEqual([item["path"] for item in files], ["app.py", "docs.md"])
        self.assertEqual([len(item["hunks"]) for item in files], [2, 1])
        self.assertTrue(files[0]["hunks"][0][0].startswith("@@ -1,3"))

    def test_all_hunks_covers_every_section(self):
        files = split(DIFF)
        self.assertEqual(all_hunks(files), {(0, 0), (0, 1), (1, 0)})

    def test_render_keeps_only_selected_hunks(self):
        files = split(DIFF)
        patch = render(files, {(0, 1), (1, 0)})
        self.assertIn("+NEW", patch)
        self.assertNotIn("+TWO", patch)
        self.assertIn("+after", patch)
        self.assertIn("diff --git a/app.py", patch)

    def test_dropping_every_hunk_drops_the_file_section(self):
        files = split(DIFF)
        patch = render(files, {(1, 0)})
        self.assertNotIn("app.py", patch)
        self.assertIn("docs.md", patch)
        self.assertEqual(render(files, set()), "")

    def test_hunkless_section_needs_an_explicit_file_selection(self):
        binary = (
            "diff --git a/blob.bin b/blob.bin\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/blob.bin\n"
            "GIT binary patch\nliteral 3\nKcmZ?0Y00000\n")
        files = split(binary)
        self.assertEqual(files[0]["hunks"], [])
        self.assertIn((0, None), all_hunks(files))
        self.assertEqual(render(files, set()), "")
        self.assertIn("blob.bin", render(files, {(0, None)}))

    def test_mode_only_section_uses_whole_file_selection(self):
        diff = ("diff --git a/app.py b/app.py\n"
                "old mode 100644\nnew mode 100755\n")
        files = split(diff)
        self.assertEqual(files[0]["path"], "app.py")
        self.assertEqual(files[0]["hunks"], [])
        self.assertEqual(render(files, set()), "")
        rendered = render(files, all_hunks(files))
        self.assertIn("old mode 100644", rendered)
        self.assertIn("new mode 100755", rendered)

    def test_mode_change_is_selectable_separately_from_content(self):
        diff = ("diff --git a/app.py b/app.py\n"
                "old mode 100644\n"
                "new mode 100755\n"
                "index 1111111..2222222\n"
                "--- a/app.py\n+++ b/app.py\n"
                "@@ -1 +1 @@\n"
                "-answer = 41\n+answer = 42\n")
        files = split(diff)
        self.assertEqual(files[0]["mode"],
                         ["old mode 100644\n", "new mode 100755\n"])
        self.assertEqual(files[0]["hunks"], [[
            "@@ -1 +1 @@\n", "-answer = 41\n", "+answer = 42\n"]])
        self.assertEqual(all_hunks(files), {(0, 0), (0, "mode")})
        both = render(files, {(0, 0), (0, "mode")})
        self.assertIn("+answer = 42", both)
        self.assertIn("new mode 100755", both)
        mode_only = render(files, {(0, "mode")})
        self.assertIn("old mode 100644", mode_only)
        self.assertIn("new mode 100755", mode_only)
        self.assertNotIn("+answer = 42", mode_only)
        self.assertNotIn("--- a/app.py", mode_only)
        content_only = render(files, {(0, 0)})
        self.assertNotIn("new mode", content_only)
        self.assertNotIn("old mode", content_only)
        self.assertIn("+answer = 42", content_only)
        self.assertEqual(render(files, set()), "")

    def test_unified_diff_without_git_headers_splits_at_hunks(self):
        plain = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
        files = split(plain)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "app.py")
        self.assertEqual(render(files, {(0, 0)}), plain)
