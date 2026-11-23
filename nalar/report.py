"""Markdown renderers for review findings and credential scans.

Both renderers escape model- or repository-controlled text so a finding cannot
break out of a table cell, inject HTML, or turn into a misleading block. They
never include credential values.
"""

import re

MAX_CELL = 600
MAX_RAW = 2_000
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_])")
CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u200b\u200e\u200f\u202a-\u202e"
    r"\u2060\u2066-\u2069\ufeff\u2028\u2029\U000e0000-\U000e007f]")
_CELL = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "[": "&#91;",
         "]": "&#93;", "(": "&#40;", ")": "&#41;", "`": "&#96;",
         "!": "&#33;", "*": "&#42;", "_": "&#95;", "~": "&#126;",
         "#": "&#35;", "|": "&#124;", "\\": "&#92;"}


def _plain(value):
    text = CONTROL.sub(" ", ANSI.sub("", str(value)))
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _findings(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def cell(value, limit=MAX_CELL):
    return "".join(_CELL.get(char, char) for char in _plain(value))[:limit]


def review_markdown(outcome, *, model="", scope=""):
    lines = ["## Nalar review", "",
             f"- Scope: {cell(scope, 200)}",
             f"- Model: {cell(model, 200)}"]
    status = str(outcome.get("status", "unknown"))
    if status != "ok":
        lines += ["", f"**Review status: {cell(status, 40)}.** The response could "
                      "not be parsed; treat this diff as unreviewed."]
        text = str(outcome.get("text") or "")[:MAX_RAW]
        if text:
            quoted = "\n".join("> " + cell(line, 300)
                               for line in text.splitlines()[:40])
            lines += ["", quoted]
        return "\n".join(lines) + "\n"
    findings = _findings(outcome.get("findings"))
    if not findings:
        lines += ["", "No concrete defects reported."]
        return "\n".join(lines) + "\n"
    lines += ["", "| Severity | Location | Finding |", "| --- | --- | --- |"]
    for item in findings:
        severity = cell(item.get("severity", "low"), 16)
        location = cell(f"{item.get('file', '')}:{item.get('line', '')}", 240)
        finding = cell(f"{item.get('title', '')} — {item.get('detail', '')}")
        lines.append(f"| {severity} | {location} | {finding} |")
    return "\n".join(lines) + "\n"


def scan_markdown(report, *, scope=""):
    findings = _findings(report.get("findings"))
    lines = ["## Credential scan", "", f"- Scope: {cell(scope, 200)}",
             f"- Findings: {len(findings)}"]
    if "files" in report:
        lines.append(f"- Committed text files scanned: {int(report['files'])}")
    if findings:
        lines += ["", "| File | Line | Kind |", "| --- | --- | --- |"]
        for item in findings:
            lines.append(f"| {cell(item.get('file', ''), 240)} | "
                         f"{cell(item.get('line', ''), 12)} | "
                         f"{cell(item.get('kind', ''), 64)} |")
    else:
        lines += ["", "No high-confidence credentials detected."]
    notes = []
    if report.get("binary"):
        notes.append(f"{int(report['binary'])} binary file(s) skipped.")
    if report.get("skipped"):
        notes.append(f"{int(report['skipped'])} oversize committed file(s) skipped.")
    if report.get("skipped_untracked"):
        notes.append(f"{int(report['skipped_untracked'])} untracked file(s) skipped.")
    if report.get("unsafe_untracked"):
        notes.append(f"{int(report['unsafe_untracked'])} unsafe untracked file(s) skipped.")
    if report.get("truncated"):
        notes.append("The scan stopped at its size limit; the rest was not scanned.")
    if report.get("capped"):
        notes.append("Additional findings were capped.")
    if report.get("malformed"):
        notes.append("A hunk header is malformed; results may be incomplete.")
    if notes:
        lines += ["", "**Incomplete:** " + " ".join(notes)]
    return "\n".join(lines) + "\n"


def _gh(value, property_=False):
    text = _plain(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def _gh_line(value):
    value = value if (isinstance(value, int) and not isinstance(value, bool)
                      and value >= 1) else 1
    return min(value, 999_999_999)


def review_github(outcome):
    """Render findings as GitHub Actions workflow annotations."""
    status = str(outcome.get("status", "unknown"))
    if status != "ok":
        message = (f"Nalar review {status}: the response could not be parsed; "
                   "treat this diff as unreviewed.")
        return ("::error title=" + _gh("Nalar review", True) + "::"
                + _gh(message) + "\n")
    lines = []
    for item in _findings(outcome.get("findings")):
        severity = str(item.get("severity", "low"))
        level = {"high": "error", "medium": "warning", "low": "notice"}.get(
            severity, "notice")
        title = f"Nalar {severity}: {item.get('title', '')}"
        lines.append(
            f"::{level} file={_gh(item.get('file', ''), True)[:240]},"
            f"line={_gh_line(item.get('line'))},"
            f"title={_gh(title, True)[:240]}::"
            f"{_gh(str(item.get('detail', ''))[:1000])}")
    return "\n".join(lines) + ("\n" if lines else "")


def scan_github(report, *, incomplete=False):
    """Render credential findings as GitHub Actions workflow annotations."""
    lines = []
    for item in _findings(report.get("findings")):
        kind = str(item.get("kind", "")).replace("_", " ")
        message = f"Potential {kind} in an added line; the value is not shown."
        lines.append(
            f"::error file={_gh(item.get('file', ''), True)[:240]},"
            f"line={_gh_line(item.get('line'))},"
            f"title={_gh('Nalar credential', True)}::{_gh(message)}")
    if incomplete:
        lines.append("::warning title=" + _gh("Nalar scan", True) + "::"
                     + _gh("The scan is incomplete; narrow the scope or scan a patch."))
    return "\n".join(lines) + ("\n" if lines else "")
