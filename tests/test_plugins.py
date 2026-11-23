import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.config import load_config
from nalar.plugins import load_plugins

ROOT = Path(__file__).resolve().parents[1]


class PluginFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()

    def write(self, name, body):
        path = self.plugins / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_python_files_in_the_user_directory_are_loaded(self):
        self.write("demo.py", '''
def tools():
    return [{
        "name": "demo_file_tool",
        "description": "From a file.",
        "parameters": {"type": "object", "properties": {}},
        "handler": lambda args: "ok",
    }]
''')
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual([spec["function"]["name"] for spec, _ in specs],
                         ["demo_file_tool"])
        self.assertEqual(warnings, [])

    def test_bad_files_are_skipped_with_warnings(self):
        self.write("broken.py", "raise RuntimeError('boom')\n")
        self.write("not_a_module.txt", "ignored\n")
        (self.plugins / "link.py").symlink_to(self.plugins / "broken.py")
        huge = self.plugins / "huge.py"
        huge.write_text("#" * (256 * 1024 + 1), encoding="utf-8")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(specs, [])
        self.assertTrue(any("broken.py" in warning for warning in warnings))
        self.assertTrue(any("huge.py" in warning for warning in warnings))

    def test_unhashable_names_do_not_escape_as_tracebacks(self):
        self.write("bad.py", """def tools():
    return [{"name": ["x"], "description": "d",
             "parameters": {"type": "object"}, "handler": lambda args: 1}]
""")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(specs, [])
        self.assertTrue(any("invalid tool" in warning for warning in warnings))

    def test_a_raising_tools_method_is_a_warning(self):
        self.write("boom.py", """class Plugin:
    def tools(self):
        raise RuntimeError("missing key")

def tools():
    return Plugin()
""")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(specs, [])
        self.assertTrue(any("unavailable" in warning for warning in warnings))

    def test_system_exit_is_a_warning(self):
        self.write("exit_tools.py", "def tools():\n    raise SystemExit(7)\n")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(specs, [])
        self.assertTrue(any("unavailable" in warning for warning in warnings))

    def test_import_system_exit_is_a_warning(self):
        self.write("exit_import.py", "raise SystemExit(3)\n")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(specs, [])
        self.assertTrue(any("unavailable" in warning for warning in warnings))

    def test_tool_list_truncation_is_warned(self):
        tools = "\n".join(
            f'    {{"name": "t{i}", "description": "d", '
            f'"parameters": {{"type": "object"}}, "handler": lambda args: 1}},'
            for i in range(33))
        self.write("many.py", f"def tools():\n    return [\n{tools}\n    ]\n")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertEqual(len(specs), 32)
        self.assertTrue(any("tool list truncated" in warning for warning in warnings))

    def test_skip_reasons_and_duplicate_tools_are_warned(self):
        for index in range(9):
            self.write(f"p{index}.py", "def tools():\n    return []\n")
        self.write("bad-name.py", "def tools():\n    return []\n")
        self.write("dup1.py", """def tools():
    return [{"name": "dup", "description": "d",
             "parameters": {"type": "object"}, "handler": lambda args: 1}]
""")
        self.write("dup2.py", """def tools():
    return [{"name": "dup", "description": "d",
             "parameters": {"type": "object"}, "handler": lambda args: 1}]
""")
        specs, warnings = load_plugins([], [self.plugins])
        self.assertTrue(any("file limit" in warning for warning in warnings))
        self.assertTrue(any("invalid name" in warning for warning in warnings))
        self.assertTrue(any("duplicate tool dup" in warning for warning in warnings))

    def test_missing_and_symlinked_directories_are_ignored(self):
        target = self.root / "real"
        target.mkdir()
        (target / "demo.py").write_text("def tools():\n    return []\n", encoding="utf-8")
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        specs, warnings = load_plugins([], [self.root / "missing", link])
        self.assertEqual(specs, [])
        self.assertEqual(warnings, [])

    def test_config_exposes_the_user_plugin_directory(self):
        global_file = self.root / "global.json"
        global_file.write_text(json.dumps({}), encoding="utf-8")
        config = load_config(self.root, global_file)
        self.assertEqual(config.plugin_dirs, (global_file.parent / "plugins",))


class PluginCheckCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "global.json"

    def cli(self, *args):
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.config)}
        return subprocess.run(
            [sys.executable, "-m", "nalar", "plugins", *args,
             "--repo", str(self.root)], cwd=ROOT, env=env,
            capture_output=True, text=True)

    def test_check_passes_without_plugins(self):
        self.config.write_text("{}", encoding="utf-8")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warnings"], [])

    def test_check_fails_for_a_missing_plugin(self):
        self.config.write_text(
            json.dumps({"plugins": ["definitely_missing_plugin"]}),
            encoding="utf-8")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("definitely_missing_plugin",
                      " ".join(payload["warnings"]))
        text = self.cli("check", "--format", "text")
        self.assertEqual(text.returncode, 1)
        self.assertIn("warning:", text.stdout)
