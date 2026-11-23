"""Detect likely verification commands from committed project markers.

Detection only reads small root-level files. It never executes a command,
never follows a symlink, and never trusts values from the repository beyond
length-checked argv strings. Everything it returns is validated by
``config._verify`` before it reaches ``nalar.json``.
"""

import json
import re
from pathlib import Path

MAX_MARKER = 64 * 1024
MAX_COMMANDS = 4
MAX_TEST_ENTRIES = 500
SKIP_DIRS = {"node_modules", "vendor", "build", "dist", "target", "__pycache__",
             "venv", ".venv"}

MAKE_TARGET = re.compile(r"(?m)^test\s*:(?!=)")
PLACEHOLDER = re.compile(r"no test specified", re.IGNORECASE)
PACKAGE_MANAGERS = (
    ("pnpm-lock.yaml", ("pnpm", "test")),
    ("yarn.lock", ("yarn", "test")),
    ("bun.lockb", ("bun", "test")),
    ("bun.lock", ("bun", "test")),
)


def _read(root, name):
    path = root / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_MARKER + 1)
    except OSError:
        return None
    if len(data) > MAX_MARKER:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def _has_python_tests(root):
    for name in ("pytest.ini", "conftest.py"):
        if _read(root, name) is not None:
            return True
    stack = [(root, 0)]
    seen = 0
    while stack:
        directory, depth = stack.pop()
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if seen > MAX_TEST_ENTRIES:
                return False
            name = child.name
            if name.startswith("."):
                continue
            try:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if depth < 3 and name not in SKIP_DIRS:
                        stack.append((child, depth + 1))
                    continue
            except OSError:
                continue
            if name.endswith(".py") and (name.startswith("test")
                                         or name.endswith("_test.py")):
                return True
    return False


def _python_command(root):
    pyproject = _read(root, "pyproject.toml")
    if pyproject is not None and "[tool.pytest" in pyproject:
        return ("python3", "-m", "pytest", "-q")
    if pyproject is not None and "pytest" in pyproject:
        return ("python3", "-m", "pytest", "-q")
    if _has_python_tests(root):
        return ("python3", "-m", "pytest", "-q")
    return None


def _package_command(root):
    raw = _read(root, "package.json")
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    test = scripts.get("test")
    if (not isinstance(test, str) or not test.strip()
            or len(test) > 500 or PLACEHOLDER.search(test)):
        return None
    for marker, command in PACKAGE_MANAGERS:
        if (root / marker).is_file() and not (root / marker).is_symlink():
            return command
    return ("npm", "test")


def _make_command(root):
    raw = _read(root, "Makefile")
    if raw is None:
        return None
    inside_define = False
    for line in raw.splitlines():
        stripped = line.strip()
        if inside_define:
            if stripped == "endef":
                inside_define = False
            continue
        if stripped == "define" or stripped.startswith("define "):
            inside_define = True
            continue
        if MAKE_TARGET.search(line):
            return ("make", "test")
    return None


def detect_verify(root):
    """Return (commands, notes): validated-shaped argv lists plus bounded notes."""
    root = Path(root)
    commands = []
    notes = []

    def add(command, source):
        if command is None:
            return
        command = tuple(command)
        if len(command) > 8 or any(len(item) > 500 for item in command):
            notes.append(f"{source}: command is too long")
            return
        if command in commands:
            return
        if len(commands) == MAX_COMMANDS:
            notes.append(f"{source}: limit of {MAX_COMMANDS} commands reached")
            return
        commands.append(command)

    add(_package_command(root), "package.json")
    python = _python_command(root)
    if python is not None:
        add(python, "Python")
    pyproject = _read(root, "pyproject.toml")
    if (pyproject is not None and "[tool.poetry]" in pyproject
            and python is None):
        notes.append("pyproject.toml: Poetry project without tests detected")

    if _read(root, "go.mod") is not None:
        add(("go", "test", "./..."), "go.mod")
    if _read(root, "Cargo.toml") is not None:
        add(("cargo", "test", "--quiet"), "Cargo.toml")
    add(_make_command(root), "Makefile")

    return [list(command) for command in commands], notes[:3]
