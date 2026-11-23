import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.global_file = self.root / "global.json"
        self.project = self.root / "project"
        self.project.mkdir()

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def load(self):
        return load_config(self.project, self.global_file)

    def test_defaults_when_no_files_exist(self):
        config = self.load()
        self.assertEqual((config.max_turns, config.max_calls, config.permission),
                         (12, 24, "ask"))
        self.assertEqual(config.mcp, ())
        self.assertIsNone(config.model)

    def test_model_ids_are_validated_consistently(self):
        for value in ("bad model!", "", 7):
            with self.subTest(value=value):
                self.write(self.global_file, {"model": value})
                with self.assertRaises(ConfigError):
                    self.load()
        self.write(self.global_file, {"model": "vendor/model"})
        self.assertEqual(self.load().model, "vendor/model")

    def test_review_model_accepts_vendor_ids_and_rejects_invalid_values(self):
        self.write(self.project / "nalar.json",
                   {"review_model": "vendor/reviewer"})
        self.assertEqual(self.load().review_model, "vendor/reviewer")
        for value in ("bad model!", "", 7):
            with self.subTest(value=value):
                self.write(self.project / "nalar.json", {"review_model": value})
                with self.assertRaises(ConfigError):
                    self.load()

    def test_global_config_sets_privileged_values_and_project_overrides_plain_ones(self):
        self.write(self.global_file, {
            "max_turns": 20, "model": "global-model", "permission": "auto",
            "mcp": {"code": {"command": ["code-server"], "env": {"TOKEN": "{env:MCP_TOKEN}"}},
                    "docs": {"command": ["docs-server"]}},
            "skills": ["global-skills"],
        })
        self.write(self.project / "nalar.json", {"max_turns": 6})
        with patch.dict(os.environ, {"MCP_TOKEN": "secret-value"}):
            config = self.load()
        self.assertEqual((config.model, config.max_turns, config.permission),
                         ("global-model", 6, "auto"))
        self.assertEqual([server.name for server in config.mcp], ["code", "docs"])
        self.assertEqual(config.mcp[0].env, {"TOKEN": "secret-value"})
        self.assertIn((self.root / "global-skills").resolve(), config.skills)

    def test_global_default_skill_and_command_directories_are_discovered(self):
        from nalar.commands import discover as discover_commands
        from nalar.skills import discover as discover_skills

        shared = self.global_file.parent / "skills" / "shared"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text(
            "---\nname: shared\ndescription: Global default\n---\nGlobal body.\n",
            encoding="utf-8")
        local = self.project / ".nalar" / "skills" / "local"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text(
            "---\nname: local\ndescription: Project default\n---\nLocal body.\n",
            encoding="utf-8")
        commands = self.global_file.parent / "commands"
        commands.mkdir()
        (commands / "deploy.md").write_text("Ship it.", encoding="utf-8")

        config = self.load()
        self.assertEqual([skill.name for skill in discover_skills(config.skills)],
                         ["local", "shared"])
        self.assertEqual([item.name for item in discover_commands(config.commands)],
                         ["deploy"])

    def test_project_skills_and_commands_shadow_global_defaults(self):
        from nalar.commands import discover as discover_commands
        from nalar.skills import discover as discover_skills

        for folder, body in ((self.global_file.parent / "skills" / "review", "Global body."),
                             (self.project / ".nalar" / "skills" / "review", "Project body.")):
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: review\ndescription: Review\n---\n{body}\n",
                encoding="utf-8")
        for folder, body in ((self.global_file.parent / "commands", "Global command."),
                             (self.project / ".nalar" / "commands", "Project command.")):
            folder.mkdir(parents=True)
            (folder / "deploy.md").write_text(body, encoding="utf-8")

        config = self.load()
        self.assertEqual(discover_skills(config.skills)[0].body, "Project body.\n")
        self.assertEqual(discover_commands(config.commands)[0].body, "Project command.")

    def test_project_config_cannot_grant_privileged_settings(self):
        cases = [
            {"mcp": {"x": {"command": ["x"]}}},
            {"provider": {"base_url": "https://attacker.example/v1/chat/completions"}},
            {"permission": "auto"},
            {"plugins": ["evil.module"]},
            {"tools": {"allow": ["*"]}},
            {"subagents": True},
            {"token_budget": 1},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write(self.project / "nalar.json", value)
                with self.assertRaises(ConfigError):
                    self.load()
        self.write(self.project / "nalar.json", {"permission": "plan"})
        self.assertEqual(self.load().permission, "plan")
        self.write(self.project / "nalar.json", {"mcp": {"x": {"command": ["x"]}}})
        trusted = load_config(self.project, self.global_file, trust_project=True)
        self.assertEqual([server.name for server in trusted.mcp], ["x"])

    def test_rejects_unknown_keys_and_invalid_values(self):
        cases = [
            {"mystery": 1},
            {"max_turns": 0},
            {"max_calls": 10_000},
            {"permission": "yolo"},
            {"skills": ["../outside"]},
            {"skills": "/etc"},
            {"mcp": {"bad name": {"command": ["x"]}}},
            {"mcp": {"x": {"command": []}}},
            {"mcp": {"x": {"command": ["x"], "env": {"A": "{env:ABSENT_VALUE_XYZ}"}}}},
            {"mcp": {"x": {"command": ["x"], "surprise": True}}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write(self.project / "nalar.json", value)
                with self.assertRaises(ConfigError):
                    self.load()

    def test_rejects_malformed_json_without_echoing_content(self):
        (self.project / "nalar.json").write_text('{"model": "secret-looking", ', encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertNotIn("secret-looking", str(caught.exception))

    def test_provider_rejects_invalid_port_and_control_header(self):
        self.write(self.global_file, {
            "model": "m",
            "provider": {"base_url": "http://127.0.0.1:99999/v1/chat/completions"},
        })
        with self.assertRaises(ConfigError):
            self.load()
        self.write(self.global_file, {
            "model": "m",
            "provider": {"headers": {"X-Key": "secret\n"}},
        })
        with self.assertRaises(ConfigError):
            self.load()

    def test_provider_rejects_non_latin1_headers_and_credentials(self):
        self.write(self.global_file, {
            "model": "m",
            "provider": {"headers": {"X-Note": "a\u20acb"}},
        })
        with self.assertRaises(ConfigError):
            self.load()
        self.write(self.global_file, {
            "model": "m",
            "provider": {"headers": {"X-Note": "\ud800"}},
        })
        with self.assertRaises(ConfigError):
            self.load()
        self.write(self.global_file, {
            "model": "m",
            "provider": {"base_url": "https://user:pw@gateway.example/v1/chat/completions"},
        })
        with self.assertRaises(ConfigError):
            self.load()
        self.write(self.global_file, {
            "model": "m",
            "provider": {"base_url": "https://gateway.example/v1/chat/completions\nX-Evil: 1"},
        })
        with self.assertRaises(ConfigError):
            self.load()

    def test_mcp_rejects_non_latin1_header_value(self):
        (self.project / "nalar.json").write_text(json.dumps({
            "mcp": {"docs": {"url": "http://127.0.0.1:9/mcp",
                             "headers": {"X-Org": "price \u20ac"}}}}),
            encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.project, self.global_file, trust_project=True)

    def test_provider_rejects_malformed_and_non_ascii_urls(self):
        for url in ("https://[zz]/v1/chat/completions",
                    "https://example.test/inference/\u00e9"):
            with self.subTest(url=url):
                self.write(self.global_file, {"model": "m", "provider": {"base_url": url}})
                with self.assertRaises(ConfigError):
                    self.load()
        (self.project / "nalar.json").write_text(json.dumps({
            "mcp": {"docs": {"url": "http://[zz]/mcp"}}}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.project, self.global_file, trust_project=True)
        (self.project / "nalar.json").write_text(json.dumps({
            "mcp": {"docs": {"url": "http://127.0.0.1:99999/mcp"}}}),
            encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.project, self.global_file, trust_project=True)
