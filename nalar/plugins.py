"""Trusted, user-configured Python tool plugins.

Plugins are executable code, so they are loaded only from the user-level
configuration file. A cloned repository's nalar.json cannot enable one.
"""

import importlib
import importlib.util
import re
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
FILE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MAX_TOOLS = 32
MAX_FILES = 8
MAX_FILE = 256 * 1024


class PluginError(ValueError):
    """A plugin could not be loaded; the message never includes code."""


def _installed_entry_points():
    try:
        from importlib.metadata import entry_points

        return list(entry_points(group="nalar.tools"))
    except Exception:
        return []


def entry_point_names():
    """The installed `nalar.tools` entry points the loader will consult."""
    return [str(getattr(entry, "name", "?"))
            for entry in _installed_entry_points()[:8]]


def _collect(produced, label, warnings, seen, specs):
    try:
        tools = getattr(produced, "tools", None)
        if callable(tools):
            produced = tools()
    except (Exception, SystemExit):
        warnings.append(f"plugin {label}: unavailable")
        return
    if not isinstance(produced, (list, tuple)):
        warnings.append(f"plugin {label}: invalid tool list")
        return
    if len(produced) > MAX_TOOLS:
        warnings.append(f"plugin {label}: tool list truncated")
    for item in produced[:MAX_TOOLS]:
        try:
            name = item["name"]
            description = item["description"]
            parameters = item["parameters"]
            handler = item["handler"]
        except (KeyError, TypeError):
            warnings.append(f"plugin {label}: invalid tool")
            continue
        if (not isinstance(name, str) or not NAME.fullmatch(name)
                or not isinstance(description, str) or len(description) > 1024
                or not isinstance(parameters, dict) or parameters.get("type") != "object"
                or not callable(handler)):
            warnings.append(f"plugin {label}: invalid tool")
            continue
        if name in seen:
            warnings.append(f"plugin {label}: duplicate tool {name}")
            continue
        seen.add(name)
        specs.append(({"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters}}, handler))


def _file_modules(directories, warnings):
    found = []
    for directory in directories:
        directory = Path(directory)
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.suffix != ".py" and not path.is_symlink():
                continue
            if len(found) >= MAX_FILES:
                warnings.append(f"plugin directory {directory.name}: file limit reached")
                break
            if path.is_symlink() or not path.is_file():
                warnings.append(f"plugin file {path.name}: not a regular file")
                continue
            if not FILE_NAME.fullmatch(path.stem):
                warnings.append(f"plugin file {path.name}: invalid name")
                continue
            try:
                if path.stat().st_size > MAX_FILE:
                    warnings.append(f"plugin file {path.name}: too large")
                    continue
            except OSError:
                continue
            found.append(path)
        if len(found) >= MAX_FILES:
            break
    return found


def load_plugins(modules, directories=()):
    specs = []
    warnings = []
    seen = set()
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
            produced = module.tools()
        except (Exception, SystemExit):
            warnings.append(f"plugin {module_name}: unavailable")
            continue
        _collect(produced, module_name, warnings, seen, specs)
    for index, path in enumerate(_file_modules(directories, warnings)):
        label = f"file {path.name}"
        try:
            spec = importlib.util.spec_from_file_location(
                f"_nalar_plugin_{index}_{path.stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            produced = module.tools()
        except (Exception, SystemExit):
            warnings.append(f"plugin {label}: unavailable")
            continue
        _collect(produced, label, warnings, seen, specs)
    entries = _installed_entry_points()
    if len(entries) > 8:
        warnings.append(f"plugin entry points truncated to 8 of {len(entries)}")
    for entry in entries[:8]:
        label = f"entry point {getattr(entry, 'name', '?')}"
        try:
            produced = entry.load()
            if callable(produced):
                produced = produced()
        except (Exception, SystemExit):
            warnings.append(f"plugin {label}: unavailable")
            continue
        _collect(produced, label, warnings, seen, specs)
    return specs, warnings
