import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from nalar.config import ConfigError, load_config
from nalar.manage import (ManageError, add_server, remove_server, set_server_enabled,
                          set_value, unset_value)


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config.json"

    def test_add_and_remove_preserve_other_settings_and_permissions(self):
        self.config.write_text(json.dumps({"model": "beam-free"}), encoding="utf-8")
        add_server("docs", ["npx", "-y", "docs-mcp"], path=self.config)
        data = json.loads(self.config.read_text())
        self.assertEqual(data["model"], "beam-free")
        self.assertEqual(data["mcp"]["docs"]["command"], ["npx", "-y", "docs-mcp"])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        loaded = load_config(self.root, self.config)
        self.assertEqual([server.name for server in loaded.mcp], ["docs"])
        remove_server("docs", path=self.config)
        self.assertNotIn("mcp", json.loads(self.config.read_text()))

    def test_add_rejects_duplicates_and_invalid_values(self):
        add_server("docs", ["docs-server"], path=self.config)
        with self.assertRaises(ManageError):
            add_server("docs", ["other"], path=self.config)
        add_server("docs", ["other"], path=self.config, force=True)
        for name, command in (("Bad Name", ["x"]), ("ok", []), ("ok", ["x", ""]),
                              ("ok", ["x"] * 9)):
            with self.subTest(name=name, command=command), self.assertRaises((ManageError, ConfigError)):
                add_server(name, command, path=self.config, force=True)

    def test_enable_and_disable_a_server(self):
        add_server("docs", ["docs-server"], path=self.config)
        set_server_enabled("docs", False, path=self.config)
        data = json.loads(self.config.read_text())
        self.assertIs(data["mcp"]["docs"]["enabled"], False)
        self.assertFalse(load_config(self.root, self.config).mcp[0].enabled)
        set_server_enabled("docs", True, path=self.config)
        self.assertTrue(load_config(self.root, self.config).mcp[0].enabled)
        with self.assertRaises(ManageError):
            set_server_enabled("missing", False, path=self.config)

    def test_force_add_preserves_a_disabled_status(self):
        add_server("docs", ["docs-server"], path=self.config)
        set_server_enabled("docs", False, path=self.config)
        add_server("docs", ["other-server"], path=self.config, force=True)
        self.assertFalse(load_config(self.root, self.config).mcp[0].enabled)

    def test_config_editor_accepts_all_known_keys(self):
        self.config.write_text(json.dumps({"retries": 3, "fallback_models": ["a"]}),
                               encoding="utf-8")
        set_value("model", "beam-free", path=self.config)
        self.assertEqual(json.loads(self.config.read_text())["model"], "beam-free")

    def test_set_and_unset_validate_and_preserve_other_keys(self):
        set_value("model", "beam-free", path=self.config)
        set_value("permission", "auto", path=self.config)
        set_value("provider.api_key_env", "GATEWAY_KEY", path=self.config)
        data = json.loads(self.config.read_text())
        self.assertEqual((data["model"], data["permission"],
                          data["provider"]["api_key_env"]),
                         ("beam-free", "auto", "GATEWAY_KEY"))
        unset_value("provider.api_key_env", path=self.config)
        self.assertNotIn("provider", json.loads(self.config.read_text()))
        for key, value in (("model", "bad id"), ("permission", "yolo"),
                           ("token_budget", "-1"), ("max_turns", "0"),
                           ("provider.base_url", "http://example.com/x"), ("nope", "x")):
            with self.subTest(key=key), self.assertRaises(ManageError):
                set_value(key, value, path=self.config)
        before = self.config.read_text()
        with self.assertRaises(ManageError):
            set_value("permission", "yolo", path=self.config)
        self.assertEqual(self.config.read_text(), before)

    def test_http_servers_can_be_added_with_headers(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"REMOTE_TOKEN": "secret"}):
            add_server("remote", url="https://example.com/mcp",
                       headers={"Authorization": "Bearer {env:REMOTE_TOKEN}"}, path=self.config)
        data = json.loads(self.config.read_text())
        self.assertEqual(data["mcp"]["remote"]["url"], "https://example.com/mcp")
        self.assertEqual(data["mcp"]["remote"]["headers"]["Authorization"],
                         "Bearer {env:REMOTE_TOKEN}")
        with self.assertRaises(ManageError):
            add_server("both", command=["x"], url="https://example.com/mcp", path=self.config)
        before = self.config.read_text()
        for headers in ({"Authorization": "Bearer x\r\nX-Injected: 1"},
                        {"Authorization": "Bearer {env:MISSING_TOKEN}"}):
            with self.subTest(headers=headers), self.assertRaises(ManageError):
                add_server("bad", url="https://example.com/mcp", headers=headers,
                           path=self.config, force=True)
            self.assertEqual(self.config.read_text(), before)

    def test_stdio_env_is_persisted_and_validated(self):
        add_server("local", ["server"], env={"MODE": "test"}, path=self.config)
        self.assertEqual(json.loads(self.config.read_text())["mcp"]["local"]["env"],
                         {"MODE": "test"})
        with self.assertRaises(ManageError):
            add_server("remote", url="https://example.com/mcp", env={"A": "b"},
                       path=self.config, force=True)
        before = self.config.read_text()
        with self.assertRaises(ManageError):
            add_server("bad", ["server"], env={"A": "x\r\nB: 1"}, path=self.config, force=True)
        self.assertEqual(self.config.read_text(), before)

    def test_remove_unknown_server_is_an_error(self):
        with self.assertRaises(ManageError):
            remove_server("missing", path=self.config)

    def test_malformed_existing_config_is_not_overwritten(self):
        self.config.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(ManageError):
            add_server("docs", ["docs-server"], path=self.config)
        self.assertEqual(self.config.read_text(), "{bad json")

    def test_review_model_can_be_set_and_unset(self):
        set_value("review_model", "vendor/reviewer", path=self.config)
        self.assertEqual(json.loads(self.config.read_text())["review_model"],
                         "vendor/reviewer")
        self.assertEqual(load_config(self.root, self.config).review_model,
                         "vendor/reviewer")
        unset_value("review_model", path=self.config)
        self.assertNotIn("review_model", json.loads(self.config.read_text()))
        with self.assertRaises(ManageError):
            set_value("review_model", "bad model!", path=self.config)

    def test_subagent_settings_can_be_set_and_unset(self):
        set_value("subagent_model", "vendor/tiny", path=self.config)
        set_value("subagent_max_turns", "3", path=self.config)
        set_value("subagent_max_calls", "4", path=self.config)
        set_value("subagent_token_budget", "5000", path=self.config)
        data = json.loads(self.config.read_text())
        self.assertEqual((data["subagent_model"], data["subagent_max_turns"],
                          data["subagent_max_calls"],
                          data["subagent_token_budget"]),
                         ("vendor/tiny", 3, 4, 5000))
        loaded = load_config(self.root, self.config)
        self.assertEqual((loaded.subagent_model, loaded.subagent_max_turns,
                          loaded.subagent_max_calls,
                          loaded.subagent_token_budget),
                         ("vendor/tiny", 3, 4, 5000))
        unset_value("subagent_max_turns", path=self.config)
        self.assertNotIn("subagent_max_turns",
                         json.loads(self.config.read_text()))
        for key, value in (("subagent_model", "bad model!"),
                           ("subagent_max_turns", "0"),
                           ("subagent_max_calls", "41"),
                           ("subagent_token_budget", "999")):
            with self.subTest(key=key), self.assertRaises(ManageError):
                set_value(key, value, path=self.config)
