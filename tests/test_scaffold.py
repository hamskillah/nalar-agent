import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.global_config = self.root / "config" / "config.json"

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.global_config)}
        return subprocess.run(
            [sys.executable, "-m", "nalar", *args], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30)

    def test_skill_init_is_discoverable_and_refuses_overwrite(self):
        result = self.cli("skills", "init", "review", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["created"], ".nalar/skills/review/SKILL.md")
        target = self.repo / summary["created"]
        self.assertTrue(target.is_file())
        self.assertIn("name: review", target.read_text(encoding="utf-8"))
        from nalar.skills import discover

        skills = discover((self.repo / ".nalar" / "skills",))
        self.assertEqual([skill.name for skill in skills], ["review"])
        duplicate = self.cli("skills", "init", "review", "--repo", str(self.repo))
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn("already exists", duplicate.stderr)
        forced = self.cli("skills", "init", "review", "--repo", str(self.repo),
                          "--force", "--description", "Custom review")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("Custom review", target.read_text(encoding="utf-8"))
        invalid = self.cli("skills", "init", "Bad Name", "--repo", str(self.repo))
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(list(self.root.rglob(".nalar-scaffold-*")), [])

    def test_command_init_is_discoverable_and_uses_arguments(self):
        result = self.cli("commands", "init", "audit", "--repo", str(self.repo),
                          "--description", "Audit one path")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        target = self.repo / summary["created"]
        self.assertEqual(summary["created"], ".nalar/commands/audit.md")
        text = target.read_text(encoding="utf-8")
        self.assertIn("description: Audit one path", text)
        self.assertIn("$ARGUMENTS", text)
        from nalar.commands import discover

        self.assertEqual([item.name for item in discover(
            (self.repo / ".nalar" / "commands",))], ["audit"])
        duplicate = self.cli("commands", "init", "audit", "--repo", str(self.repo))
        self.assertEqual(duplicate.returncode, 1)

    def test_plugin_init_creates_a_private_loadable_module(self):
        result = self.cli("plugins", "init", "demo", "--repo", str(self.repo),
                          "--description", "Echo a value")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        target = Path(summary["created"])
        self.assertTrue(target.is_file())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(summary["module"], "demo")
        from nalar.plugins import load_plugins

        specs, warnings = load_plugins((), (self.root / "config" / "plugins",))
        self.assertEqual(warnings, [])
        self.assertEqual([spec["function"]["name"] for spec, _ in specs], ["demo"])
        self.assertEqual(specs[0][0]["function"]["description"], "Echo a value")
        handler = specs[0][1]
        self.assertEqual(handler({"text": "hello"}), {"text": "hello"})
        duplicate = self.cli("plugins", "init", "demo", "--repo", str(self.repo))
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn("already exists", duplicate.stderr)
        invalid = self.cli("plugins", "init", "bad-name", "--repo", str(self.repo))
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(list(self.root.rglob(".nalar-scaffold-*")), [])

    def test_scaffolding_refuses_a_directory_target(self):
        target = self.repo / ".nalar" / "skills" / "review" / "SKILL.md"
        target.mkdir(parents=True)
        result = self.cli("skills", "init", "review", "--repo", str(self.repo),
                          "--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a regular file", result.stderr)

    def test_scaffolding_refuses_symlinked_directories(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".nalar").symlink_to(outside, target_is_directory=True)
        result = self.cli("skills", "init", "review", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

        (self.repo / ".nalar").unlink()
        config = self.root / "config"
        config.mkdir()
        plugins = config / "plugins"
        plugins.symlink_to(outside, target_is_directory=True)
        result = self.cli("plugins", "init", "demo", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
