import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_apache_license_and_package_metadata(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", text)
        self.assertIn("Version 2.0", text)
        self.assertGreater(len(text), 10_000)
        self.assertTrue((ROOT / "NOTICE").is_file())
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["version"], "0.7.281")
        self.assertEqual(project["license"], {"text": "Apache-2.0"})
        self.assertIn("License :: OSI Approved :: Apache Software License",
                      project["classifiers"])
        import nalar

        self.assertEqual(nalar.__version__, project["version"])
