"""Environment diagnostics that never contact the model provider."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .skills import check as check_skills

MIN_PYTHON = (3, 12)


def _git(*args):
    return subprocess.run(["git", "--no-pager", *args], capture_output=True, timeout=10)


def _mcp(root, config, required=False):
    from .mcp import MCPError, make_client

    servers = []
    for server in config.mcp:
        if not server.enabled:
            servers.append({"name": server.name, "status": "disabled", "tools": []})
            continue
        client = None
        try:
            client = make_client(server)
            client.initialize()
            try:
                tools = [tool["function"]["name"] for tool in client.tools()]
            except MCPError as exc:
                if str(exc) != "no_tools":
                    raise
                tools = []
            servers.append({"name": server.name, "status": "ok", "tools": tools})
        except MCPError as exc:
            servers.append({"name": server.name, "status": str(exc), "tools": []})
        finally:
            if client is not None:
                client.close()
    ready = all(item["status"] in {"ok", "disabled"} for item in servers)
    return {"name": "mcp", "ok": ready, "required": required,
            "detail": f"{sum(item['status'] == 'ok' for item in servers)}/{len(servers)} servers ready",
            "servers": servers}


def run_checks(root, *, include_mcp=False, trust_project=False, connect=False):
    root = Path(root).resolve()
    checks = []

    def add(name, ok, detail, required=False):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500],
                       "required": required})

    from . import __version__

    add("nalar", True, __version__)
    add("python", sys.version_info[:2] >= MIN_PYTHON,
        f"{sys.version.split()[0]} (requires 3.12+)", True)
    try:
        git = _git("--version").stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        git = ""
    add("git", bool(git), git or "git is not available", True)
    try:
        from importlib.metadata import version

        add("tui", True, f"textual {version('textual')}")
    except Exception:
        add("tui", False, "textual is not installed; the TUI is unavailable")
    try:
        top = _git("-C", str(root), "rev-parse", "--show-toplevel").stdout.decode().strip()
        head = _git("-C", str(root), "rev-parse", "--short", "HEAD").stdout.decode().strip()
        add("repository", bool(top) and Path(top) == root, f"{top or 'not a repository'} @ {head or 'no commit'}",
            True)
    except (OSError, subprocess.SubprocessError):
        add("repository", False, "not a Git repository", True)

    config = None
    try:
        config = load_config(root, trust_project=trust_project)
        add("config", True, f"model={config.model or 'unset'}, permission={config.permission}", True)
    except ConfigError as exc:
        add("config", False, str(exc), True)

    if config is not None:
        env_name = config.provider.api_key_env
        add("api_key", bool(os.environ.get(env_name)),
            f"{env_name} is {'set' if os.environ.get(env_name) else 'missing'}", True)
        add("provider", True, config.provider.base_url)
        if connect:
            key = os.environ.get(env_name)
            if not key:
                add("provider_connect", False, f"{env_name} is missing", True)
            else:
                from .core import ProviderError
                from .go import list_models

                try:
                    models = list_models(config.provider.base_url, key,
                                         dict(config.provider.headers))
                    add("provider_connect", True, f"{len(models)} models listed", True)
                    if config.model:
                        listed = config.model in models
                        add("model_available", listed,
                            f"{config.model} is "
                            f"{'listed' if listed else 'not in the model list'}")
                    if config.review_model:
                        listed = config.review_model in models
                        add("review_model_available", listed,
                            f"{config.review_model} is "
                            f"{'listed' if listed else 'not in the model list'}")
                    if config.subagent_model:
                        listed = config.subagent_model in models
                        add("subagent_model_available", listed,
                            f"{config.subagent_model} is "
                            f"{'listed' if listed else 'not in the model list'}")
                except (ValueError, ProviderError) as exc:
                    add("provider_connect", False, str(exc), True)
        from .instructions import (load_instructions, load_nested_instructions,
                                   nested_names)

        repository = None
        try:
            from .coding import Repository

            repository = Repository(root)
            guidance = load_instructions(repository, config.instructions)
            nested = load_nested_instructions(
                repository, nested_names(config.instructions))
            detail = ", ".join(name for name, _ in guidance) or "none found"
            if nested:
                detail += (f" + {len(nested)} nested ("
                           + ", ".join(name for name, _ in nested) + ")")
            add("instructions", True, detail)
        except ValueError:
            add("instructions", False, "no committed snapshot to read instructions from")
        skills, skill_problems = check_skills(config.skills)
        add("skills", not skill_problems, f"{len(skills)} discovered" + (
            f": {', '.join(skill.name for skill in skills)}" if skills else "") + (
            f"; {len(skill_problems)} problem(s)" if skill_problems else ""))
        from .commands import check as check_commands

        prompts, prompt_problems = check_commands(config.commands)
        add("commands", not prompt_problems, f"{len(prompts)} discovered" + (
            f": {', '.join('/' + item.name for item in prompts)}" if prompts else "") + (
            f"; {len(prompt_problems)} problem(s)" if prompt_problems else ""))
        missing = []
        notes = []
        for argv in config.verify:
            command = argv[0]
            if os.path.isabs(command):
                found = os.access(command, os.X_OK)
            else:
                candidate = root / command
                worktree = (candidate.is_file()
                            and os.access(candidate, os.X_OK))
                found = worktree or shutil.which(command) is not None
                if "/" in command and found:
                    entry = repository.blobs.get(command) if repository else None
                    if entry is None:
                        notes.append(f"{command} is not committed")
                    elif entry[2] != "100755":
                        notes.append(f"{command} is not committed executable")
            if not found:
                missing.append(command)
        detail = (f"{len(config.verify)} configured; missing executable: "
                  f"{', '.join(missing[:3])}" if missing else
                  f"{len(config.verify)} configured" if config.verify else
                  "no verification commands configured")
        if notes:
            detail += "; " + "; ".join(notes[:3])
        if not config.verify:
            try:
                from .detect import detect_verify

                detected, _ = detect_verify(root)
            except Exception:
                detected = []
            if detected:
                detail += ("; detected: "
                           + "; ".join(" ".join(argv) for argv in detected[:2])
                           + " (run nalar init --detect)")
        add("verify", not missing, detail)
    try:
        state = Path.home() / ".local" / "share" / "nalar"
        if state.is_symlink():
            add("web_state", False, f"{state} is a symlink")
        elif state.exists():
            if not state.is_dir():
                add("web_state", False, f"{state} is not a directory")
            else:
                mode = state.stat().st_mode & 0o777
                ok = mode & 0o077 == 0
                detail = f"{state} ({oct(mode)})"
                workspaces = state / "workspaces"
                if workspaces.is_symlink():
                    ok = False
                    detail += "; workspaces is a symlink"
                elif workspaces.exists() and (
                        not workspaces.is_dir()
                        or workspaces.stat().st_mode & 0o077):
                    ok = False
                    detail += "; workspaces is not private"
                if not ok:
                    detail += "; run chmod 700"
                add("web_state", ok, detail)
        else:
            add("web_state", True, f"{state} (not created; doctor may create it)")
    except OSError:
        add("web_state", False, "the web state directory cannot be checked")
    try:
        from .store import SessionStore

        store = SessionStore()
        add("sessions", True,
            f"{store.directory} ({store.count(root)} for this repo)")
    except (ValueError, OSError):
        add("sessions", False, "session directory is not private or writable")
    if include_mcp and config is not None:
        checks.append(_mcp(root, config, required=include_mcp))
    return checks
