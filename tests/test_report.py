import unittest

from nalar.report import (cell, review_github, review_markdown,
                          scan_github, scan_markdown)


class MarkdownTests(unittest.TestCase):
    def test_review_findings_render_as_an_escaped_table(self):
        outcome = {"status": "ok", "findings": [
            {"severity": "high", "file": "app|core.py", "line": 3,
             "title": "Break <script> & tabs\there",
             "detail": "First line\nsecond line | pipe"},
        ]}
        text = review_markdown(outcome, model="vendor/model", scope="worktree")
        self.assertIn("## Nalar review", text)
        self.assertIn("- Model: vendor/model", text)
        self.assertIn("| high | app&#124;core.py:3 |", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("\nsecond line", text)
        self.assertIn("First line second line &#124; pipe", text)
        self.assertTrue(text.endswith("\n"))

    def test_unparsed_review_is_quoted_and_not_presented_as_clean(self):
        text = review_markdown(
            {"status": "unparsed", "findings": [], "text": "not json\n# heading"},
            model="m", scope="staged")
        self.assertIn("could not be parsed", text)
        self.assertIn("> not json", text)
        self.assertIn("> &#35; heading", text)
        self.assertNotIn("\n# heading", text)

    def test_review_without_findings_is_explicit(self):
        text = review_markdown({"status": "ok", "findings": []},
                               model="m", scope="s")
        self.assertIn("No concrete defects reported.", text)

    def test_scan_findings_and_counts_render_without_values(self):
        report = {"findings": [
            {"file": "a|b.py", "line": 2, "kind": "aws_access_key"}],
            "files": 6, "binary": 2, "skipped": 1, "skipped_untracked": 3,
            "unsafe_untracked": 1, "truncated": True, "capped": True}
        text = scan_markdown(report, scope="tree")
        self.assertIn("- Scope: tree", text)
        self.assertIn("- Findings: 1", text)
        self.assertIn("| a&#124;b.py | 2 | aws&#95;access&#95;key |", text)
        self.assertIn("2 binary file(s) skipped.", text)
        self.assertIn("1 oversize committed file(s) skipped.", text)
        self.assertIn("3 untracked file(s) skipped.", text)
        self.assertIn("1 unsafe untracked file(s) skipped.", text)
        self.assertIn("size limit", text)
        self.assertIn("Additional findings were capped.", text)
        self.assertNotIn("AKIA", text)

    def test_clean_scan_is_explicit(self):
        text = scan_markdown({"findings": [], "files": 4}, scope="tree")
        self.assertIn("No high-confidence credentials detected.", text)
        self.assertNotIn("**Incomplete:**", text)

    def test_cell_escapes_html_and_control_free_text(self):
        self.assertEqual(cell("a<b>&c"), "a&lt;b&gt;&amp;c")
        self.assertEqual(cell("x|y"), "x&#124;y")
        self.assertEqual(cell("x\ny\tz"), "x y z")
        self.assertEqual(len(cell("x" * 100, 10)), 10)

    def test_github_review_annotations_escape_properties_and_data(self):
        text = review_github({"status": "ok", "findings": [{
            "severity": "high", "file": "a,b.py", "line": 3,
            "title": "Bad%thing\nline", "detail": "x:y,z\n%done"}]})
        self.assertTrue(text.startswith(
            "::error file=a%2Cb.py,line=3,"
            "title=Nalar high%3A Bad%25thing line::"))
        self.assertIn("x:y,z %25done", text)
        self.assertEqual(text.count("\n"), 1)

    def test_github_unparsed_review_is_one_error_without_raw_text(self):
        text = review_github({"status": "unparsed", "findings": [],
                              "text": "secret raw\nline"})
        self.assertTrue(text.startswith("::error"))
        self.assertIn("unreviewed", text)
        self.assertNotIn("secret raw", text)
        self.assertEqual(text.count("\n"), 1)

    def test_github_scan_annotations_never_include_values(self):
        text = scan_github({"findings": [{
            "file": "a,b.py", "line": 2, "kind": "aws_access_key"}]},
            incomplete=True)
        self.assertIn("::error file=a%2Cb.py,line=2,"
                      "title=Nalar credential::", text)
        self.assertIn("Potential aws access key", text)
        self.assertIn("::warning title=Nalar scan::", text)
        self.assertNotIn("AKIA", text)

    def test_markdown_links_images_and_emphasis_are_neutralized(self):
        text = review_markdown({"status": "ok", "findings": [{
            "severity": "high", "file": "![x](https://evil/p.png).py",
            "line": 1,
            "title": "[APPROVED](https://evil.example)",
            "detail": "![status](https://evil/p.png) **no defects** _x_ ~~y~~"}]})
        self.assertNotIn("[APPROVED]", text)
        self.assertNotIn("![status]", text)
        self.assertNotIn("**no defects**", text)
        self.assertNotIn("_x_", text)
        self.assertNotIn("~~y~~", text)
        self.assertIn("&#91;APPROVED&#93;", text)
        self.assertIn("&#33;&#91;status&#93;", text)

    def test_bidi_and_zero_width_controls_are_removed(self):
        text = cell("\u202eRTL\u200b zero \u2066x\u2069")
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\u200b", text)
        self.assertNotIn("\u2066", text)
        self.assertIn("RTL", text)

    def test_github_scan_path_is_bounded(self):
        text = scan_github({"findings": [{
            "file": "a" * 60_000, "line": 2,
            "kind": "aws_access_key"}]})
        self.assertLess(len(text), 2_000)
        self.assertIn("::error", text)

    def test_renderers_tolerate_non_list_or_non_dict_findings(self):
        for findings in ("text", [None, {"file": "a.py", "line": 1,
                                        "severity": "low", "title": "t",
                                        "detail": "d", "kind": "k"}]):
            with self.subTest(findings=findings):
                self.assertIsInstance(review_markdown(
                    {"status": "ok", "findings": findings}), str)
                self.assertIsInstance(scan_markdown(
                    {"findings": findings}), str)
                self.assertIsInstance(review_github(
                    {"status": "ok", "findings": findings}), str)
                self.assertIsInstance(scan_github(
                    {"findings": findings}), str)

    def test_cell_keeps_zwj_and_strips_bidi_marks(self):
        self.assertIn("👨‍👩", cell("family 👨‍👩"))
        self.assertNotIn("‮", cell("a‮b"))
        self.assertNotIn("؜", cell("a؜b"))

    def test_cell_strips_default_ignorables(self):
        text = cell("a\u00adb\U000e0041c\u2028d")
        self.assertNotIn("\u00ad", text)
        self.assertNotIn("\U000e0041", text)
        self.assertNotIn("\u2028", text)
