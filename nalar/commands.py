"""User-invoked prompt templates from committed or local directories.

Commands are guidance text only: they cannot change tools, permissions or
budgets. Invoking one sends its rendered body as the task.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .workspace import ToolError

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_BODY = 6_000
MAX_RENDERED = 6_000
MAX_COMMANDS = 64
MAX_DESCRIPTION = 200


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    body: str
    path: Path


def _parse(text, name, path):
    description = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            for line in text[4:end].split("\n"):
                key, separator, value = line.partition(":")
                if separator and key.strip() == "description":
                    description = value.strip()[:MAX_DESCRIPTION]
            body = text[end + 5:]
    if not body.strip() or len(body) > MAX_BODY:
        return None
    if not description:
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        description = first[:MAX_DESCRIPTION]
    return Command(name, description, body, path)


def check(directories):
    """Return (commands, problems); problems explain why a file was rejected."""
    commands = {}
    problems = []

    def problem(path, reason):
        problems.append({"path": str(path), "reason": reason})

    for directory in directories:
        directory = Path(directory)
        if directory.is_symlink():
            problem(directory, "command directory is a symlink")
            continue
        if not directory.is_dir():
            problem(directory, "command directory is missing")
            continue
        try:
            entries = sorted(directory.glob("*.md"))
        except OSError:
            problem(directory, "command directory cannot be listed")
            continue
        for path in entries:
            if len(commands) >= MAX_COMMANDS:
                problem(path, "command limit reached")
                break
            name = path.stem
            if name.startswith("."):
                continue
            if not NAME.fullmatch(name):
                problem(path, "invalid command name")
                continue
            if name in commands:
                problem(path, "duplicate command name")
                continue
            if path.is_symlink():
                problem(path, "command file is a symlink")
                continue
            if not path.is_file():
                problem(path, "command is not a regular file")
                continue
            try:
                if path.stat().st_size > MAX_BODY + 4_000:
                    problem(path, "command file is too large")
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                problem(path, "command file is not readable UTF-8")
                continue
            parsed = _parse(text, name, path)
            if parsed is None:
                problem(path, "body is empty or too large")
                continue
            commands[name] = parsed
    return [commands[name] for name in sorted(commands)], problems


def discover(directories):
    return check(directories)[0]


def discover_committed(repository, directories=(".nalar/commands",)):
    """Discover commands from the committed Git snapshot, never the worktree."""
    commands = {}
    prefixes = tuple(str(directory).strip("/") + "/" for directory in directories)
    for path in sorted(repository.command_paths):
        if len(commands) >= MAX_COMMANDS:
            break
        prefix = next((item for item in prefixes if path.startswith(item)), None)
        if prefix is None:
            continue
        relative = path[len(prefix):]
        if "/" in relative or not relative.endswith(".md"):
            continue
        name = relative[:-3]
        if not NAME.fullmatch(name) or name in commands:
            continue
        try:
            text = repository.read_committed_text(path)
        except ToolError:
            continue
        parsed = _parse(text, name, Path(path))
        if parsed is not None:
            commands[name] = parsed
    return [commands[name] for name in sorted(commands)]


def find(commands, name):
    for command in commands:
        if command.name == name:
            return command
    return None


def render(command, arguments=""):
    if "$ARGUMENTS" in command.body:
        rendered = command.body.replace("$ARGUMENTS", arguments).strip()
    elif arguments:
        rendered = f"{command.body.rstrip()}\n\nArguments: {arguments}".strip()
    else:
        rendered = command.body.strip()
    if len(rendered) > MAX_RENDERED:
        raise CommandError(
            f"/{command.name} with these arguments exceeds {MAX_RENDERED} characters.")
    return rendered
