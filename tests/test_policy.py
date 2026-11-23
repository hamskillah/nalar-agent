import json
import tempfile
import unittest
from pathlib import Path

from nalar.config import ConfigError, load_config
from nalar.tools import ToolBelt


class ToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def test_policy_is_parsed_and_defaults_to_unrestricted(self):
        config = load_config(self.root, self.root / "none.json", trust_project=True)
        self.assertIsNone(config.tools_allow)
        self.assertEqual(config.tools_deny, ())
        self.write({"tools": {"allow": ["read_*", "fixture__*"], "deny": ["fixture__explode"]}})
        config = load_config(self.root, self.root / "none.json", trust_project=True)
        self.assertEqual(config.tools_allow, ("read_*", "fixture__*"))
        self.assertEqual(config.tools_deny, ("fixture__explode",))

    def test_policy_rejects_invalid_patterns(self):
        for value in ({"tools": "read"}, {"tools": {"allow": "read"}},
                      {"tools": {"allow": ["a"] * 33}}, {"tools": {"allow": [""]}},
                      {"tools": {"allow": ["x"], "surprise": True}}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    load_config(self.root, self.root / "none.json", trust_project=True)

    def test_belt_filters_specs_and_handlers(self):
        belt = ToolBelt()
        for name in ("read_lines", "create_file", "fixture__echo", "fixture__explode"):
            belt.add({"type": "function", "function": {
                "name": name, "description": "d",
                "parameters": {"type": "object", "properties": {},
                               "additionalProperties": False}}}, lambda args: "ok")
        belt.apply_policy(["read_*", "fixture__*"], ["fixture__explode"])
        self.assertEqual({spec["function"]["name"] for spec in belt.specs},
                         {"read_lines", "fixture__echo"})
        from nalar.workspace import ToolError
        with self.assertRaises(ToolError):
            belt.call("fixture__explode", {})
        with self.assertRaises(ToolError):
            belt.call("create_file", {})
        self.assertEqual(belt.call("read_lines", {}), "ok")
