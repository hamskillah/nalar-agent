import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"'


class ScanCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "--template=")
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def scan(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "nalar", "scan", "--repo", str(self.root), *args],
            cwd=ROOT, capture_output=True, text=True)

    def test_worktree_secret_is_reported_without_echoing_it(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["scope"], "worktree")
        self.assertEqual(payload["findings"],
                         [{"file": "app.py", "line": 2,
                           "kind": "aws_access_key"}])
        self.assertNotIn("AKIA", result.stdout)

        text = self.scan("--format", "text")
        self.assertEqual(text.returncode, 1)
        self.assertIn("app.py:2 aws_access_key", text.stdout)
        self.assertIn("1 potential credential(s)", text.stdout)
        self.assertNotIn("AKIA", text.stdout)

    def test_clean_and_empty_scopes_exit_zero(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["findings"], [])
        text = self.scan("--format", "text")
        self.assertEqual(text.returncode, 0)
        self.assertIn("No credentials found", text.stdout)

    def test_staged_scope_is_scanned_separately_from_the_worktree(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        self.git("add", "app.py")
        worktree = json.loads(self.scan().stdout)
        self.assertEqual(worktree["findings"], [])
        staged = self.scan("--scope", "staged")
        self.assertEqual(staged.returncode, 1, staged.stderr)
        self.assertEqual(json.loads(staged.stdout)["findings"][0]["file"], "app.py")

    def test_untracked_files_are_optional(self):
        (self.root / "new.py").write_text(
            'TOKEN = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
            encoding="utf-8")
        clean = self.scan()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        found = self.scan("--include-untracked")
        self.assertEqual(found.returncode, 1, found.stderr)
        findings = json.loads(found.stdout)["findings"]
        self.assertEqual(findings[0]["file"], "new.py")
        self.assertEqual(findings[0]["kind"], "github_token")

    def test_committed_tree_scan_ignores_the_worktree_and_supports_refs(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        clean = self.scan("--scope", "tree")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["files"], 1)
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "secret")
        found = self.scan("--scope", "tree")
        self.assertEqual(found.returncode, 1, found.stderr)
        payload = json.loads(found.stdout)
        self.assertEqual(payload["findings"][0]["file"], "app.py")
        self.assertEqual(payload["files"], 1)
        text = self.scan("--scope", "tree", "--format", "text")
        self.assertIn("committed tree", text.stdout)
        self.assertIn("Scanned 1 committed text file(s).", text.stdout)
        previous = self.scan("--scope", "tree", "--ref", "HEAD~1")
        self.assertEqual(previous.returncode, 0, previous.stderr)
        self.assertEqual(json.loads(previous.stdout)["findings"], [])

    def test_tree_scan_covers_dotfiles_keys_and_large_text(self):
        (self.root / ".env").write_text(
            'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n', encoding="utf-8")
        certs = self.root / "certs"
        certs.mkdir()
        (certs / "ci.key").write_text(
            'TOKEN = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
            encoding="utf-8")
        data = self.root / "data"
        data.mkdir()
        (data / "large.txt").write_text(
            "x" * 100_000 + f"\n{SECRET}\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\x00" + SECRET.encode())
        self.git("add", ".")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "coverage")
        result = self.scan("--scope", "tree", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual({item["file"] for item in payload["findings"]},
                         {".env", "certs/ci.key", "data/large.txt"})
        self.assertGreaterEqual(payload["binary"], 1)
        self.assertEqual(payload["files"], 4)

    def test_include_untracked_is_rejected_outside_the_worktree(self):
        result = self.scan("--scope", "tree", "--include-untracked")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--include-untracked", result.stderr)

    def test_benign_untracked_symlinks_are_not_incomplete(self):
        target = self.root / "target.txt"
        target.write_text("safe\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(target)
        result = self.scan("--include-untracked")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload.get("unsafe_untracked", 0), 1)
        self.assertNotIn("skipped_untracked", payload)

    def test_tree_scan_skips_binary_text(self):
        (self.root / "data.bin").write_bytes(
            b'\x00\x01AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
        self.git("add", "data.bin")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "binary")
        result = self.scan("--scope", "tree")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["files"], 1)

    def test_branch_scope_requires_an_explicit_ref(self):
        result = self.scan("--scope", "branch")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--ref", result.stderr)

    def test_branch_scope_scans_the_named_range(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "secret")
        found = self.scan("--scope", "branch", "--ref", "HEAD~1")
        self.assertEqual(found.returncode, 1, found.stderr)
        self.assertEqual(json.loads(found.stdout)["findings"][0]["file"], "app.py")

    def test_large_diffs_are_scanned_not_rejected(self):
        filler = "x" * 250_000
        (self.root / "app.py").write_text(
            f"answer = 42\n# {filler}\n{SECRET}\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"][0]["line"], 3)

    def test_untracked_limit_is_reported_as_incomplete(self):
        (self.root / "big.py").write_text(
            "x" * 33_000 + f"\n{SECRET}\n", encoding="utf-8")
        result = self.scan("--include-untracked")
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["skipped_untracked"], 1)
        self.assertFalse(payload["ok"])
        text = self.scan("--include-untracked", "--format", "text")
        self.assertEqual(text.returncode, 2)
        self.assertIn("Skipped", text.stderr)

    def test_scan_can_emit_sarif_locations(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        result = self.scan("--format", "sarif")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["version"], "2.1.0")
        run = payload["runs"][0]
        self.assertTrue(run["invocations"][0]["executionSuccessful"])
        self.assertEqual(run["results"][0]["ruleId"],
                         "nalar/credential/aws_access_key")
        location = run["results"][0]["locations"][0]["physicalLocation"]
        self.assertEqual(location["artifactLocation"]["uri"], "app.py")
        self.assertEqual(location["region"]["startLine"], 2)
        self.assertNotIn("AKIA", result.stdout)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        clean = self.scan("--format", "sarif")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["runs"][0]["results"], [])

    def test_tree_sarif_reports_scope_and_file_counts(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        self.git("add", "app.py")
        self.git("-c", "user.name=F", "-c", "user.email=f@example.test",
                 "commit", "-qm", "secret")
        result = self.scan("--scope", "tree", "--format", "sarif")
        self.assertEqual(result.returncode, 1, result.stderr)
        run = json.loads(result.stdout)["runs"][0]
        self.assertEqual(run["properties"]["scope"], "tree")
        self.assertGreaterEqual(run["properties"]["files"], 1)
        self.assertTrue(run["results"])

    def test_sarif_marks_incomplete_scans(self):
        (self.root / "big.py").write_text(
            "x" * 33_000 + f"\n{SECRET}\n", encoding="utf-8")
        result = self.scan("--include-untracked", "--format", "sarif")
        self.assertEqual(result.returncode, 2, result.stderr)
        run = json.loads(result.stdout)["runs"][0]
        self.assertFalse(run["invocations"][0]["executionSuccessful"])
        note = run["invocations"][0]["toolExecutionNotifications"][0]
        self.assertIn("incomplete", note["message"]["text"].lower())
        self.assertEqual(run["results"], [])

    def test_a_diff_over_the_scan_limit_is_incomplete(self):
        (self.root / "app.py").write_text(
            "x" * 2_100_000 + f"\n{SECRET}\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["truncated"])
        self.assertFalse(payload["ok"])
        text = self.scan("--format", "text")
        self.assertEqual(text.returncode, 2)
        self.assertIn("size limit", text.stderr)

    def test_a_non_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "nalar", "scan", "--repo", tmp],
                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("repository diff", result.stderr)

    def test_saved_patch_is_scanned_without_applying_it(self):
        patch = self.root / "change.diff"
        patch.write_text(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1,2 @@\n answer = 41\n+" + SECRET + "\n", encoding="utf-8")
        result = self.scan("--file", str(patch))
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["scope"], "file")
        self.assertEqual(payload["findings"],
                         [{"file": "app.py", "line": 2,
                           "kind": "aws_access_key"}])
        self.assertNotIn("AKIA", result.stdout)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"),
                         "answer = 41\n")
        text = self.scan("--file", str(patch), "--format", "text")
        self.assertEqual(text.returncode, 1)
        self.assertIn("the saved patch", text.stdout)
        self.assertNotIn("AKIA", text.stdout)
        sarif = self.scan("--file", str(patch), "--format", "sarif")
        self.assertEqual(sarif.returncode, 1)
        run = json.loads(sarif.stdout)["runs"][0]
        self.assertEqual(run["properties"]["scope"], "file")
        self.assertTrue(run["results"])

    def test_markdown_output_lists_findings_without_values(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        result = self.scan("--format", "markdown")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("## Credential scan", result.stdout)
        self.assertIn("| app.py | 2 | aws&#95;access&#95;key |", result.stdout)
        self.assertIn("- Findings: 1", result.stdout)
        self.assertNotIn("AKIA", result.stdout)

    def test_clean_saved_patch_exits_zero(self):
        patch = self.root / "clean.diff"
        patch.write_text(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1 @@\n-answer = 41\n+answer = 42\n", encoding="utf-8")
        result = self.scan("--file", str(patch), "--format", "text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No credentials found in the saved patch.", result.stdout)

    def test_file_rejects_conflicting_scope_flags(self):
        patch = self.root / "change.diff"
        patch.write_text(
            "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
            encoding="utf-8")
        for args in (("--scope", "tree"), ("--scope", "branch", "--ref", "HEAD"),
                     ("--include-untracked",)):
            result = self.scan("--file", str(patch), *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--file", result.stderr)

    def test_file_rejects_missing_symlink_and_non_diff(self):
        missing = self.scan("--file", str(self.root / "missing.diff"))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("regular file", missing.stderr)
        target = self.root / "target.diff"
        target.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n",
                          encoding="utf-8")
        link = self.root / "link.diff"
        link.symlink_to(target)
        linked = self.scan("--file", str(link))
        self.assertEqual(linked.returncode, 2)
        self.assertIn("symlink", linked.stderr)
        plain = self.root / "plain.txt"
        plain.write_text("just text\n", encoding="utf-8")
        invalid = self.scan("--file", str(plain))
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("unified diff", invalid.stderr)

    def test_plain_diff_timestamps_are_stripped_from_paths(self):
        patch = self.root / "ts.diff"
        patch.write_text(
            "--- a/app.py\t2026-11-21 10:00:00 +0000\n"
            "+++ b/app.py\t2026-11-21 10:01:00 +0000\n"
            "@@ -1 +1,2 @@\n x\n+" + SECRET + "\n", encoding="utf-8")
        result = self.scan("--file", str(patch))
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["findings"],
                         [{"file": "app.py", "line": 2,
                           "kind": "aws_access_key"}])
        sarif = self.scan("--file", str(patch), "--format", "sarif")
        run = json.loads(sarif.stdout)["runs"][0]
        self.assertEqual(
            run["results"][0]["locations"][0]["physicalLocation"]
            ["artifactLocation"]["uri"], "app.py")

    def test_scan_text_strips_terminal_escapes_from_paths(self):
        patch = self.root / "esc.diff"
        patch.write_text(
            "--- a/x.py\n+++ b/\x1b[31mEVIL\x1b[0m.py\n"
            "@@ -1 +1,2 @@\n x\n+" + SECRET + "\n", encoding="utf-8")
        result = self.scan("--file", str(patch), "--format", "text")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("EVIL.py", result.stdout)
        self.assertNotIn("\x1b", result.stdout)

    def test_capped_findings_are_incomplete_everywhere(self):
        lines = "\n".join(f'+K{i} = "AKIA{i:016d}"' for i in range(21))
        patch = self.root / "many.diff"
        patch.write_text("--- a/x\n+++ b/x\n@@ -1,0 +1,21 @@\n"
                         + lines + "\n", encoding="utf-8")
        sarif = self.scan("--file", str(patch), "--format", "sarif")
        self.assertEqual(sarif.returncode, 1, sarif.stderr)
        run = json.loads(sarif.stdout)["runs"][0]
        self.assertFalse(run["invocations"][0]["executionSuccessful"])
        self.assertTrue(run["properties"]["capped"])
        note = run["invocations"][0]["toolExecutionNotifications"][0]
        self.assertIn("capped", note["message"]["text"])
        text = self.scan("--file", str(patch), "--format", "text")
        self.assertEqual(text.returncode, 1)
        self.assertIn("capped", text.stderr)

    def test_absolute_patch_paths_do_not_leak_local_directories(self):
        patch = self.root / "abs.diff"
        patch.write_text(
            "--- /tmp/old/app.py\n+++ /tmp/new/app.py\n"
            "@@ -1 +1,2 @@\n x\n+" + SECRET + "\n", encoding="utf-8")
        sarif = self.scan("--file", str(patch), "--format", "sarif")
        self.assertEqual(sarif.returncode, 1, sarif.stderr)
        run = json.loads(sarif.stdout)["runs"][0]
        self.assertEqual(
            run["results"][0]["locations"][0]["physicalLocation"]
            ["artifactLocation"]["uri"], "app.py")
        self.assertNotIn("/tmp", sarif.stdout)
        payload = json.loads(self.scan("--file", str(patch)).stdout)
        self.assertEqual(payload["findings"][0]["file"], "app.py")

    def test_github_output_is_annotation_ready_without_values(self):
        (self.root / "app.py").write_text(f"answer = 42\n{SECRET}\n",
                                          encoding="utf-8")
        result = self.scan("--format", "github")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("::error file=app.py,line=2,title=Nalar credential::", result.stdout)
        self.assertIn("Potential aws access key", result.stdout)
        self.assertNotIn("AKIA", result.stdout)

    def test_github_output_marks_incomplete_scans(self):
        (self.root / "big.py").write_text("x" * 33_000 + "\n", encoding="utf-8")
        result = self.scan("--include-untracked", "--format", "github")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("::warning title=Nalar scan::", result.stdout)
        self.assertIn("incomplete", result.stdout)

    def test_malformed_hunk_numbers_fail_closed_without_a_traceback(self):
        patch = self.root / "malformed.diff"
        patch.write_text(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -0,0 +" + "9" * 5000 + " @@\n+x = 1\n", encoding="utf-8")
        result = self.scan("--file", str(patch))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["malformed"])
        sarif = self.scan("--file", str(patch), "--format", "sarif")
        self.assertEqual(sarif.returncode, 2)
        run = json.loads(sarif.stdout)["runs"][0]
        self.assertTrue(run["properties"]["malformed"])
        self.assertFalse(run["invocations"][0]["executionSuccessful"])

    def test_multibyte_patches_are_bounded_in_bytes(self):
        patch = self.root / "multibyte.diff"
        patch.write_text(
            "--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n x\n+"
            + "é" * 1_050_000 + "\n", encoding="utf-8")
        result = self.scan("--file", str(patch))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("too large", result.stderr)

    def test_text_output_escapes_newline_in_a_finding_path(self):
        patch = self.root / "evil.patch"
        patch.write_text(
            'diff --git "a/evil\\nPWNED" "b/evil\\nPWNED"\n'
            '+++ "b/evil\\nPWNED"\n'
            "@@ -0,0 +1 @@\n"
            f"+{SECRET}\n", encoding="utf-8")
        result = self.scan("--file", str(patch), "--format", "text")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("evil\\nPWNED:1", result.stdout)
        self.assertNotIn("\nPWNED:1", result.stdout)
