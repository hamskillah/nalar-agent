"""Edit the user configuration for MCP servers without touching other keys."""

import json
import os
import re
import tempfile
from pathlib import Path

from .atomic import write as _atomic_write
from .config import ConfigError, _servers
from .core import MODEL_ID

MAX_CONFIG = 64 * 1024


class ManageError(ValueError):
    pass


def default_path():
    return Path(os.environ.get("NALAR_GLOBAL_CONFIG")
                or Path.home() / ".config" / "nalar" / "config.json")


def _read(path):
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG:
        raise ManageError("User configuration is not a regular, reasonably sized file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ManageError("User configuration is not valid JSON; edit it manually.") from None
    if not isinstance(value, dict):
        raise ManageError("User configuration must be a JSON object.")
    return value


def _write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2) + "\n").encode()
    try:
        _atomic_write(path, payload, mode=0o600, prefix=".tmp-")
    except OSError:
        raise ManageError("User configuration could not be written.") from None


from .config import KEYS as _CONFIG_KEYS

KNOWN_KEYS = set(_CONFIG_KEYS)
MUTABLE = {"model", "review_model", "subagent_model", "permission",
           "max_turns", "max_calls", "subagent_max_turns",
           "subagent_max_calls", "subagent_token_budget",
           "context_budget", "token_budget",
           "provider.base_url", "provider.api_key_env"}
INT_RANGES = {"max_turns": (1, 50), "max_calls": (0, 200),
              "subagent_max_turns": (1, 20), "subagent_max_calls": (1, 40),
              "subagent_token_budget": (1_000, 200_000),
              "context_budget": (8_000, 1_000_000),
              "token_budget": (0, 2_000_000)}
PERMISSIONS = {"ask", "auto", "plan"}
MODEL = MODEL_ID
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _typed(key, raw):
    if key in {"model", "review_model", "subagent_model"}:
        if not MODEL.fullmatch(raw):
            raise ManageError("Model IDs use letters, digits, . _ : - only.")
        return raw
    if key == "permission":
        if raw not in PERMISSIONS:
            raise ManageError("permission must be ask, auto or plan.")
        return raw
    if key in INT_RANGES:
        if not raw.isdigit():
            raise ManageError(f"{key} must be a nonnegative integer.")
        value = int(raw)
        low, high = INT_RANGES[key]
        if not low <= value <= high:
            raise ManageError(f"{key} must be between {low} and {high}.")
        return value
    if key == "provider.api_key_env":
        if not VARIABLE.fullmatch(raw):
            raise ManageError("api_key_env must name an environment variable.")
        return raw
    if key == "provider.base_url":
        from .go import validate_base_url

        try:
            validate_base_url(raw)
        except ValueError as exc:
            raise ManageError(str(exc)) from None
        return raw
    raise ManageError(f"Unknown setting {key}.")


def _validate(value):
    from .config import ConfigError, load_config

    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "config.json"
        candidate.write_text(json.dumps(value), encoding="utf-8")
        try:
            load_config(Path(directory), candidate, trust_project=True)
        except ConfigError as exc:
            raise ManageError(str(exc)) from None


def set_value(key, raw, *, path=None):
    path = Path(path) if path else default_path()
    if key not in MUTABLE:
        raise ManageError(f"Unknown setting {key}. Choose one of: {', '.join(sorted(MUTABLE))}.")
    typed = _typed(key, raw)
    value = _read(path)
    if set(value) - KNOWN_KEYS:
        raise ManageError("User configuration has unknown keys; edit it manually.")
    if key.startswith("provider."):
        provider = value.setdefault("provider", {})
        if not isinstance(provider, dict):
            raise ManageError("provider in the user config is not an object.")
        provider[key.split(".", 1)[1]] = typed
    else:
        value[key] = typed
    _validate(value)
    _write(path, value)
    return value


def unset_value(key, *, path=None):
    path = Path(path) if path else default_path()
    if key not in MUTABLE:
        raise ManageError(f"Unknown setting {key}.")
    value = _read(path)
    if key.startswith("provider."):
        provider = value.get("provider")
        sub = key.split(".", 1)[1]
        if not isinstance(provider, dict) or sub not in provider:
            raise ManageError(f"{key} is not set.")
        del provider[sub]
        if not provider:
            value.pop("provider", None)
    else:
        if key not in value:
            raise ManageError(f"{key} is not set.")
        del value[key]
    _write(path, value)
    return value


def add_server(name, command=None, *, url=None, env=None, headers=None, path=None, force=False):
    path = Path(path) if path else default_path()
    if (command is None) == (url is None):
        raise ManageError("Provide exactly one of a command or a url.")
    if env and url is not None:
        raise ManageError("env applies only to command servers.")
    entry = {"command": list(command)} if command is not None else {"url": url}
    if env:
        entry["env"] = dict(env)
    if headers:
        entry["headers"] = dict(headers)
    try:
        validated = _servers({name: entry}, expand=False)[0]
    except ConfigError as exc:
        raise ManageError(str(exc)) from None
    value = _read(path)
    servers = value.get("mcp")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict) or set(value) - KNOWN_KEYS:
        raise ManageError("User configuration has unknown keys; edit it manually.")
    existing = servers.get(name)
    if existing is not None and not force:
        raise ManageError(f"MCP server {name} already exists; pass --force to replace it.")
    record = {"url": validated.url} if validated.url else {"command": list(validated.command)}
    if isinstance(existing, dict):
        if existing.get("enabled") is False:
            record["enabled"] = False
        if "timeout" in existing:
            record["timeout"] = existing["timeout"]
    if validated.env and not validated.url:
        record["env"] = dict(validated.env)
    if validated.headers:
        record["headers"] = dict(validated.headers)
    servers[name] = record
    value["mcp"] = servers
    _validate(value)
    _write(path, value)
    return value


def set_server_enabled(name, enabled, *, path=None):
    path = Path(path) if path else default_path()
    value = _read(path)
    servers = value.get("mcp")
    if not isinstance(servers, dict) or name not in servers:
        raise ManageError(f"No MCP server named {name} in the user config; "
                          "project-defined servers are edited in nalar.json.")
    entry = servers[name]
    if not isinstance(entry, dict):
        raise ManageError("The MCP server entry is malformed; edit it manually.")
    entry["enabled"] = bool(enabled)
    _validate(value)
    _write(path, value)
    return value


def remove_server(name, *, path=None):
    path = Path(path) if path else default_path()
    value = _read(path)
    servers = value.get("mcp")
    if not isinstance(servers, dict) or name not in servers:
        raise ManageError(f"No MCP server named {name}.")
    del servers[name]
    if servers:
        value["mcp"] = servers
    else:
        value.pop("mcp", None)
    _write(path, value)
    return value
