"""Read a reviewed patch file and apply it with the same safety checks."""

import errno
import json
import os
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

MAX_PATCH = 10 * 1024 * 1024
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
            "v": "\v", "\\": "\\", '"': '"'}


class ApplyError(ValueError):
    pass


def _validate_patch(text, noun="file"):
    if not text.strip():
        raise ApplyError(f"The {noun} is empty.")
    if "diff --git " not in text and not ("--- " in text and "+++ " in text):
        raise ApplyError(f"The {noun} does not look like a unified diff.")
    return text


def read_patch(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ApplyError("The patch must be a regular file, not a symlink.")
    try:
        if target.stat().st_size > MAX_PATCH:
            raise ApplyError("The patch is too large.")
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ApplyError("The patch cannot be read as UTF-8 text.") from None
    return _validate_patch(text, "patch")


def read_patch_stream(stream, limit=MAX_PATCH):
    """Read one bounded patch from a stream (binary or text)."""
    try:
        data = stream.read(limit + 1)
    except (OSError, ValueError):
        raise ApplyError("The patch stream cannot be read.") from None
    if data is None:
        data = b""
    if isinstance(data, str):
        text = data
        if len(text) > limit:
            raise ApplyError("The patch stream is too large.")
    else:
        if len(data) > limit:
            raise ApplyError("The patch stream is too large.")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ApplyError("The patch stream cannot be read as UTF-8 text.") from None
    return _validate_patch(text, "patch stream")


def read_patch_source(path, stream=None):
    """Read a patch path, or stdin when the argument is exactly ``-``."""
    if str(path) == "-":
        return read_patch_stream(stream if stream is not None else sys.stdin.buffer)
    return read_patch(path)


def _quoted_end(value):
    """Index just past a C-quoted token at the start of `value`, or -1."""
    index = 1
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 4 if value[index + 1:index + 2].isdigit() else 2
            continue
        if char == '"':
            return index + 1
        index += 1
    return -1


def _unquote(value):
    body = value[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(body):
            raise ApplyError("A file path in the patch is malformed.")
        escape = body[index + 1]
        if escape in _ESCAPES:
            out.extend(_ESCAPES[escape].encode("utf-8"))
            index += 2
            continue
        digits = body[index + 1:index + 4]
        if len(digits) != 3 or any(digit not in "01234567" for digit in digits):
            raise ApplyError("A file path in the patch is malformed.")
        out.append(int(digits, 8))
        index += 4
    return out.decode("utf-8", "surrogateescape")


def _token(value):
    """Parse one `---`/`+++` payload: an optional quoted path and a date."""
    value = value.rstrip("\r\n")
    if value.startswith('"'):
        end = _quoted_end(value)
        if end < 0:
            raise ApplyError("A file path in the patch is malformed.")
        return _unquote(value[:end])
    return value.split("\t", 1)[0]


def _strip_prefix(value, prefix):
    if value == "/dev/null":
        return None
    return value[len(prefix):] if value.startswith(prefix) else value


def _header_paths(line):
    """Pre/post paths from a `diff --git` header."""
    rest = line[len("diff --git "):].rstrip("\r\n")
    tokens = []
    while rest.startswith('"'):
        end = _quoted_end(rest)
        if end < 0:
            raise ApplyError("A file path in the patch is malformed.")
        tokens.append(_unquote(rest[:end]))
        rest = rest[end:].lstrip()
        if len(tokens) == 2:
            break
    if tokens:
        if len(tokens) == 2 and not rest:
            return _strip_prefix(tokens[0], "a/"), _strip_prefix(tokens[1], "b/")
        raise ApplyError("A file path in the patch cannot be resolved.")
    if not rest.startswith("a/"):
        raise ApplyError("A file path in the patch cannot be resolved.")
    # Equal pre/post paths are the common case; split them exactly by length.
    if (len(rest) - 5) % 2 == 0:
        half = (len(rest) - 5) // 2
        if rest[2 + half:5 + half] == " b/" and rest[2:2 + half] == rest[5 + half:]:
            return rest[2:2 + half], rest[5 + half:]
    separator = rest.rfind(" b/", 2)
    if separator < 0:
        raise ApplyError("A file path in the patch cannot be resolved.")
    pre, post = rest[2:separator], rest[separator + 3:]
    if not pre or not post or " b/" in pre:
        raise ApplyError("A file path in the patch cannot be resolved.")
    return pre, post


def _hunk_counts(line):
    match = HUNK.match(line)
    if match is None:
        return None
    try:
        _hunk_number(match.group(1))
        _hunk_number(match.group(3))
        old = _hunk_number(match.group(2), 1)
        new = _hunk_number(match.group(4), 1)
    except ApplyError:
        return None
    return old, new


def _skip_hunk(lines, index):
    """`index` points at a `@@` header; return the index after its body."""
    counts = _hunk_counts(lines[index])
    if counts is None:
        raise ApplyError("A hunk header in the patch is malformed.")
    old_left, new_left = counts
    index += 1
    while index < len(lines) and (old_left > 0 or new_left > 0):
        body = lines[index]
        if body.startswith("\\"):
            index += 1
            continue
        if body.startswith(" "):
            old_left -= 1
            new_left -= 1
        elif body.startswith("-"):
            old_left -= 1
        elif body.startswith("+"):
            new_left -= 1
        elif body in ("\n", "\r\n"):
            old_left -= 1
            new_left -= 1
        else:
            break
        if old_left < 0 or new_left < 0:
            raise ApplyError("A hunk in the patch does not match its header.")
        index += 1
    if old_left > 0 or new_left > 0:
        raise ApplyError("A hunk in the patch ends early.")
    while index < len(lines) and lines[index].startswith("\\"):
        index += 1
    return index


def _validate_section(section):
    """A section may carry at most one file header with complete hunks."""
    seen = False
    index = 0
    while index < len(section):
        line = section[index]
        if (line.startswith("--- ") and index + 1 < len(section)
                and section[index + 1].startswith("+++ ")):
            if seen:
                raise ApplyError("A patch section contains more than one file header.")
            seen = True
            index += 2
            continue
        if line.startswith("@@"):
            index = _skip_hunk(section, index)
            continue
        index += 1


def sections(text):
    """Split a unified diff into file sections; return (lines, starts).

    Hunk bodies are skipped by their declared counts, so content lines that
    begin with `---`/`+++` cannot be mistaken for a file header.
    """
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines)
              if line.startswith("diff --git ")]
    if starts:
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            _validate_section(lines[start:end])
        return lines, starts
    starts = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (line.startswith("--- ") and index + 1 < len(lines)
                and lines[index + 1].startswith("+++ ")):
            starts.append(index)
            index += 2
            continue
        if line.startswith("@@"):
            index = _skip_hunk(lines, index)
            continue
        index += 1
    return lines, starts


def _hunk_number(value, default=0):
    if value is None:
        return default
    if len(value) > 9:
        raise ApplyError("A hunk header in the patch is malformed.")
    try:
        return int(value)
    except ValueError:
        raise ApplyError("A hunk header in the patch is malformed.") from None


def parse_sections(text):
    """Parse a unified diff into file sections with structured hunks.

    Only modifications of existing text files are accepted here: creating,
    deleting, renaming, binary and mode-changing sections raise ApplyError.
    Each hunk item is [op, body, no_newline] where op is " ", "-" or "+".
    """
    lines, starts = sections(text)
    if not starts:
        raise ApplyError("The patch has no file section.")
    parsed = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        section = lines[start:end]
        pre = post = None
        hunks = []
        index = 0
        while index < len(section):
            line = section[index]
            if line.startswith("diff --git "):
                index += 1
                continue
            if line.startswith(("new file mode ", "deleted file mode ", "old mode ",
                                "new mode ", "rename from ", "rename to ", "copy from ",
                                "copy to ", "GIT binary patch", "Binary files ")):
                raise ApplyError("Only modifications of existing text files can be staged.")
            if (line.startswith("--- ") and index + 1 < len(section)
                    and section[index + 1].startswith("+++ ")):
                if pre is not None:
                    raise ApplyError("A patch section contains more than one file header.")
                pre = _token(line[4:])
                post = _token(section[index + 1][4:])
                if pre == "/dev/null" or post == "/dev/null":
                    raise ApplyError("Creating or deleting files is not supported here.")
                pre_path = _strip_prefix(pre, "a/")
                post_path = _strip_prefix(post, "b/")
                if not pre_path or not post_path or pre_path != post_path:
                    raise ApplyError("Renames are not supported here.")
                index += 2
                continue
            if line.startswith("@@ "):
                match = HUNK.match(line)
                if match is None:
                    raise ApplyError("A hunk header in the patch is malformed.")
                old_start = _hunk_number(match.group(1))
                _hunk_number(match.group(3))
                old_count = _hunk_number(match.group(2), 1)
                new_count = _hunk_number(match.group(4), 1)
                index += 1
                items = []
                old_left, new_left = old_count, new_count
                while old_left > 0 or new_left > 0:
                    if index >= len(section):
                        raise ApplyError("A hunk in the patch ends early.")
                    body = section[index]
                    if body.startswith("\\"):
                        if not items:
                            raise ApplyError("A patch marker has no line.")
                        items[-1][2] = True
                        index += 1
                        continue
                    if body in ("\n", "\r\n"):
                        op, content, ending = " ", "", body
                    else:
                        op = body[0]
                        if op not in {" ", "-", "+"}:
                            raise ApplyError("A hunk in the patch is malformed.")
                        content = body[1:]
                        if content.endswith("\r\n"):
                            content, ending = content[:-2], "\r\n"
                        elif content.endswith("\n"):
                            content, ending = content[:-1], "\n"
                        else:
                            ending = ""
                    if op in {" ", "-"}:
                        old_left -= 1
                    if op in {" ", "+"}:
                        new_left -= 1
                    if old_left < 0 or new_left < 0:
                        raise ApplyError("A hunk in the patch does not match its header.")
                    items.append([op, content, False, ending])
                    index += 1
                while index < len(section) and section[index].startswith("\\"):
                    if not items:
                        raise ApplyError("A patch marker has no line.")
                    items[-1][2] = True
                    index += 1
                if not items:
                    raise ApplyError("A hunk in the patch is empty.")
                hunks.append({"old_start": old_start, "old_count": old_count,
                              "new_count": new_count, "items": items})
                continue
            index += 1
        if pre is None or post is None:
            raise ApplyError("A file section has no file header.")
        if not hunks:
            raise ApplyError("A file section has no hunks.")
        parsed.append({"path": post_path, "old_path": pre_path, "hunks": hunks})
    return parsed


def _section_paths(section):
    """Every path one section touches: post-image first, then pre-image."""
    start = 0 if section and not section[0].startswith("diff --git ") else 1
    pre = post = None
    for line in section[start:]:
        if line.startswith(("@@ ", "GIT binary patch", "Binary files ")):
            break
        if line.startswith("+++ "):
            post = _token(line[4:])
        elif line.startswith("--- ") and pre is None:
            pre = _token(line[4:])
        if pre is not None and post is not None:
            break
    paths = []
    if post is not None:
        resolved = _strip_prefix(post, "b/")
        if resolved:
            paths.append(resolved)
    if pre is not None:
        resolved = _strip_prefix(pre, "a/")
        if resolved and resolved not in paths:
            paths.append(resolved)
    if not paths and section and section[0].startswith("diff --git "):
        header = _header_paths(section[0])
        for resolved in (header[1], header[0]):
            if resolved and resolved not in paths:
                paths.append(resolved)
    if not paths:
        raise ApplyError("Cannot determine a file path in the patch; the patch is refused.")
    return paths


def _section_path(section):
    """The post-image path of one section, or the pre-image for deletions."""
    return _section_paths(section)[0]


def patch_paths(text):
    """Every path a patch touches, including both sides of renames."""
    lines, starts = sections(text)
    paths = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        for path in _section_paths(lines[start:end]):
            if path not in paths:
                paths.append(path)
    return paths


def patch_files(text):
    """Every file path a patch touches, in order (post-image or deletion)."""
    lines, starts = sections(text)
    files = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        path = _section_path(lines[start:end])
        if path not in files:
            files.append(path)
    return files


def filter_patch(text, include=(), exclude=()):
    """Keep only the file sections selected by include/exclude globs.

    Include/exclude patterns match any path a section touches; for a rename
    both the source and target match. Excludes win.
    Sections are never partially applied, so a hunk can never land without the
    rest of its file change. Filtering fails closed: a patch whose sections
    cannot be identified is refused rather than applied in full.
    """
    if not include and not exclude:
        return text
    lines, starts = sections(text)
    if not starts:
        raise ApplyError("Filtering needs file headers; apply the patch without filters.")
    preamble = "".join(lines[:starts[0]])
    kept = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        paths = _section_paths(lines[start:end])
        if any(fnmatch(path, pattern) for path in paths for pattern in exclude):
            continue
        if include and not any(fnmatch(path, pattern)
                               for path in paths for pattern in include):
            continue
        kept.append("".join(lines[start:end]))
    if not kept:
        raise ApplyError("No file in the patch matches the include/exclude filters.")
    return preamble + "".join(kept)


def check_patch_target(path):
    """Validate a `--patch-out` destination before spending a model run."""
    target = Path(path)
    if target.is_symlink():
        raise ApplyError("Refusing to write the patch through a symlink.")
    if target.exists() and not target.is_file():
        raise ApplyError("The patch output must be a regular file path.")
    parent = target.parent
    if not parent.is_dir():
        raise ApplyError("The patch output directory does not exist.")
    if not os.access(parent, os.W_OK):
        raise ApplyError("The patch output directory is not writable.")
    return target


def write_patch(path, text):
    """Save a patch atomically for `nalar apply`; never follow a symlink."""
    target = check_patch_target(path)
    data = text.encode("utf-8")
    if len(data) > MAX_PATCH:
        raise ApplyError("The patch is too large to save.")
    from .atomic import write as _atomic_write

    try:
        _atomic_write(target, data, mode=0o600, prefix=".nalar-patch-")
    except OSError:
        raise ApplyError("The patch file cannot be written.") from None
    return str(target)


LAST_PATCH = "nalar-last-apply.patch"
LAST_META = "nalar-last-apply.json"
MAX_RECORD_META = 64 * 1024


def _record_paths(repository):
    try:
        raw = repository._git("rev-parse", "--git-path", LAST_META).decode().strip()
    except (AttributeError, ValueError, UnicodeError):
        raise ApplyError("The repository has no Git metadata directory.") from None
    meta = Path(raw)
    if not meta.is_absolute():
        meta = repository.root / meta
    return meta, meta.with_name(LAST_PATCH)


def _read_regular(path, limit, label):
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ApplyError(f"The recorded apply {label} is a symlink; "
                             "revert manually.") from None
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise ApplyError("No recorded apply was found; revert with the original "
                             "patch file.") from None
        raise ApplyError(f"The recorded apply {label} cannot be opened; "
                         "revert manually.") from None
    try:
        info = os.fstat(handle)
        if (info.st_mode & 0o170000) != 0o100000:
            raise ApplyError(f"The recorded apply {label} is not a regular file; "
                             "revert manually.")
        if info.st_size > limit:
            raise ApplyError(f"The recorded apply {label} is too large; "
                             "revert manually.")
        with os.fdopen(handle, "rb") as stream:
            handle = None
            return stream.read(limit + 1)
    except OSError:
        raise ApplyError(f"The recorded apply {label} cannot be read; revert manually.") from None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass


def record_apply(repository, patch, files):
    """Best-effort record of the patch just applied, used by `apply --last`."""
    if not patch or len(patch) > MAX_PATCH:
        return False
    try:
        meta_path, patch_path = _record_paths(repository)
        payload = json.dumps({"base": repository.commit.decode(),
                              "files": list(files)},
                             ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(payload) > MAX_RECORD_META:
            return False
        from .atomic import write as _atomic_write

        _atomic_write(patch_path, patch.encode("utf-8"), mode=0o600,
                      prefix=".nalar-last-")
        _atomic_write(meta_path, payload, mode=0o600, prefix=".nalar-last-")
    except (OSError, ValueError, UnicodeError):
        return False
    return True


def read_last_apply(repository):
    """Read and validate the record written by the most recent successful apply."""
    meta_path, patch_path = _record_paths(repository)
    meta_raw = _read_regular(meta_path, MAX_RECORD_META, "metadata")
    patch_raw = _read_regular(patch_path, MAX_PATCH, "patch")
    try:
        meta = json.loads(meta_raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ApplyError("The recorded apply metadata is invalid; revert manually.") from None
    try:
        patch = patch_raw.decode("utf-8")
    except UnicodeError:
        raise ApplyError("The recorded apply patch is not valid UTF-8; revert manually.") from None
    if (not isinstance(meta, dict) or set(meta) != {"base", "files"}
            or not isinstance(meta["base"], str)
            or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", meta["base"])
            or not isinstance(meta["files"], list)
            or not all(isinstance(name, str) and name for name in meta["files"])):
        raise ApplyError("The recorded apply metadata is invalid; revert manually.")
    if not patch.strip() or ("diff --git " not in patch
                             and not ("--- " in patch and "+++ " in patch)):
        raise ApplyError("The recorded apply is invalid; revert manually.")
    try:
        expected = patch_paths(patch)
    except ApplyError:
        raise ApplyError("The recorded apply is invalid; revert manually.") from None
    if sorted(meta["files"]) != sorted(expected):
        raise ApplyError("The recorded apply metadata does not match the patch; "
                         "revert manually.")
    return {"base": meta["base"], "files": list(meta["files"]), "patch": patch}


def clear_last_apply(repository, patch=None):
    """Drop the record, but only when `patch` matches (or when it is omitted)."""
    try:
        meta_path, patch_path = _record_paths(repository)
    except ApplyError:
        return
    if patch is not None:
        try:
            recorded = _read_regular(patch_path, MAX_PATCH, "patch").decode("utf-8")
        except (ApplyError, UnicodeError):
            return
        if recorded != patch:
            return
    for path in (meta_path, patch_path):
        try:
            path.unlink()
        except OSError:
            pass
