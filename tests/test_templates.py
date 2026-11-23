"""URI template matching for MCP resources (linear, no hostile regex)."""

import time
import unittest

from nalar.extensions import _template_matcher


class TemplateTests(unittest.TestCase):
    def test_simple_templates_match_one_segment(self):
        matcher = _template_matcher("file:///{path}")
        self.assertIsNotNone(matcher)
        self.assertTrue(matcher("file:///a.txt"))
        self.assertFalse(matcher("file:///a/b.txt"))
        self.assertFalse(matcher("file:///"))
        self.assertFalse(matcher("file:///"))

    def test_reserved_expansion_matches_the_rest(self):
        matcher = _template_matcher("mem://{+path}")
        self.assertIsNotNone(matcher)
        self.assertTrue(matcher("mem://a/b/c"))
        self.assertFalse(matcher("mem://"))

    def test_literals_and_multi_variable_templates(self):
        matcher = _template_matcher("repo://{owner}/{name}/raw")
        self.assertTrue(matcher("repo://alice/tool/raw"))
        self.assertFalse(matcher("repo://alice/tool/other"))
        self.assertFalse(matcher("repo://alice/tool/extra/raw"))
        host = _template_matcher("http://{host}/{port}")
        self.assertTrue(host("http://example.test/8080"))
        self.assertFalse(host("http://example.test:8080"))

    def test_unsupported_templates_are_rejected(self):
        for template in ("file://{?query}", "x://{a}{b}",
                         "x://" + "".join(f"{{v{i}}}" for i in range(9)),
                         "x://{+rest}/tail"):
            with self.subTest(template=template[:40]):
                self.assertIsNone(_template_matcher(template))

    def test_adjacent_variables_cannot_cause_backtracking(self):
        matcher = _template_matcher("x://" + "".join("{v}" for _ in range(8)))
        self.assertIsNone(matcher)
        started = time.monotonic()
        matcher = _template_matcher("x://{a}/{b}/{c}/{d}")
        self.assertIsNotNone(matcher)
        self.assertFalse(matcher("x://" + "a" * 900))
        self.assertLess(time.monotonic() - started, 0.5)
