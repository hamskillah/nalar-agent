import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.workspace import MAX_BYTES, TOOLS, ToolError, Workspace


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.enterContext(Workspace(self.root))

    def test_tool_specs_expose_the_full_workspace_set(self):
        self.assertEqual({spec["function"]["name"] for spec in TOOLS},
                         {"list", "read", "create", "search", "replace",
                          "delete", "rename"})

    def test_large_directories_still_find_the_readable_files(self):
        for index in range(1_030):
            (self.root / f".hidden-{index}").write_text("ignored", encoding="utf-8")
        (self.root / "note.txt").write_text("kept", encoding="utf-8")
        self.assertEqual(self.workspace.call("list", {}), ["note.txt"])

    def test_search_replace_delete_and_rename(self):
        self.workspace.call("create", {"name": "a.txt", "text": "hello\nworld\n"})
        self.workspace.call("create", {"name": "b.txt", "text": "hello\n"})
        found = self.workspace.call("search", {"query": "hello"})
        self.assertEqual(len(found["matches"]), 2)
        self.assertFalse(found["truncated"])
        self.workspace.call("replace", {"name": "a.txt", "before": "world",
                                        "after": "there"})
        self.assertEqual(self.workspace.call("read", {"name": "a.txt"}),
                         "hello\nthere\n")
        self.workspace.call("rename", {"name": "a.txt", "new_name": "c.txt"})
        self.assertEqual(self.workspace.call("list", {}), ["b.txt", "c.txt"])
        self.workspace.call("delete", {"name": "b.txt"})
        self.assertEqual(self.workspace.call("list", {}), ["c.txt"])
        self.assertEqual(list(self.root.glob(".nalar-*")), [])

    def test_replace_fails_closed(self):
        self.workspace.call("create", {"name": "a.txt", "text": "word word\n"})
        for args, code in (
            ({"name": "a.txt", "before": "word", "after": "no"}, "ambiguous_match"),
            ({"name": "a.txt", "before": "missing", "after": "no"}, "no_match"),
            ({"name": "a.txt", "before": "", "after": "no"}, "empty_match"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ToolError, code):
                self.workspace.call("replace", args)

    def test_rename_and_delete_reject_collisions_and_bad_names(self):
        self.workspace.call("create", {"name": "a.txt", "text": "a"})
        self.workspace.call("create", {"name": "b.txt", "text": "b"})
        with self.assertRaisesRegex(ToolError, "already_exists"):
            self.workspace.call("rename", {"name": "a.txt", "new_name": "b.txt"})
        with self.assertRaisesRegex(ToolError, "invalid_name"):
            self.workspace.call("rename", {"name": "a.txt", "new_name": "a.txt"})
        with self.assertRaisesRegex(ToolError, "invalid_name"):
            self.workspace.call("delete", {"name": "../outside"})
        with self.assertRaisesRegex(ToolError, "not_found"):
            self.workspace.call("delete", {"name": "missing.txt"})
        self.assertEqual(self.workspace.call("list", {}), ["a.txt", "b.txt"])

    def test_symlinks_and_hardlinks_are_not_tools(self):
        self.workspace.call("create", {"name": "real.txt", "text": "private"})
        (self.root / "link.txt").symlink_to("real.txt")
        self.assertEqual(self.workspace.call("list", {}), ["real.txt"])
        for tool in ("read", "delete"):
            with self.assertRaises(ToolError):
                self.workspace.call(tool, {"name": "link.txt"})
        os.link(self.root / "real.txt", self.root / "hard.txt")
        self.assertEqual(self.workspace.call("list", {}), [])
        for name in ("real.txt", "hard.txt"):
            for tool in ("read", "delete"):
                with self.subTest(name=name, tool=tool), \
                        self.assertRaises(ToolError):
                    self.workspace.call(tool, {"name": name})

    def test_replace_preserves_the_file_mode(self):
        self.workspace.call("create", {"name": "run.sh", "text": "#!/bin/sh\n"})
        os.chmod(self.root / "run.sh", 0o755)
        self.workspace.call("replace", {"name": "run.sh", "before": "sh",
                                        "after": "bash"})
        self.assertEqual(os.stat(self.root / "run.sh").st_mode & 0o777, 0o755)

    def test_search_preview_keeps_the_match_and_truncation_is_exact(self):
        line = "a" * 300 + "NEEDLE" + "b" * 10
        self.workspace.call("create", {"name": "long.txt", "text": line + "\n"})
        found = self.workspace.call("search", {"query": "NEEDLE"})
        self.assertIn("NEEDLE", found["matches"][0]["text"])
        self.workspace.call("create", {"name": "exact.txt", "text": "hit\n" * 30})
        exact = self.workspace.call("search", {"query": "hit"})
        self.assertEqual(len(exact["matches"]), 30)
        self.assertFalse(exact["truncated"])
        self.workspace.call("create", {"name": "more.txt", "text": "hit\n"})
        more = self.workspace.call("search", {"query": "hit"})
        self.assertTrue(more["truncated"])
        self.assertEqual(len(more["matches"]), 30)

    def test_search_skips_binary_files_instead_of_aborting(self):
        self.workspace.call("create", {"name": "good.txt", "text": "NEEDLE\n"})
        (self.root / "image.png").write_bytes(b"\xff\xfe")
        found = self.workspace.call("search", {"query": "NEEDLE"})
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["skipped"], 1)

    def test_rename_falls_back_without_hardlink_support(self):
        self.workspace.call("create", {"name": "a.txt", "text": "x"})
        with patch("nalar.workspace.os.link", side_effect=OSError(errno.EPERM, "no")):
            result = self.workspace.call("rename", {"name": "a.txt",
                                                    "new_name": "b.txt"})
        self.assertEqual(result, {"renamed": {"from": "a.txt", "to": "b.txt"}})
        self.assertEqual(self.workspace.call("list", {}), ["b.txt"])

    def test_large_files_can_be_deleted_and_renamed(self):
        (self.root / "big.txt").write_text("x" * (MAX_BYTES + 1), encoding="utf-8")
        self.assertEqual(self.workspace.call("delete", {"name": "big.txt"}),
                         {"deleted": "big.txt"})
        (self.root / "big2.txt").write_text("x" * (MAX_BYTES + 1), encoding="utf-8")
        self.workspace.call("rename", {"name": "big2.txt", "new_name": "big3.txt"})
        self.assertTrue((self.root / "big3.txt").exists())

    def test_replace_refuses_a_file_that_changed_during_the_edit(self):
        self.workspace.call("create", {"name": "race.txt", "text": "old\n"})
        original = self.workspace._read

        def racing_read(name):
            text = original(name)
            (self.root / name).write_text(text + "other\n", encoding="utf-8")
            return text

        with patch.object(self.workspace, "_read", side_effect=racing_read):
            with self.assertRaisesRegex(ToolError, "file_changed"):
                self.workspace.call("replace", {"name": "race.txt",
                                                "before": "old", "after": "new"})

    def test_search_and_replace_respect_bounds(self):
        text = "a" * (MAX_BYTES - 4) + "TAIL"
        self.workspace.call("create", {"name": "a.txt", "text": text})
        with self.assertRaisesRegex(ToolError, "invalid_query"):
            self.workspace.call("search", {"query": "x\ny"})
        with self.assertRaisesRegex(ToolError, "file_too_large"):
            self.workspace.call("replace", {"name": "a.txt", "before": "TAIL",
                                            "after": "b" * (MAX_BYTES + 1)})
