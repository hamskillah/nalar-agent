import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository
from nalar.workspace import ToolError


class RepoMapToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "app.py").write_text(
            "def load(path):\n    return path\n\n"
            "class Parser:\n    def parse(self):\n        pass\n\n"
            "async def save(item):\n    return item\n", encoding="utf-8")
        (self.root / "lib.py").write_text("def helper():\n    return 1\n",
                                          encoding="utf-8")
        (self.root / "ui.js").write_text(
            "export function render() {}\nconst state = 1\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_counts_languages_and_ranks_files_by_symbols(self):
        info = self.repo.call("repo_map", {})
        self.assertEqual(info["files"], 4)
        self.assertEqual(info["languages"]["py"], 2)
        self.assertEqual(info["languages"]["js"], 1)
        self.assertEqual(info["languages"]["md"], 1)
        self.assertEqual(info["symbols"], 7)
        self.assertEqual(info["symbol_files"], 3)
        self.assertEqual(info["skipped_symbol_files"], 0)
        self.assertEqual([item["path"] for item in info["hot"]],
                         ["app.py", "ui.js", "lib.py"])
        self.assertEqual(info["hot"][0]["total"], 4)
        self.assertEqual({item["name"] for item in info["hot"][0]["symbols"]},
                         {"load", "Parser", "parse", "save"})
        lines = [item["line"] for item in info["hot"][0]["symbols"]]
        self.assertEqual(lines, sorted(lines))
        self.assertFalse(info["truncated"])
        self.assertEqual(info["omitted_symbols"], 0)

    def test_staged_files_are_included_and_deleted_files_are_not(self):
        self.repo.call("create_file", {"path": "extra.py",
                                       "text": "def fresh():\n    pass\n"})
        self.repo.call("create_file", {"path": "extra2.py",
                                       "text": "def fresh2():\n    pass\n"})
        self.repo.call("delete_file", {"path": "lib.py"})
        info = self.repo.call("repo_map", {})
        self.assertNotIn("lib.py", [item["path"] for item in info["hot"]])
        self.assertIn("extra.py", [item["path"] for item in info["hot"]])
        self.assertEqual(info["files"], 5)
        self.assertEqual(info["languages"].get("py"), 3)

    def test_output_is_bounded_and_reports_truncation(self):
        for number in range(20):
            (self.root / f"mod{number}.py").write_text(
                "".join(f"def f{number}_{index}():\n    pass\n" for index in range(10)),
                encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "many"],
                       check=True)
        repo = Repository(self.root)
        info = repo.call("repo_map", {})
        self.assertEqual(len(info["hot"]), 15)
        self.assertTrue(all(len(item["symbols"]) == 8 for item in info["hot"]))
        expected = info["symbols"] - sum(min(item["total"], 8) for item in info["hot"])
        self.assertEqual(info["omitted_symbols"], expected)
        self.assertTrue(info["truncated"])
        for item in info["hot"]:
            lines = [symbol["line"] for symbol in item["symbols"]]
            self.assertEqual(lines, sorted(lines))

    def test_many_non_code_files_do_not_hide_the_code(self):
        assets = self.root / "assets" / "i18n"
        assets.mkdir(parents=True)
        for number in range(320):
            (assets / f"m{number:03d}.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "assets"],
                       check=True)
        info = Repository(self.root).call("repo_map", {})
        self.assertGreater(info["symbols"], 0)
        self.assertIn("app.py", [item["path"] for item in info["hot"]])

    def test_more_than_300_symbol_files_report_truncation(self):
        for number in range(305):
            (self.root / f"mod{number:03d}.py").write_text(
                f"def f{number}():\n    pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "many"],
                       check=True)
        info = Repository(self.root).call("repo_map", {})
        self.assertEqual(info["symbols"], 303)
        expected = info["symbols"] - sum(min(item["total"], 8) for item in info["hot"])
        self.assertEqual(info["omitted_symbols"], expected)
        self.assertTrue(info["truncated"])

    def test_unreadable_symbol_file_is_reported_as_skipped(self):
        (self.root / "latin.py").write_bytes(b"answer = 41\xff\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "binary"],
                       check=True)
        info = Repository(self.root).call("repo_map", {})
        self.assertEqual(info["skipped_symbol_files"], 1)
        self.assertTrue(info["truncated"])

    def test_language_cap_is_reported_as_truncated(self):
        for number in range(13):
            (self.root / f"data{number}.ext{number}").write_text("x\n",
                                                                 encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "langs"],
                       check=True)
        info = Repository(self.root).call("repo_map", {})
        self.assertEqual(len(info["languages"]), 12)
        self.assertTrue(info["truncated"])

    def test_git_show_rejects_a_missing_commit_or_path(self):
        with self.assertRaisesRegex(ToolError, "not_found"):
            self.repo.call("git_show", {"sha": "0000000", "path": ""})
        with self.assertRaisesRegex(ToolError, "not_found"):
            self.repo.call("git_show", {"sha": self.repo.commit.decode(),
                                        "path": "missing.py"})
        with self.assertRaisesRegex(ToolError, "invalid_sha"):
            self.repo.call("git_show", {"sha": "a" * 41, "path": ""})

    def test_git_show_accepts_a_sha256_commit_id(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        init = subprocess.run(
            ["git", "-C", str(root), "init", "-q", "--object-format=sha256",
             "--template="], capture_output=True, text=True)
        if init.returncode != 0:
            self.skipTest("git does not support sha256 repositories")
        (root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "sha256"],
                       check=True)
        repo = Repository(root)
        shown = repo.call("git_show", {"sha": repo.commit.decode(),
                                       "path": "app.py"})
        self.assertIn("app.py", shown["text"])
        with self.assertRaisesRegex(ToolError, "invalid_sha"):
            repo.call("git_show", {"sha": "a" * 65, "path": ""})

    def test_empty_repository_and_invalid_arguments(self):
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        subprocess.run(["git", "-C", str(empty), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "empty",
                        "--allow-empty"], check=True)
        self.assertEqual(Repository(empty).call("repo_map", {}),
                         {"files": 0, "symbols": 0, "symbol_files": 0,
                          "languages": {}, "hot": [], "omitted_symbols": 0,
                          "skipped_symbol_files": 0, "truncated": False})
        with self.assertRaisesRegex(ToolError, "invalid_arguments"):
            self.repo.call("repo_map", {"prefix": ""})
