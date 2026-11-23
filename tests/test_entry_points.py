import unittest

from nalar.plugins import load_plugins


class FakeEntryPoint:
    def __init__(self, name, produced=None, error=False):
        self.name = name
        self._produced = produced
        self._error = error

    def load(self):
        if self._error:
            raise RuntimeError
        return lambda: self._produced


def tool(name):
    return {"name": name, "description": "d",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
            "handler": lambda args: "ok"}


class EntryPointTests(unittest.TestCase):
    def test_installed_entry_points_are_loaded(self):
        from unittest.mock import patch

        entries = [FakeEntryPoint("pkg.one", [tool("one")]),
                   FakeEntryPoint("pkg.broken", error=True),
                   FakeEntryPoint("pkg.bad", [{"name": "bad name", "description": "d",
                                               "parameters": {"type": "object"}, "handler": None}])]
        with patch("nalar.plugins._installed_entry_points", return_value=entries):
            specs, warnings = load_plugins([])
        self.assertEqual([spec["function"]["name"] for spec, _ in specs], ["one"])
        self.assertTrue(any("pkg.broken" in warning for warning in warnings))
        self.assertTrue(any("pkg.bad" in warning for warning in warnings))

    def test_duplicate_names_between_config_and_entry_points_are_rejected(self):
        from unittest.mock import patch

        class Module:
            @staticmethod
            def tools():
                return [tool("one")]

        with patch("nalar.plugins._installed_entry_points",
                   return_value=[FakeEntryPoint("pkg", [tool("one")])]):
            specs, warnings = load_plugins([])
        self.assertEqual(len(specs), 1)
        with patch("nalar.plugins._installed_entry_points",
                   return_value=[FakeEntryPoint("pkg", [tool("one")])]), \
                patch("nalar.plugins.importlib.import_module", return_value=Module):
            specs, warnings = load_plugins(["fake_plugin"])
        self.assertEqual(len(specs), 1)
        self.assertTrue(warnings)
