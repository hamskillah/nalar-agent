"""Create starter skills, prompt commands and user-level plugin files.

Every write is explicit and atomic. Existing files are only replaced with
``force``, symlinked components are refused, and names are validated with the
same rules the loaders use. Templates contain no secrets and no network calls.
"""

import json
import os
from pathlib import Path

from .atomic import write_text
from .commands import NAME as COMMAND_NAME
from .manage import default_path
from .plugins import FILE_NAME as PLUGIN_NAME
from .skills import NAME as SKILL_NAME

MAX_DESCRIPTION = 500


class ScaffoldError(ValueError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

1. Read the files involved before changing anything.
2. State the change, then verify it with the configured checks.
3. Report what you did not verify.
"""

COMMAND_TEMPLATE = """---
description: {description}
---

Follow this task for the repository:

$ARGUMENTS

Read the relevant files first, propose a patch, and say what you verified.
"""

PLUGIN_TEMPLATE = '''"""User-level Nalar tool plugin created by `nalar plugins init`."""


def tools():
    """Return tool descriptors. Handlers receive validated arguments."""

    def echo(arguments):
        return {"text": arguments["text"]}

    return [{
        "name": "{tool}",
        "description": {description_json},
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": echo,
    }]
'''


def _description(value, default, limit=MAX_DESCRIPTION):
    text = " ".join(str(value or "").split())
    return (text or default)[:limit]


def _refuse_symlinks(paths):
    for path in paths:
        if path.is_symlink():
            raise ScaffoldError(
                "Refusing to write through a symlink; remove it first.")


def _prepare(directory, target, force):
    _refuse_symlinks([target])
    if target.exists() and not force:
        raise ScaffoldError(
            f"{target.name} already exists; pass --force to overwrite it.",
            code=1)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ScaffoldError("The directory could not be created.") from None
    _refuse_symlinks([directory])
    if target.exists() and not target.is_file():
        raise ScaffoldError("The target is not a regular file.")


def create_skill(root, name, *, description=None, force=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ScaffoldError("The repository directory does not exist.")
    if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
        raise ScaffoldError("Skill names use lowercase letters, digits and "
                            "hyphens (max 64).")
    base = root / ".nalar"
    skills = base / "skills"
    directory = skills / name
    target = directory / "SKILL.md"
    _refuse_symlinks([base, skills, directory, target])
    text = SKILL_TEMPLATE.format(
        name=name,
        description=_description(description, f"Guidance for {name} tasks"))
    _prepare(directory, target, force)
    try:
        write_text(target, text, mode=0o644, prefix=".nalar-scaffold-")
    except OSError:
        raise ScaffoldError("The skill could not be written.") from None
    return {"created": str(target.relative_to(root)), "name": name}


def create_command(root, name, *, description=None, force=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ScaffoldError("The repository directory does not exist.")
    if not isinstance(name, str) or not COMMAND_NAME.fullmatch(name):
        raise ScaffoldError("Command names use lowercase letters, digits and "
                            "hyphens (max 64).")
    base = root / ".nalar"
    directory = base / "commands"
    target = directory / f"{name}.md"
    _refuse_symlinks([base, directory, target])
    text = COMMAND_TEMPLATE.format(
        name=name,
        description=_description(description, f"Run the {name} task", 200))
    _prepare(directory, target, force)
    try:
        write_text(target, text, mode=0o644, prefix=".nalar-scaffold-")
    except OSError:
        raise ScaffoldError("The command could not be written.") from None
    return {"created": str(target.relative_to(root)), "name": name}


def plugin_directory():
    return default_path().parent / "plugins"


def create_plugin(name, *, description=None, force=False):
    if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name):
        raise ScaffoldError("Plugin names use letters, digits and underscores, "
                            "starting with a letter or underscore (max 64).")
    directory = plugin_directory()
    target = directory / f"{name}.py"
    _refuse_symlinks([directory, target])
    description_json = json.dumps(
        _description(description, "Echo text back.", 200))
    text = PLUGIN_TEMPLATE.replace("{tool}", name).replace(
        "{description_json}", description_json)
    _prepare(directory, target, force)
    try:
        write_text(target, text, mode=0o600, prefix=".nalar-scaffold-")
    except OSError:
        raise ScaffoldError("The plugin could not be written.") from None
    return {"created": str(target), "module": name,
            "directory": str(directory),
            "note": "Files in this directory load automatically."}
