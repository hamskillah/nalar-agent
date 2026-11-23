"""High-confidence credential detection for proposed patches.

Only added lines are scanned, findings never include the secret value, and the
scan is bounded. This is a guardrail against accidentally writing credentials
into a repository, not a complete secret scanner.
"""

import ast
import re

from .workspace import ToolError

MAX_SCAN = 2_000_000
MAX_FINDINGS = 20
MAX_HUNK_DIGITS = 9
MAX_TREE_FILES = 2_000
MAX_TREE_BYTES = 8_000_000

PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("database_url", re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)"
        r"://[^\s:@/]+:[^\s@/]+@")),
    ("secret_assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|secret|password|passwd|token|credential)"
        r"(?:_(?:KEY|ACCESS_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS))*"
        r"\s*(?::=|[:=])\s*"
        r"(?:[\"'][^\"']{16,}[\"']|"
        r"(?![/.~]|[A-Za-z]:[\\/])"
        r"(?![A-Za-z_]{1,40}(?:\.[A-Za-z_]{1,40}){1,4}(?:\W|$))"
        r"(?![A-Za-z_][\w.]*\()"
        r"[^\s\"'#]{16,})")),
)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT_PATH = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _header_path(value):
    value = value.strip()
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return ""
        try:
            # Git C-quotes non-ASCII path bytes as octal escapes.
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    else:
        # Plain `diff -u` headers append a tab-separated timestamp.
        value = value.split("\t", 1)[0].rstrip()
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
        # Never surface a local absolute directory from an untrusted patch.
        value = value.replace("\\", "/").rsplit("/", 1)[-1]
    return value


def _hunk_int(value, default):
    if value is None:
        return default
    if len(value) > MAX_HUNK_DIGITS:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def scan_diff(diff):
    """Return findings and bounded scan flags for added lines."""
    if not diff:
        return {"findings": [], "truncated": False, "capped": False,
                "malformed": False}
    truncated = len(diff) > MAX_SCAN
    malformed = False
    findings = []
    seen = set()
    dropped = False
    path = ""
    line = 0
    in_hunk = False
    old_left = new_left = 0
    # split("\n") only: str.splitlines() also splits on form feed/U+2028, which
    # would end the hunk early and hide later added lines.
    for raw in diff[:MAX_SCAN].split("\n"):
        if in_hunk:
            header_like = False
            if raw.startswith("+"):
                content = raw[1:]
                if new_left > 0:
                    new_left -= 1
                    for kind, pattern in PATTERNS:
                        if pattern.search(content):
                            key = (path, line, kind)
                            if key not in seen:
                                if len(findings) >= MAX_FINDINGS:
                                    dropped = True
                                else:
                                    seen.add(key)
                                    findings.append({"file": path or "(unknown)",
                                                     "line": line, "kind": kind})
                            break
                    line += 1
            elif raw.startswith("-"):
                if old_left > 0:
                    old_left -= 1
            elif raw.startswith(" "):
                if old_left > 0:
                    old_left -= 1
                if new_left > 0:
                    new_left -= 1
                line += 1
            elif raw.startswith("\\"):
                pass
            elif raw.startswith(("@@", "diff --git ")):
                in_hunk = False
                header_like = True
            else:
                in_hunk = False
                continue
            if in_hunk and old_left <= 0 and new_left <= 0:
                in_hunk = False
            if not header_like:
                continue
        if raw.startswith("diff --git "):
            path = ""
            match = GIT_PATH.match(raw)
            if match:
                path = match.group(2)
        elif raw.startswith("+++ "):
            target = _header_path(raw[4:])
            if target == "/dev/null":
                path = ""
            elif target.startswith("b/"):
                path = target[2:]
            elif target.startswith("a/"):
                path = target[2:]
            else:
                path = target
        elif match := HUNK.match(raw):
            old_start = _hunk_int(match.group(1), 0)
            old_left = _hunk_int(match.group(2), 1)
            new_left = _hunk_int(match.group(4), 1)
            line = _hunk_int(match.group(3), 0)
            if (old_start is None or old_left is None or new_left is None
                    or line is None):
                malformed = True
                in_hunk = False
            else:
                in_hunk = old_left > 0 or new_left > 0
    return {"findings": findings, "truncated": truncated, "capped": dropped,
            "malformed": malformed}

def scan_text(text, path=""):
    """Scan plain text, returning findings with one-based line numbers."""
    findings = []
    for number, line in enumerate(text.split("\n"), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                findings.append({"file": path or "(unknown)", "line": number,
                                 "kind": kind})
                break
    return findings


def scan_repository(repository, *, max_files=MAX_TREE_FILES,
                    max_bytes=MAX_TREE_BYTES, max_file_bytes=2_000_000):
    """Scan all committed text blobs without the edit-tool path filter."""
    findings = []
    seen = set()
    files = 0
    skipped = 0
    binary = 0
    total = 0
    truncated = False
    for path in sorted(repository.blobs):
        if files >= max_files or total >= max_bytes:
            truncated = True
            break
        try:
            text = repository.read_committed_blob(path, max_file_bytes)
        except ToolError as exc:
            if str(exc) == "too_large":
                skipped += 1
            else:
                binary += 1
            continue
        files += 1
        total += len(text.encode("utf-8"))
        for item in scan_text(text, path):
            key = (item["file"], item["line"], item["kind"])
            if key in seen:
                continue
            seen.add(key)
            if len(findings) >= MAX_FINDINGS:
                return {"findings": findings, "truncated": truncated,
                        "capped": True, "files": files, "skipped": skipped,
                        "binary": binary}
            findings.append(item)
    return {"findings": findings, "truncated": truncated, "capped": False,
            "files": files, "skipped": skipped, "binary": binary}

def sarif(report, *, scope="", incomplete=False):
    """Render scan findings as SARIF 2.1.0 without exposing any value."""
    rules = []
    results = []
    used = set()
    for item in report["findings"]:
        kind = item["kind"]
        rule_id = f"nalar/credential/{kind}"
        if kind not in used:
            used.add(kind)
            rules.append({"id": rule_id, "name": kind,
                          "shortDescription": {
                              "text": f"Potential {kind.replace('_', ' ')}"}})
        results.append({
            "ruleId": rule_id,
            "level": "error",
            "message": {"text": (f"Potential {kind.replace('_', ' ')} in an "
                                 "added line; the value is not shown.")},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": item["file"]},
                "region": {"startLine": item["line"]},
            }}],
        })
    invocation = {"executionSuccessful": not incomplete}
    if incomplete:
        notes = []
        if report.get("truncated"):
            notes.append("scan size limit reached")
        if report.get("skipped"):
            notes.append(f"{report['skipped']} oversize committed file(s) skipped")
        if report.get("skipped_untracked"):
            notes.append(f"{report['skipped_untracked']} untracked file(s) skipped")
        if report.get("capped"):
            notes.append("additional findings were capped")
        if report.get("malformed"):
            notes.append("a hunk header is malformed")
        invocation["toolExecutionNotifications"] = [{
            "level": "error",
            "message": {"text": "Scan incomplete: " + "; ".join(notes or ["limits reached"])},
        }]
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "nalar", "rules": rules}},
            "invocations": [invocation],
            "results": results,
            "properties": {
            "scope": scope, "incomplete": incomplete,
            "files": report.get("files", 0),
            "skipped": report.get("skipped", 0),
            "binary": report.get("binary", 0),
            "truncated": bool(report.get("truncated")),
            "capped": bool(report.get("capped")),
            "malformed": bool(report.get("malformed")),
            "skipped_untracked": report.get("skipped_untracked", 0),
            "unsafe_untracked": report.get("unsafe_untracked", 0),
        },
        }],
    }
