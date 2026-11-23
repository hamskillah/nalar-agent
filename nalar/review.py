"""Read-only review of a Git diff with structured findings.

The model sees a bounded diff and returns findings; nothing is written and no
tools are exposed, so review cannot change the repository.
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

from .agent import RETRYABLE
from .coding import _safe_path, git_environment
from .core import ProviderError

MAX_DIFF = 200_000
MAX_FINDINGS = 50
MAX_UNTRACKED_FILES = 20
MAX_UNTRACKED_BYTES = 32_768
SEVERITIES = {"high", "medium", "low"}
SYSTEM = """You review code changes. Report only concrete defects: incorrect behavior,
security problems, data loss, race conditions, broken error handling, or missing
tests that hide a defect. Ignore style preferences and praise.
Return ONLY a JSON array, with no prose and no code fences. Each item:
{"file": "path", "line": 0, "severity": "high|medium|low", "title": "short", "detail": "why and how to fix"}.
Return [] when the diff has no defect."""


class ReviewError(ValueError):
    """The requested diff cannot be reviewed."""


def _untracked_diff(root, budget, stats=None):
    """New-file diffs for safe, small untracked text files."""
    stats = stats if stats is not None else {}
    try:
        raw = subprocess.run(
            ["git", "--no-pager", "-C", str(root), "ls-files", "--others",
             "--exclude-standard", "-z"],
            env=git_environment(), check=True, capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    names = [name for name in raw.split(b"\0") if name]
    if budget <= 0:
        stats["skipped"] = stats.get("skipped", 0) + len(names)
        return ""
    extra = max(0, len(names) - MAX_UNTRACKED_FILES)
    if extra:
        stats["skipped"] = stats.get("skipped", 0) + extra
    names = names[:MAX_UNTRACKED_FILES]
    blocks = []
    used = 0
    for index, name in enumerate(names):
        relative = os.fsdecode(name)
        if not _safe_path(relative):
            stats["unsafe"] = stats.get("unsafe", 0) + 1
            continue
        path = Path(root) / relative
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                stats["unsafe"] = stats.get("unsafe", 0) + 1
                continue
            if info.st_size > MAX_UNTRACKED_BYTES:
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            if b"\0" in path.read_bytes():
                stats["unsafe"] = stats.get("unsafe", 0) + 1
                continue
        except OSError:
            stats["unsafe"] = stats.get("unsafe", 0) + 1
            continue
        try:
            block = subprocess.run(
                ["git", "--no-pager", "-C", str(root), "diff", "--no-index",
                 "--no-color", "--no-ext-diff", "--no-textconv", "--",
                 os.devnull, relative],
                env=git_environment(), capture_output=True, timeout=10).stdout.decode(
                    "utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            stats["unsafe"] = stats.get("unsafe", 0) + 1
            continue
        if not block:
            stats["empty"] = stats.get("empty", 0) + 1
            continue
        if used + len(block) > budget:
            stats["skipped"] = stats.get("skipped", 0) + len(names) - index
            break
        blocks.append(block if block.endswith("\n") else block + "\n")
        used += len(block)
    return "".join(blocks)


def collect_diff(root, scope="worktree", ref="HEAD", include_untracked=False,
                 allow_empty=False, max_diff=MAX_DIFF, too_large=None,
                 stats=None):
    root = Path(root).resolve()
    # Repository diff drivers, textconv filters and external diff tools execute
    # code. None of them are needed to review text, so disable all three.
    common = ("--no-color", "--no-ext-diff", "--no-textconv")
    if scope == "staged":
        args = ("diff", "--cached", *common)
    elif scope == "branch":
        args = ("diff", *common, f"{ref}...HEAD")
    else:
        args = ("diff", *common)
    try:
        raw = subprocess.run(["git", "--no-pager", "-C", str(root), *args],
                             env=git_environment(), check=True, capture_output=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        raise ReviewError("cannot read the repository diff") from None
    diff = raw.decode("utf-8", "replace")
    if include_untracked:
        diff += _untracked_diff(root, max_diff - len(diff), stats)
    if not diff.strip():
        if allow_empty:
            return scope, ""
        raise ReviewError("no changes to review for this scope")
    if len(diff) > max_diff:
        raise ReviewError(too_large or "diff is too large to review; narrow the scope")
    return scope, diff


def _parse(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, list):
        return None
    if len(value) > MAX_FINDINGS:
        return None
    findings = []
    for item in value:
        if not isinstance(item, dict):
            return None
        file = item.get("file")
        title = item.get("title")
        detail = item.get("detail", "")
        severity = item.get("severity")
        line = item.get("line", 0)
        if (not isinstance(file, str) or not 1 <= len(file) <= 300
                or not isinstance(title, str) or not 1 <= len(title) <= 200
                or not isinstance(detail, str) or len(detail) > 2_000
                or severity not in SEVERITIES or type(line) is not int or line < 0):
            return None
        findings.append({"file": file, "line": line, "severity": severity,
                         "title": title, "detail": detail})
    unique = []
    seen = set()
    for item in findings:
        key = (item["file"], item["line"], item["severity"], item["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _ask(chat, messages, *, retries=2, sleep=time.sleep, budget=None):
    for attempt in range(retries + 1):
        try:
            reply = chat(messages, [])
            if budget is not None and reply.tokens:
                budget.add(reply.tokens)
            return reply
        except ProviderError as exc:
            code = str(exc)
            transient = (code in RETRYABLE or code.startswith("http_5")
                         or code == "http_429")
            if not transient or attempt == retries:
                raise
            delay = exc.retry_after if exc.retry_after is not None else 0.5 * (2 ** attempt)
            sleep(min(delay, 60))


def review(chat, diff, *, model, retries=2, sleep=time.sleep, budget=None,
           context=""):
    content = f"Review this diff:\n\n{diff}"
    if context:
        content += "\n\n" + str(context)[:32_000]
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": content}]
    reply = _ask(chat, messages, retries=retries, sleep=sleep, budget=budget)
    findings = _parse(reply.text or "")
    return {
        "status": "ok" if findings is not None else "unparsed",
        "model": model,
        "findings": findings or [],
        "text": "" if findings is not None else (reply.text or "")[:4_000],
        "tokens": reply.tokens,
    }


SARIF_LEVELS = {"high": "error", "medium": "warning", "low": "note"}


def sarif(outcome, *, model="", scope=""):
    """Render structured findings as SARIF 2.1.0 for CI code scanning."""
    results = []
    used = set()
    for item in outcome.get("findings", []):
        severity = item.get("severity") if item.get("severity") in SEVERITIES else "low"
        line = item.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            line = 1
        title = str(item.get("title", ""))
        detail = str(item.get("detail", ""))
        message = title if not detail else f"{title}: {detail}"
        results.append({
            "ruleId": f"nalar/{severity}",
            "level": SARIF_LEVELS[severity],
            "message": {"text": message},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": str(item.get("file", ""))},
                "region": {"startLine": line},
            }}],
        })
        used.add(severity)
    rules = [{"id": f"nalar/{severity}", "name": severity,
              "shortDescription": {"text": f"Nalar {severity} finding"}}
             for severity in ("high", "medium", "low") if severity in used]
    invocation = {"executionSuccessful": outcome.get("status") == "ok"}
    if outcome.get("status") != "ok":
        invocation["toolExecutionNotifications"] = [{
            "level": "error",
            "message": {"text": "The review response could not be parsed."},
        }]
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "nalar", "rules": rules}},
            "invocations": [invocation],
            "results": results,
            "properties": {"model": model, "scope": scope},
        }],
    }
