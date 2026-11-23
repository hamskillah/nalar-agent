"""Validated, layered configuration for the Nalar agent.

Project configuration may not load executable plugins: a cloned repository must
not be able to run code on the user's machine without explicit consent.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .core import MODEL_ID

from .go import DEFAULT_BASE

MAX_CONFIG = 64 * 1024
MAX_HEADERS = 8
MAX_SKILL_DIRS = 8
MAX_SERVERS = 16
MAX_COMMAND = 8
PERMISSIONS = {"ask", "auto", "plan"}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ENV = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
KEYS = {"$schema", "model", "review_model", "max_turns", "max_calls",
        "context_budget", "permission", "skills", "mcp", "plugins", "provider",
        "verify", "instructions", "tools", "token_budget", "subagents", "retries",
        "fallback_models", "commands", "subagent_model", "subagent_max_turns",
        "subagent_max_calls", "subagent_token_budget"}
MAX_VERIFY = 4
MAX_ARGV = 8
SERVER_KEYS = {"command", "env", "enabled", "url", "headers", "timeout"}
PROVIDER_KEYS = {"base_url", "api_key_env", "headers", "session_header",
                 "max_tokens", "temperature", "timeout"}
HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
GLOBAL_ONLY = {"plugins"}


class ConfigError(ValueError):
    """A configuration problem described without echoing secrets or contents."""


@dataclass(frozen=True)
class MCPServer:
    name: str
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 15.0


@dataclass(frozen=True)
class Provider:
    base_url: str = DEFAULT_BASE
    api_key_env: str = "NALAR_API_KEY"
    headers: tuple = ()
    session_header: bool | str = True
    max_tokens: int = 1536
    temperature: float = 0.0
    timeout: float = 60.0


@dataclass(frozen=True)
class Config:
    model: str | None = None
    max_turns: int = 12
    max_calls: int = 24
    context_budget: int = 120_000
    permission: str = "ask"
    skills: tuple[Path, ...] = ()
    mcp: tuple[MCPServer, ...] = ()
    plugins: tuple[str, ...] = ()
    provider: Provider = Provider()
    verify: tuple = ()
    instructions: tuple = ("NALAR.md", "AGENTS.md")
    tools_allow: tuple | None = None
    tools_deny: tuple = ()
    tools_ask: tuple = ()
    token_budget: int = 0
    subagents: bool = True
    retries: int = 2
    fallback_models: tuple = ()
    commands: tuple = ()
    root: Path | None = None
    plugin_dirs: tuple = ()
    review_model: str | None = None
    subagent_model: str | None = None
    subagent_max_turns: int = 6
    subagent_max_calls: int = 8
    subagent_token_budget: int = 40_000

    def with_defaults(self, root):
        return Config(self.model, self.max_turns, self.max_calls, self.context_budget,
                      self.permission, self.skills, self.mcp, self.plugins, self.provider,
                      self.verify, self.instructions, self.tools_allow, self.tools_deny,
                      self.tools_ask, self.token_budget, self.subagents, self.retries,
                      self.fallback_models, self.commands, root, self.plugin_dirs,
                      self.review_model, self.subagent_model,
                      self.subagent_max_turns, self.subagent_max_calls,
                      self.subagent_token_budget)


def _read(path):
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"Configuration is not a regular file: {path}")
    try:
        if path.stat().st_size > MAX_CONFIG:
            raise ConfigError("Configuration is too large.")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise ConfigError("Configuration cannot be read.") from None
    except ValueError:
        raise ConfigError("Configuration is not valid JSON.") from None
    if not isinstance(value, dict):
        raise ConfigError("Configuration must be a JSON object.")
    unknown = set(value) - KEYS
    if unknown:
        raise ConfigError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    return value


def _expand(value):
    def replace(match):
        replacement = os.environ.get(match.group(1))
        if replacement is None:
            raise ConfigError(f"Environment variable {match.group(1)} is not set.")
        return replacement

    return ENV.sub(replace, value)


def _command_dirs(value, base):
    if not isinstance(value, list) or len(value) > MAX_SKILL_DIRS:
        raise ConfigError("commands must be a list of at most eight relative paths.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise ConfigError("Each commands entry must be a relative path.")
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError("Command directories must stay inside the configuration directory.")
        result.append((base / candidate).resolve())
    return result


def _skills(value, base):
    if not isinstance(value, list) or len(value) > MAX_SKILL_DIRS:
        raise ConfigError("skills must be a list of at most eight relative paths.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise ConfigError("Each skills entry must be a relative path.")
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError("Skill directories must stay inside the configuration directory.")
        result.append((base / candidate).resolve())
    return result


def _servers(value, *, expand=True):
    if not isinstance(value, dict) or len(value) > MAX_SERVERS:
        raise ConfigError("mcp must be an object of at most sixteen servers.")
    result = []
    for name, server in value.items():
        if not NAME.fullmatch(name) or not isinstance(server, dict):
            raise ConfigError("Each MCP server needs a short lowercase name.")
        if set(server) - SERVER_KEYS:
            raise ConfigError(f"Unknown keys in MCP server {name}.")
        command = server.get("command")
        url = server.get("url")
        if (url is None) == (command is None):
            raise ConfigError(f"MCP server {name} needs exactly one of command or url.")
        if command is not None and (
                not isinstance(command, list) or not 1 <= len(command) <= MAX_COMMAND
                or any(not isinstance(part, str) or not part or len(part) > 500 for part in command)):
            raise ConfigError(f"MCP server {name} needs a nonempty command list.")
        if url is not None:
            from .mcp import MCPError, validate_url

            try:
                validate_url(url)
            except MCPError:
                raise ConfigError(f"MCP server {name} url must be https, or http only for loopback.") from None
        header_values = server.get("headers", {})
        if not isinstance(header_values, dict) or len(header_values) > MAX_HEADERS:
            raise ConfigError(f"MCP server {name} headers must be an object of at most eight entries.")
        resolved_headers = {}
        for header, item in header_values.items():
            if (not isinstance(header, str) or not HEADER.fullmatch(header)
                    or header.lower() in {"host", "content-length", "transfer-encoding"}):
                raise ConfigError(f"MCP server {name} has an invalid header name.")
            if not isinstance(item, str):
                raise ConfigError(f"MCP server {name} header values must be strings.")
            value = (_expand(item) if expand else item)
            if CONTROL.search(value):
                raise ConfigError(f"MCP server {name} header values may not contain control characters.")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise ConfigError(f"MCP server {name} header values must be Latin-1.") from None
            resolved_headers[header] = value[:512]
        env = server.get("env", {})
        if not isinstance(env, dict) or len(env) > 32:
            raise ConfigError(f"MCP server {name} has an invalid env object.")
        resolved = {}
        for key, item in env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) or not isinstance(item, str):
                raise ConfigError(f"MCP server {name} has an invalid environment entry.")
            value = (_expand(item) if expand else item)
            if CONTROL.search(value):
                raise ConfigError(f"MCP server {name} environment values may not contain control characters.")
            resolved[key] = value[:4096]
        enabled = server.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"MCP server {name} enabled must be true or false.")
        timeout = server.get("timeout", 15.0)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 1 <= timeout <= 600):
            raise ConfigError(f"MCP server {name} timeout must be between 1 and 600 seconds.")
        result.append(MCPServer(name, tuple(command or ()), resolved, enabled,
                                url, resolved_headers, float(timeout)))
    return result


def _tools(value):
    if not isinstance(value, dict) or set(value) - {"allow", "deny", "ask"}:
        raise ConfigError("tools must be an object with allow and deny patterns.")
    def patterns(item, optional=False, empty_ok=False):
        if item is None and optional:
            return None
        if (not isinstance(item, list) or len(item) > 32
                or (not item and not empty_ok)):
            raise ConfigError("Tool patterns must be a list of at most thirty-two strings.")
        for pattern in item:
            if not isinstance(pattern, str) or not 0 < len(pattern) <= 64 or "\0" in pattern:
                raise ConfigError("Each tool pattern must be a short string.")
        return tuple(item)
    return (patterns(value.get("allow"), optional=True),
            patterns(value.get("deny", []), empty_ok=True),
            patterns(value.get("ask", []), empty_ok=True))


def _instructions(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise ConfigError("instructions must be a list of one to four relative paths.")
    paths = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise ConfigError("Each instructions entry must be a relative path.")
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError("Instruction files must stay inside the repository.")
        paths.append(item)
    return tuple(paths)


def _verify(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_VERIFY:
        raise ConfigError("verify must be a list of one to four argv lists.")
    commands = []
    for argv in value:
        if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV
                or any(not isinstance(item, str) or not item or len(item) > 500 or "\0" in item
                       for item in argv)):
            raise ConfigError("Each verify command is an argv list of up to eight strings.")
        commands.append(tuple(argv))
    return tuple(commands)


def _provider(value):
    if not isinstance(value, dict) or set(value) - PROVIDER_KEYS:
        raise ConfigError("provider must be an object with base_url, api_key_env, headers and session_header.")
    base_url = value.get("base_url", DEFAULT_BASE)
    if not isinstance(base_url, str) or not 12 <= len(base_url) <= 500:
        raise ConfigError("provider base_url must be an http(s) URL.")
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
    except ValueError:
        raise ConfigError("provider base_url must be an http(s) URL.") from None
    try:
        parsed.port
    except ValueError:
        raise ConfigError("provider base_url has an invalid port.") from None
    if (parsed.scheme not in {"https", "http"} or not hostname
            or parsed.fragment or parsed.username or parsed.password
            or CONTROL.search(base_url) or not base_url.isascii()):
        raise ConfigError("provider base_url must be an http(s) URL.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("provider base_url may use plain http only for loopback.")
    api_key_env = value.get("api_key_env", "NALAR_API_KEY")
    if not isinstance(api_key_env, str) or not VARIABLE.fullmatch(api_key_env):
        raise ConfigError("provider api_key_env must name an environment variable.")
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise ConfigError("provider headers must be an object of at most eight entries.")
    resolved = {}
    for name, item in headers.items():
        if not isinstance(name, str) or not HEADER.fullmatch(name) or name.lower() in {
                "host", "content-length", "transfer-encoding"}:
            raise ConfigError("provider header names must be simple and may not override transport headers.")
        if not isinstance(item, str):
            raise ConfigError("provider header values must be strings.")
        header_value = _expand(item)
        if CONTROL.search(header_value):
            raise ConfigError("provider header values may not contain control characters.")
        try:
            header_value.encode("latin-1")
        except UnicodeEncodeError:
            raise ConfigError("provider header values must be Latin-1.") from None
        resolved[name] = header_value[:512]
    session_header = value.get("session_header", True)
    if not isinstance(session_header, bool):
        if (not isinstance(session_header, str)
                or not HEADER.fullmatch(session_header)
                or session_header.lower() in {"host", "content-length", "transfer-encoding"}):
            raise ConfigError("provider session_header must be true, false or a simple header name.")
    max_tokens = value.get("max_tokens", 1536)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32_768:
        raise ConfigError("provider max_tokens must be an integer between 1 and 32768.")
    temperature = value.get("temperature", 0.0)
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2):
        raise ConfigError("provider temperature must be between 0 and 2.")
    timeout = value.get("timeout", 60.0)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= 600):
        raise ConfigError("provider timeout must be between 1 and 600 seconds.")
    return Provider(base_url, api_key_env, tuple(sorted(resolved.items())), session_header,
                    max_tokens, float(temperature), float(timeout))


def _plugins(value, allowed):
    if not allowed:
        raise ConfigError("plugins may only be configured in the user config file.")
    if not isinstance(value, list) or len(value) > 8:
        raise ConfigError("plugins must be a list of at most eight module names.")
    names = []
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", item):
            raise ConfigError("Each plugin must be a dotted Python module name.")
        names.append(item)
    return names


def load_config(root, global_file=None, *, trust_project=False):
    root = Path(root).resolve()
    global_file = Path(global_file) if global_file else Path(
        os.environ.get("NALAR_GLOBAL_CONFIG") or Path.home() / ".config" / "nalar" / "config.json")
    merged = {}
    project_skills, global_skills = [], []
    project_commands, global_commands = [], []
    servers = {}
    for path, base, is_global in ((global_file, global_file.parent, True),
                                  (root / "nalar.json", root, False)):
        value = _read(path)
        if not is_global:
            if "permission" in value and value["permission"] != "plan":
                raise ConfigError("permission may only be set in the user config, "
                                  "except for the read-only plan mode.")
            if "plugins" in value:
                raise ConfigError("plugins may only be configured in the user config.")
            if "provider" in value and not trust_project:
                raise ConfigError("provider may only be set in the user config, "
                                  "or with --trust-project.")
            if "mcp" in value and not trust_project:
                raise ConfigError("mcp servers may only be set in the user config, "
                                  "or with --trust-project.")
            for key in ("tools", "subagents", "token_budget", "fallback_models"):
                if key in value and not trust_project:
                    raise ConfigError(f"{key} may only be set in the user config, "
                                      "or with --trust-project.")
        if "model" in value and not (isinstance(value["model"], str)
                                     and MODEL_ID.fullmatch(value["model"])):
            raise ConfigError("model must be a provider model ID.")
        if "review_model" in value and not (isinstance(value["review_model"], str)
                                            and MODEL_ID.fullmatch(value["review_model"])):
            raise ConfigError("review_model must be a provider model ID.")
        if "subagent_model" in value and not (isinstance(value["subagent_model"], str)
                                              and MODEL_ID.fullmatch(value["subagent_model"])):
            raise ConfigError("subagent_model must be a provider model ID.")
        if "subagent_max_turns" in value and (type(value["subagent_max_turns"]) is not int
                                              or not 1 <= value["subagent_max_turns"] <= 20):
            raise ConfigError("subagent_max_turns must be between 1 and 20.")
        if "subagent_max_calls" in value and (type(value["subagent_max_calls"]) is not int
                                              or not 1 <= value["subagent_max_calls"] <= 40):
            raise ConfigError("subagent_max_calls must be between 1 and 40.")
        if "subagent_token_budget" in value and (
                type(value["subagent_token_budget"]) is not int
                or not 1_000 <= value["subagent_token_budget"] <= 200_000):
            raise ConfigError("subagent_token_budget must be between 1000 and 200000.")
        for key in ("max_turns", "max_calls", "context_budget"):
            if key in value and (type(value[key]) is not int or value[key] < 0):
                raise ConfigError(f"{key} must be a nonnegative integer.")
        if "max_turns" in value and not 1 <= value["max_turns"] <= 50:
            raise ConfigError("max_turns must be between 1 and 50.")
        if "max_calls" in value and value["max_calls"] > 200:
            raise ConfigError("max_calls must be at most 200.")
        if "token_budget" in value and (type(value["token_budget"]) is not int
                                        or not 0 <= value["token_budget"] <= 2_000_000):
            raise ConfigError("token_budget must be between 0 and 2000000 (0 = unlimited).")
        if "subagents" in value and not isinstance(value["subagents"], bool):
            raise ConfigError("subagents must be true or false.")
        if "retries" in value and (type(value["retries"]) is not int
                                   or not 0 <= value["retries"] <= 5):
            raise ConfigError("retries must be an integer between 0 and 5.")
        if "fallback_models" in value:
            models = value["fallback_models"]
            if not isinstance(models, list) or len(models) > 5:
                raise ConfigError("fallback_models must be a list of at most five model IDs.")
            for model in models:
                if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                    raise ConfigError("Each fallback model must be a valid model ID.")
            merged["fallback_models"] = tuple(dict.fromkeys(models))
        if "context_budget" in value and not 8_000 <= value["context_budget"] <= 1_000_000:
            raise ConfigError("context_budget must be between 8000 and 1000000.")
        if "permission" in value and value["permission"] not in PERMISSIONS:
            raise ConfigError("permission must be ask or auto.")
        if "skills" in value:
            target = global_skills if is_global else project_skills
            target.extend(_skills(value["skills"], base))
        if "commands" in value:
            target = global_commands if is_global else project_commands
            target.extend(_command_dirs(value["commands"], base))
        if "mcp" in value:
            for server in _servers(value["mcp"]):
                servers[server.name] = server
        if "plugins" in value:
            merged["plugins"] = _plugins(value["plugins"], is_global)
        if "provider" in value:
            merged["provider"] = _provider(value["provider"])
        if "verify" in value:
            merged["verify"] = _verify(value["verify"])
        if "instructions" in value:
            merged["instructions"] = _instructions(value["instructions"])
        if "tools" in value:
            merged["tools"] = _tools(value["tools"])
        for key in ("model", "review_model", "max_turns", "max_calls",
                    "context_budget", "permission", "token_budget", "subagents",
                    "retries", "subagent_model", "subagent_max_turns",
                    "subagent_max_calls", "subagent_token_budget"):
            if key in value:
                merged[key] = value[key]
    def defaults(*paths):
        return [path for path in paths if path.is_dir() and not path.is_symlink()]

    ordered = [*project_skills, *defaults(root / ".nalar" / "skills"),
               *global_skills, *defaults(global_file.parent / "skills")]
    unique = tuple(dict.fromkeys(ordered))
    command_dirs = tuple(dict.fromkeys([
        *project_commands, *defaults(root / ".nalar" / "commands"),
        *global_commands, *defaults(global_file.parent / "commands")]))
    return Config(model=merged.get("model"), max_turns=merged.get("max_turns", 12),
                  max_calls=merged.get("max_calls", 24),
                  context_budget=merged.get("context_budget", 120_000),
                  permission=merged.get("permission", "ask"), skills=unique,
                  mcp=tuple(servers[name] for name in sorted(servers)),
                  plugins=tuple(merged.get("plugins", ())),
                  provider=merged.get("provider", Provider()),
                  verify=merged.get("verify", ()),
                  instructions=merged.get("instructions", ("NALAR.md", "AGENTS.md")),
                  tools_allow=merged.get("tools", (None, (), ()))[0],
                  tools_deny=merged.get("tools", (None, (), ()))[1],
                  tools_ask=merged.get("tools", (None, (), ()))[2],
                  token_budget=merged.get("token_budget", 0),
                  subagents=merged.get("subagents", True),
                  retries=merged.get("retries", 2),
                  fallback_models=merged.get("fallback_models", ()),
                  commands=command_dirs, root=root,
                  plugin_dirs=(global_file.parent / "plugins",),
                  review_model=merged.get("review_model"),
                  subagent_model=merged.get("subagent_model"),
                  subagent_max_turns=merged.get("subagent_max_turns", 6),
                  subagent_max_calls=merged.get("subagent_max_calls", 8),
                  subagent_token_budget=merged.get("subagent_token_budget", 40_000))
