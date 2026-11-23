"""Split and filter unified diffs at hunk granularity.

The TUI uses this to let a reviewer accept only some hunks. A file section is
kept only when at least one of its hunks is selected, so a header can never be
applied without a change. Section detection is shared with `nalar.apply`, which
skips hunk bodies by their declared counts.
"""

from .apply import ApplyError, _section_path, sections

MODE_PREFIXES = ("old mode ", "new mode ")


def is_mode(line):
    return line.startswith(MODE_PREFIXES)


def split(diff):
    """Return one dict per file: {path, header, hunks} (text fragments)."""
    lines, starts = sections(diff)
    files = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        item = {"header": [], "hunks": [], "mode": []}
        for line in lines[start:end]:
            if line.startswith("@@ "):
                item["hunks"].append([line])
            elif item["hunks"]:
                item["hunks"][-1].append(line)
            else:
                item["header"].append(line)
        item["mode"] = [line for line in item["header"] if is_mode(line)]
        sample = item["header"] + (item["hunks"][0] if item["hunks"] else [])
        try:
            item["path"] = _section_path(sample)
        except ApplyError:
            item["path"] = ""
        files.append(item)
    return files


def render(files, selected):
    """Join the selected (file_index, hunk_index) pairs into a patch."""
    parts = []
    for file_index, item in enumerate(files):
        hunks = [hunk for hunk_index, hunk in enumerate(item["hunks"])
                 if (file_index, hunk_index) in selected]
        if not item["hunks"]:
            # Sections without hunks (binary or mode-only) cannot be split;
            # include them only when the reviewer selected the whole file.
            if (file_index, None) in selected:
                parts.append("".join(item["header"]))
            continue
        mode_selected = (file_index, "mode") in selected
        if not hunks and not mode_selected:
            continue
        header = []
        for line in item["header"]:
            if is_mode(line) and not mode_selected:
                continue
            if not hunks and line.startswith(("--- ", "+++ ")):
                # A mode-only selection must not leave a header without a hunk.
                continue
            header.append(line)
        parts.append("".join(header))
        for hunk in hunks:
            parts.append("".join(hunk))
    return "".join(parts)


def all_hunks(files):
    keys = {(file_index, hunk_index) for file_index, item in enumerate(files)
            for hunk_index in range(len(item["hunks"]))}
    for file_index, item in enumerate(files):
        if not item["hunks"]:
            keys.add((file_index, None))
        elif item.get("mode"):
            keys.add((file_index, "mode"))
    return keys
