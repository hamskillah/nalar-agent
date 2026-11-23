import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InitTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "--template="], check=True)

    def init(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        return subprocess.run([sys.executable, "-m", "nalar", "init", "--repo", str(self.repo), *args],
                              cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def test_creates_loadable_config_and_example_skill(self):
        result = self.init("--model", "beam-free")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.repo.glob(".nalar-init-*")), [])
        payload = json.loads(result.stdout)
        self.assertIn("nalar.json", payload["created"])
        config = json.loads((self.repo / "nalar.json").read_text())
        self.assertEqual(config["model"], "beam-free")
        self.assertNotIn("permission", config)
        skill = self.repo / ".nalar" / "skills" / "review" / "SKILL.md"
        self.assertTrue(skill.is_file())
        from nalar.config import load_config
        loaded = load_config(self.repo, self.repo / "none.json")
        self.assertEqual(loaded.model, "beam-free")
        self.assertEqual([item.name for item in __import__("nalar.skills", fromlist=["discover"]).discover(loaded.skills)],
                         ["review"])

    def test_refuses_to_overwrite_without_force(self):
        self.assertEqual(self.init().returncode, 0)
        result = self.init()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)
        forced = self.init("--force", "--model", "other")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(json.loads((self.repo / "nalar.json").read_text())["model"], "other")

    def test_accepts_a_vendor_prefixed_model(self):
        result = self.init("--model", "openai/gpt-5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.repo / "nalar.json").read_text())["model"],
                         "openai/gpt-5")

    def test_keeps_existing_permissions_and_cleans_up_an_interrupted_write(self):
        config = self.repo / "nalar.json"
        config.write_text("{}", encoding="utf-8")
        os.chmod(config, 0o600)
        self.assertEqual(self.init("--force").returncode, 0)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

        config.unlink()
        from nalar.init import initialize

        with patch("nalar.atomic.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                initialize(self.repo, force=True)
        self.assertEqual(list(self.repo.glob(".nalar-init-*")), [])
        self.assertFalse(config.exists())

    def test_refuses_to_write_through_symlinks(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "outside.json"
        outside.write_text("keep", encoding="utf-8")
        (self.repo / "nalar.json").symlink_to(outside)
        result = self.init("--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

        (self.repo / "nalar.json").unlink()
        target = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".nalar").symlink_to(target, target_is_directory=True)
        result = self.init()
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.repo / "nalar.json").exists())

    def test_rejects_invalid_model_and_missing_repo(self):
        result = self.init("--model", "bad model!")
        self.assertEqual(result.returncode, 2)
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.repo / "none.json")}
        result = subprocess.run([sys.executable, "-m", "nalar", "init", "--repo", "/nonexistent/nalar"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_detect_writes_python_and_node_commands(self):
        (self.repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
        (self.repo / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test"}}) + "\n", encoding="utf-8")
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["verify"], [["pnpm", "test"],
                                             ["python3", "-m", "pytest", "-q"]])
        config = json.loads((self.repo / "nalar.json").read_text())
        self.assertEqual(config["verify"], summary["verify"])
        from nalar.config import load_config
        loaded = load_config(self.repo, self.repo / "none.json", trust_project=True)
        self.assertEqual([list(argv) for argv in loaded.verify], summary["verify"])

    def test_detect_handles_go_cargo_make_and_skips_placeholder_tests(self):
        (self.repo / "go.mod").write_text("module example.test/app\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname = 'app'\n", encoding="utf-8")
        (self.repo / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        (self.repo / "package.json").write_text(
            json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})
            + "\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verify"],
                         [["go", "test", "./..."], ["cargo", "test", "--quiet"],
                          ["make", "test"]])

    def test_detect_ignores_symlinked_and_oversize_markers(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pyproject.toml"
        outside.write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        (self.repo / "pyproject.toml").symlink_to(outside)
        result = self.init("--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verify"], [])
        self.assertNotIn("verify", json.loads((self.repo / "nalar.json").read_text()))

        (self.repo / "pyproject.toml").unlink()
        (self.repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n" + "x" * (64 * 1024), encoding="utf-8")
        result = self.init("--force", "--detect")
        self.assertEqual(json.loads(result.stdout)["verify"], [])

    def test_detect_without_markers_adds_no_verify_and_never_runs_anything(self):
        marker = self.repo / "ran"
        (self.repo / "Makefile").write_text(
            f"test:\n\t@touch {marker}\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verify"], [["make", "test"]])
        self.assertFalse(marker.exists())

    def test_detection_is_limited_to_four_commands(self):
        (self.repo / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test"}}) + "\n", encoding="utf-8")
        (self.repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        (self.repo / "go.mod").write_text("module example.test/app\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\nname = 'app'\n", encoding="utf-8")
        (self.repo / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        result = self.init("--detect")
        summary = json.loads(result.stdout)
        self.assertEqual(len(summary["verify"]), 4)
        self.assertTrue(any("limit" in note for note in summary.get("notes", [])))

    def test_detect_finds_tests_only_and_nested_python_tests(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "unit").mkdir()
        (self.repo / "tests" / "unit" / "test_a.py").write_text(
            "def test_a():\n    assert True\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verify"],
                         [["python3", "-m", "pytest", "-q"]])

    def test_detect_finds_suffix_test_files(self):
        (self.repo / "parser_test.py").write_text(
            "def test_parser():\n    assert True\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(json.loads(result.stdout)["verify"],
                         [["python3", "-m", "pytest", "-q"]])

    def test_detect_poetry_note_only_without_a_python_command(self):
        (self.repo / "pyproject.toml").write_text(
            "[tool.poetry]\nname = 'app'\n", encoding="utf-8")
        result = self.init("--detect")
        summary = json.loads(result.stdout)
        self.assertEqual(summary["verify"], [])
        self.assertTrue(any("Poetry" in note for note in summary["notes"]))
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_a.py").write_text(
            "def test_a():\n    assert True\n", encoding="utf-8")
        result = self.init("--force", "--detect")
        summary = json.loads(result.stdout)
        self.assertEqual(summary["verify"], [["python3", "-m", "pytest", "-q"]])
        self.assertFalse(any("Poetry" in note for note in summary.get("notes", [])))

    def test_detect_ignores_make_assignment_targets(self):
        (self.repo / "Makefile").write_text(
            "test := hello\nall:\n\t@true\n", encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(json.loads(result.stdout)["verify"], [])

    def test_detect_skips_make_define_blocks(self):
        (self.repo / "Makefile").write_text(
            "define HELP\ntest: fake recipe\nendef\ntest:\n\t@true\n",
            encoding="utf-8")
        result = self.init("--detect")
        self.assertEqual(json.loads(result.stdout)["verify"], [["make", "test"]])
        (self.repo / "Makefile").write_text(
            "define HELP\ntest: fake recipe\nendef\n", encoding="utf-8")
        result = self.init("--force", "--detect")
        self.assertEqual(json.loads(result.stdout)["verify"], [])
