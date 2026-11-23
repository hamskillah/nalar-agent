import random
import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository
from nalar.secrets import MAX_SCAN, scan_diff, scan_repository, scan_text


class SecretScanTests(unittest.TestCase):
    def test_only_added_lines_are_scanned_with_new_line_numbers(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            " old\n"
            "-password = \"removed-secret-value-123\"\n"
            "+AWS_KEY = \"AKIAABCDEFGHIJKLMNOP\"\n"
            "+GITHUB = \"ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\n"
            " context\n")
        report = scan_diff(diff)
        self.assertFalse(report["truncated"])
        self.assertEqual([(item["file"], item["line"], item["kind"])
                          for item in report["findings"]],
                         [("app.py", 2, "aws_access_key"),
                          ("app.py", 3, "github_token")])

    def test_private_keys_assignments_and_empty_diffs(self):
        diff = (
            "diff --git a/cfg.py b/cfg.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/cfg.py\n"
            "@@ -0,0 +1,4 @@\n"
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            "+password = \"a-very-long-password-12345\"\n"
            "+client = \"sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa\"\n"
            "+safe = \"short\"\n")
        kinds = [item["kind"] for item in scan_diff(diff)["findings"]]
        self.assertEqual(kinds, ["private_key", "secret_assignment", "anthropic_key"])
        self.assertEqual(scan_diff("")["findings"], [])

    def test_scan_text_reports_line_numbers(self):
        findings = scan_text('safe\nAWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n',
                             "app.py")
        self.assertEqual(findings, [{"file": "app.py", "line": 2,
                                     "kind": "aws_access_key"}])

    def test_scan_repository_skips_binary_and_honors_the_file_limit(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(root), "init", "-q", "--template="],
                       check=True)
        (root / "a.py").write_text('AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n',
                                   encoding="utf-8")
        (root / "b.bin").write_bytes(
            b'\x00AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
        (root / "c.py").write_text(
            'TOKEN = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
            encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "fixture"],
                       check=True)
        repository = Repository(root)
        report = scan_repository(repository)
        self.assertEqual([item["file"] for item in report["findings"]],
                         ["a.py", "c.py"])
        self.assertEqual(report["files"], 2)
        limited = scan_repository(repository, max_files=1)
        self.assertEqual(limited["files"], 1)
        self.assertTrue(limited["truncated"])

    def test_findings_are_bounded_without_echoing_values(self):
        lines = "".join(f"+token = \"{'x' * 20}{number}\"\n" for number in range(30))
        diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                "@@ -0,0 +1,30 @@\n" + lines)
        report = scan_diff(diff)
        self.assertEqual(len(report["findings"]), 20)
        self.assertTrue(report["capped"])
        self.assertNotIn("x" * 20, str(report))


def _added_diff(path, lines, start=1):
    body = "".join(f"+{line}\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},0 +{start},{len(lines)} @@\n{body}")


class SecretScannerRegressionTests(unittest.TestCase):
    def test_prefixed_and_unquoted_assignments_are_detected(self):
        diff = _added_diff("env", [
            'DB_PASSWORD = "a-very-long-password-12345"',
            'MY_API_KEY="abcdefghijklmnopqrstuvwx"',
            "SECRET_KEY = abcdefghijklmnopqrstuvwx",
            "auth_token: abcdef0123456789abcdef0123456789",
            "DATABASE_URL=postgres://user:s3cretpass@db.internal:5432/app",
        ])
        report = scan_diff(diff)
        self.assertEqual([item["kind"] for item in report["findings"]],
                         ["secret_assignment", "secret_assignment",
                          "secret_assignment", "secret_assignment",
                          "database_url"])
        self.assertEqual([item["line"] for item in report["findings"]],
                         [1, 2, 3, 4, 5])
        self.assertNotIn("s3cretpass", str(report))

    def test_context_and_removed_lines_are_ignored(self):
        diff = (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,3 +1,3 @@\n"
            ' password = "context-secret-value-123"\n'
            '-token = "removed-secret-value-123"\n'
            "+safe = 1\n")
        self.assertEqual(scan_diff(diff)["findings"], [])

    def test_form_feed_and_unicode_separator_do_not_end_the_hunk(self):
        diff = (
            "diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n"
            "@@ -1,0 +1,2 @@\n"
            '+A = "\x0c and \u2028 content"\n'
            '+K = "AKIAABCDEFGHIJKLMNOP"\n')
        findings = scan_diff(diff)["findings"]
        self.assertEqual([(item["line"], item["kind"]) for item in findings],
                         [(2, "aws_access_key")])

    def test_quoted_paths_and_header_like_content_keep_the_real_file(self):
        diff = (
            "diff --git a/caf\303\251.py b/caf\303\251.py\n"
            "--- a/caf\303\251.py\n"
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1 +1,4 @@\n"
            " old\n"
            "+x = 1\n"
            "++ b/evil.py\n"
            '+K = "AKIAABCDEFGHIJKLMNOP"\n')
        findings = scan_diff(diff)["findings"]
        self.assertEqual(findings, [{"file": "caf\u00e9.py", "line": 4,
                                     "kind": "aws_access_key"}])

    def test_second_hunk_in_the_same_file_is_scanned(self):
        diff = (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n line\n-old\n+new\n"
            "@@ -30,2 +30,2 @@\n line30\n-old30\n+K = \"AKIAABCDEFGHIJKLMNOP\"\n")
        self.assertEqual(scan_diff(diff)["findings"],
                         [{"file": "app.py", "line": 31,
                           "kind": "aws_access_key"}])

    def test_plain_unified_diff_scans_each_file(self):
        diff = (
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n"
            '+K = "AKIAABCDEFGHIJKLMNOP"\n')
        self.assertEqual(scan_diff(diff)["findings"],
                         [{"file": "b.py", "line": 1,
                           "kind": "aws_access_key"}])

    def test_variable_references_and_calls_are_not_assignments(self):
        diff = _added_diff("auth.py", [
            "password = credentials.password",
            "token = credentials.token",
            "api_key = settings.API_KEY",
            "token = load_token_from_keyring()",
            "client_secret = vault.read(name)",
            "DB_PASSWORD=correcthorsebatterystaple",
        ])
        self.assertEqual([item["kind"] for item in scan_diff(diff)["findings"]],
                         ["secret_assignment"])

    def test_unquoted_aws_secret_jwt_and_ya29_are_detected(self):
        diff = _added_diff("env", [
            "SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef",
            "TOKEN=ya29.a0AfH6SMBx8aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "password=abc/defghijklmnopqrstuvwxyz",
        ])
        self.assertEqual([item["kind"] for item in scan_diff(diff)["findings"]],
                         ["secret_assignment"] * 5)

    def test_a_malformed_hunk_header_does_not_hide_later_hunks(self):
        diff = (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,4 @@\n old\n+x = 1\n"
            "@@ -30,2 +30,2 @@\n line30\n-old30\n+K = \"AKIAABCDEFGHIJKLMNOP\"\n")
        self.assertEqual(scan_diff(diff)["findings"],
                         [{"file": "app.py", "line": 31,
                           "kind": "aws_access_key"}])

    def test_plain_diff_without_prefixes_keeps_the_path(self):
        diff = ("--- old.py\n+++ new.py\n@@ -1 +1 @@\n-x\n"
                '+K = "AKIAABCDEFGHIJKLMNOP"\n')
        self.assertEqual(scan_diff(diff)["findings"],
                         [{"file": "new.py", "line": 1,
                           "kind": "aws_access_key"}])

    def test_go_short_declaration_is_detected(self):
        diff = _added_diff("main.go", ['apiKey := "abcdefghijklmnopqrstuvwx"'])
        self.assertEqual([item["kind"] for item in scan_diff(diff)["findings"]],
                         ["secret_assignment"])

    def test_capped_is_only_true_when_a_finding_was_dropped(self):
        lines = [f'K{i} = "AKIA{i:016d}"' for i in range(20)]
        exact = scan_diff(_added_diff("keys.txt", lines))
        self.assertEqual(len(exact["findings"]), 20)
        self.assertFalse(exact["capped"])
        over = scan_diff(_added_diff("keys.txt", lines + ['EXTRA = "AKIA9999999999999999"']))
        self.assertEqual(len(over["findings"]), 20)
        self.assertTrue(over["capped"])

    def test_oversized_diff_is_marked_truncated(self):
        diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+"
                + "x" * MAX_SCAN)
        report = scan_diff(diff)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["findings"], [])


class SecretScannerFuzzTests(unittest.TestCase):
    def test_random_multi_hunk_diffs_always_find_the_secret(self):
        secret = 'K = "AKIAABCDEFGHIJKLMNOP"'
        for seed in range(25):
            rng = random.Random(seed)
            path = f"mod{seed}.py"
            parts = [f"diff --git a/{path} b/{path}", f"--- a/{path}",
                     f"+++ b/{path}"]
            secret_hunk = rng.randrange(3)
            expected = None
            for hunk in range(3):
                new_start = rng.randint(1, 200)
                new_count = rng.randint(1, 5)
                context = rng.randint(0, new_count - 1)
                added = new_count - context
                removed = rng.randint(0, 3)
                old_count = context + removed
                secret_slot = rng.randrange(added) if hunk == secret_hunk else None
                types = [" "] * context + ["+"] * added + ["-"] * removed
                rng.shuffle(types)
                parts.append(f"@@ -{rng.randint(1, 200)},{old_count} "
                             f"+{new_start},{new_count} @@")
                line = new_start
                added_seen = 0
                for kind in types:
                    if kind == "-":
                        parts.append("-old_value = 0")
                        continue
                    if kind == "+":
                        if hunk == secret_hunk and added_seen == secret_slot:
                            parts.append("+" + secret)
                            expected = (path, line)
                        else:
                            parts.append(f"+value_{hunk}_{added_seen} = 1")
                        added_seen += 1
                    else:
                        parts.append(" context = 1")
                    line += 1
            findings = scan_diff("\n".join(parts))["findings"]
            self.assertTrue(
                any(item["file"] == expected[0] and item["line"] == expected[1]
                    and item["kind"] == "aws_access_key" for item in findings),
                (seed, expected, findings))

    def test_malformed_hunk_numbers_are_reported_not_raised(self):
        huge = "9" * 5000
        diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                f"@@ -0,0 +{huge} @@\n+x\n")
        report = scan_diff(diff)
        self.assertTrue(report["malformed"])
        self.assertEqual(report["findings"], [])

    def test_an_oversized_old_start_is_malformed_too(self):
        diff = ("--- a/x\n+++ b/x\n"
                "@@ -1234567890,1 +1,1 @@\n-a\n+b\n")
        report = scan_diff(diff)
        self.assertTrue(report["malformed"])
