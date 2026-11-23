import unittest

from nalar.coding import repository_map


class RepositoryMapTests(unittest.TestCase):
    def test_groups_directories_and_lists_root_files(self):
        text = repository_map(["README.md", "src/a.py", "src/b.py", "src/deep/c.py", "tests/t.py"])
        self.assertIn("src/ (3 files)", text)
        self.assertIn("tests/ (1 file)", text)
        self.assertIn("README.md", text)

    def test_truncates_large_maps(self):
        entries = [f"pkg{index}/file.py" for index in range(80)]
        text = repository_map(entries, limit=10)
        self.assertLessEqual(len(text.splitlines()), 12)
        self.assertIn("more", text)

    def test_empty_map_is_empty(self):
        self.assertEqual(repository_map([]), "")
