"""Create starter configuration and a review skill without clobbering work."""

import json

from pathlib import Path

from .atomic import write_text as _write_text
from .config import ConfigError, _verify
from .core import MODEL_ID
from .detect import detect_verify

MODEL = MODEL_ID
CONFIG = {"max_turns": 12, "max_calls": 24}
SKILL = """---
name: review
description: Check a proposed change before applying it
---

1. Read the diff and list each changed file.
2. Check error paths, boundaries and data loss before style.
3. Say what you did not verify; never claim tests you did not run.
"""


def _write_atomic(path, text):
    _write_text(path, text, prefix=".nalar-init-")


class InitError(ValueError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def initialize(root, *, model=None, force=False, detect=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise InitError("The repository directory does not exist.")
    if model is not None and not MODEL.fullmatch(model):
        raise InitError("The model ID is invalid.")
    config_path = root / "nalar.json"
    skill_path = root / ".nalar" / "skills" / "review" / "SKILL.md"
    for item in (config_path, root / ".nalar", root / ".nalar" / "skills",
                 skill_path.parent, skill_path):
        if item.is_symlink():
            raise InitError("Refusing to write through a symlink; remove it first.")
    if config_path.exists() and not force:
        raise InitError("nalar.json already exists; pass --force to overwrite it.", code=1)
    created = []
    detected = []
    notes = []
    if detect:
        detected, notes = detect_verify(root)
        if detected:
            try:
                detected = [list(command) for command in _verify(detected)]
            except ConfigError as exc:
                raise InitError(f"Detected verification commands are invalid: {exc}") from None
    config = dict(CONFIG)
    if model:
        config["model"] = model
    if detected:
        config["verify"] = detected
    try:
        _write_atomic(config_path, json.dumps(config, indent=2) + "\n")
    except OSError:
        raise InitError("nalar.json could not be written.") from None
    created.append("nalar.json")
    if force or not skill_path.exists():
        try:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(skill_path, SKILL)
        except OSError:
            raise InitError("The example skill could not be written.") from None
        created.append(str(skill_path.relative_to(root)))
    summary = {"created": created}
    if detect:
        summary["verify"] = detected
        if notes:
            summary["notes"] = notes
    return summary
