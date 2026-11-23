"""Command-line entry point."""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .benchmark import benchmark
from .commands import discover as discover_commands
from .core import run
from .go import ChatCompletions
from .workspace import Workspace
from .usage import take_usage, usage_totals


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_])")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f"
                     r"\u00ad\u061c\u200b\u200e\u200f\u2028\u2029\ufeff\u202a-\u202e\u2060\u2066-\u2069\U000e0000-\U000e007f]")


def _safe_text(value):
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def _clean(value):
    return CONTROL.sub("", ANSI.sub("", _safe_text(value)))


def _line(value):
    return (_clean(value).replace("\r", "\\r")
            .replace("\n", "\\n").replace("\t", "\\t"))


def _session_message(exc):
    from .session import SESSION_ERRORS, session_error

    return session_error(exc) if str(exc) in SESSION_ERRORS else str(exc)


def _print_patch(text):
    try:
        sys.stdout.flush()
        if (hasattr(sys.stdout, "buffer")
                and not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty())):
            sys.stdout.buffer.write(text.encode("utf-8", "backslashreplace"))
            sys.stdout.buffer.flush()
        else:
            from .session import _terminal

            sys.stdout.write(_terminal(text))
            sys.stdout.flush()
        return True
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except (OSError, ValueError):
            pass
    except (AttributeError, OSError, ValueError, UnicodeError):
        pass
    return False


def _emit(value):
    try:
        print(json.dumps(value, indent=2, ensure_ascii=True))
    except RecursionError:
        print(json.dumps({"error": "response_too_complex"}))


def _verification_feedback(verification, diff=""):
    from .fix import verification_feedback

    return verification_feedback(verification, diff)


def _plan_text(items):
    marks = {"completed": "x", "in_progress": ">", "pending": " "}
    return [f"  [{marks.get(item.get('status'), '?')}] {_clean(item.get('step', ''))}"
            for item in items[:20]]


def _review_text(outcome):
    return [f"  ! [{_line(item['severity'])}] {_line(item['file'])}:"
            f"{_line(item['line'])} {_line(item['title'])}"
            for item in outcome.get("findings", [])]


def _review_fix_prompt(diff, findings):
    lines = ["A reviewer found defects in your current patch. Fix them, keep the rest "
             "of the change intact, and reply with the corrected patch.", ""]
    for item in findings[:10]:
        detail = str(item.get("detail", ""))[:400]
        lines.append(f"[{item['severity']}] {item['file']}:{item['line']} "
                     f"{item['title']} — {detail}")
    if diff:
        lines.append("\nCurrent patch:\n" + diff[:12_000])
    return "\n".join(lines)[:16_000]


def _client(provider, model, fallbacks=()):
    from .go import FallbackChat

    key = os.environ.get(provider.api_key_env)

    def build(name):
        return ChatCompletions(name, key, base_url=provider.base_url,
                          extra_headers=dict(provider.headers),
                          session=bool(provider.session_header), session_header=(provider.session_header if isinstance(provider.session_header, str) else 'x-nalar-session'),
                          max_tokens=provider.max_tokens,
                          temperature=provider.temperature,
                          timeout=provider.timeout)

    models = list(dict.fromkeys([model, *fallbacks]))
    if len(models) > 1:
        return FallbackChat(models, build)
    return build(model)


def _trust(args):
    return bool(getattr(args, "trust_project", False)
                or os.environ.get("NALAR_TRUST_PROJECT") == "1")


def _repo_config(args, parser):
    from .config import ConfigError, load_config

    try:
        return load_config(args.repo, trust_project=_trust(args))
    except ConfigError as exc:
        parser.error(_line(str(exc))[:1000])


def _add_limits(parser):
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--token-budget", type=int)
    parser.add_argument("--retries", type=int)


def _confirm(label):
    try:
        return input(label).strip() == "yes"
    except (EOFError, KeyboardInterrupt):
        return False


def _approver(args):
    """Approve tools.ask calls interactively; `--yes` approves all for scripts."""
    if getattr(args, "yes", False):
        return lambda name, arguments: True
    if not sys.stdin.isatty():
        return None

    def approve(name, arguments):
        payload = json.dumps(arguments, ensure_ascii=True)
        shown = _clean(payload[:300])
        if len(payload) > 300:
            shown += " … (truncated)"
        print(f"Approval required: {_clean(name)} {shown}", file=sys.stderr)
        print("Allow this tool once? Type yes: ", file=sys.stderr, end="", flush=True)
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip() == "yes"

    return approve


def _limit_overrides(args, parser):
    changes = {}
    checks = (("max_turns", 1, 50, "1 and 50"),
              ("max_calls", 0, 200, "0 and 200"),
              ("token_budget", 0, 2_000_000, "0 and 2000000"),
              ("retries", 0, 5, "0 and 5"))
    for key, low, high, label in checks:
        value = getattr(args, key, None)
        if value is None:
            continue
        if not low <= value <= high:
            parser.error(f"--{key.replace('_', '-')} must be between {label}.")
        changes[key] = value
    return changes


def _apply_limits(args, cfg, parser):
    changes = _limit_overrides(args, parser)
    return replace(cfg, **changes) if changes else cfg


def _allow_trust(parser):
    parser.add_argument("--trust-project", action="store_true",
                        help="allow project config to enable MCP servers and a provider endpoint")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="nalar",
        epilog="Examples: nalar tui  ·  nalar code \"Fix the failing test\"  ·  "
               "nalar review --scope worktree  ·  nalar apply --check patch.diff  ·  "
               "nalar doctor --connect")
    parser.add_argument("--version", action="version", version=f"nalar {__version__}")
    commands = parser.add_subparsers(dest="command")
    agent = commands.add_parser("run", help="perform a task with a scoped workspace")
    agent.add_argument("--model", required=True)
    agent.add_argument("--workspace", required=True)
    agent.add_argument("--mode", choices=("recover", "baseline"), default="recover")
    agent.add_argument("--events", type=Path, help="write agent events as newline-delimited JSON")
    _add_limits(agent)
    agent.add_argument("prompt")
    bench = commands.add_parser("bench", help="grade baseline and recovery on artifact tasks")
    bench.add_argument("--model", required=True)
    web = commands.add_parser("serve", help="start the single-user local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--state", type=Path, default=Path.home() / ".local" / "share" / "nalar")
    web.add_argument("--repo", type=Path,
                     help="serve a Git coding workspace instead of a flat workspace")
    web.add_argument("--verify-timeout", type=int, default=300)
    _allow_trust(web)
    code = commands.add_parser("code", help="propose a patch against a committed Git snapshot")
    code.add_argument("--model")
    code.add_argument("--repo", type=Path, default=Path.cwd())
    code.add_argument("--ref", default="HEAD")
    code.add_argument("--mode", choices=("recover", "baseline"), default="recover")
    code.add_argument("--no-extensions", action="store_true", help="skip skills, MCP and plugins")
    code.add_argument("--plan", action="store_true", help="read-only plan mode for this run")
    code.add_argument("--yes", action="store_true", help="approve tools listed in tools.ask")
    code.add_argument("--events", type=Path, help="write agent events as newline-delimited JSON")
    code.add_argument("--verify", action="store_true",
                      help="run configured checks on a temporary snapshot before returning")
    code.add_argument("--fix-attempts", type=int, default=0,
                      help="after a failed pre-flight, ask the model to fix it (0-3)")
    code.add_argument("--review", action="store_true",
                      help="ask the model to review the final patch before returning")
    code.add_argument("--review-model",
                      help="use a separate model for the patch review")
    code.add_argument("--review-fail-on", choices=("high", "medium", "low"))
    code.add_argument("--review-fix-attempts", type=int, default=0,
                      help="ask the model to repair review findings (0-2)")
    code.add_argument("--review-fix-on", choices=("high", "medium", "low"), default="high",
                      help="minimum severity that triggers a review repair")
    code.add_argument("--verify-timeout", type=int, default=300,
                      help="seconds per verification command (1-1800)")
    code.add_argument("--context", action="append", type=Path, default=[],
                      help="attach a UTF-8 context file (repeatable, max 4)")
    code.add_argument("--patch-out", type=Path,
                      help="save the proposed patch here for `nalar apply`")
    code.add_argument("--json", action="store_true",
                      help="write one JSON document (with the patch) to stdout")
    code.add_argument("--record", action="store_true",
                      help="save a private run summary in the session store")
    _add_limits(code)
    _allow_trust(code)
    code.add_argument("prompt")
    fix_cmd = commands.add_parser(
        "fix", help="verify the committed snapshot and propose a patch for failures")
    fix_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    fix_cmd.add_argument("--model")
    fix_cmd.add_argument("--ref", default="HEAD")
    fix_cmd.add_argument("--max-fix-attempts", type=int, default=2,
                         help="total model attempts after a failed snapshot (0-3)")
    fix_cmd.add_argument("--verify-timeout", type=int, default=300)
    fix_cmd.add_argument("--yes", action="store_true",
                         help="approve running the configured verification commands")
    fix_cmd.add_argument("--patch-out", type=Path)
    fix_cmd.add_argument("--events", type=Path)
    fix_cmd.add_argument("--context", type=Path, action="append", default=[],
                         metavar="FILE",
                         help="attach a UTF-8 context file (repeatable, max 4)")
    fix_cmd.add_argument("--instructions", default="",
                         help="extra guidance for the repair prompt (max 4000 characters)")
    fix_cmd.add_argument("--no-extensions", action="store_true")
    fix_cmd.add_argument("--format", choices=("json", "text"), default="json")
    fix_cmd.add_argument("--record", action="store_true",
                         help="save a private run summary in the session store")
    _add_limits(fix_cmd)
    _allow_trust(fix_cmd)
    session = commands.add_parser("chat", help="revise and review coding proposals interactively")
    session.add_argument("--model")
    session.add_argument("--repo", type=Path, default=Path.cwd())
    session.add_argument("--ref", default="HEAD")
    session.add_argument("--resume", help="resume a saved session id")
    session.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                         help="resume the latest session for this repository")
    session.add_argument("--no-sessions", action="store_true", help="keep this run in memory only")
    _add_limits(session)
    _allow_trust(session)
    code_bench = commands.add_parser("code-bench", help="evaluate patches on synthetic Git tasks")
    code_bench.add_argument("--model", required=True)
    tui = commands.add_parser("tui", help="open the full-screen coding workspace")
    tui.add_argument("--model")
    tui.add_argument("--repo", type=Path, default=Path.cwd())
    tui.add_argument("--ref", default="HEAD")
    tui.add_argument("--no-extensions", action="store_true")
    tui.add_argument("--plan", action="store_true", help="start in read-only plan mode")
    tui.add_argument("--no-sessions", action="store_true")
    tui.add_argument("--resume", help="resume a saved session id")
    _add_limits(tui)
    tui.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                     help="resume the latest session for this repository")
    _allow_trust(tui)
    plan_cmd = commands.add_parser("plan", help="produce a read-only implementation plan")
    plan_cmd.add_argument("--model")
    plan_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    plan_cmd.add_argument("--ref", default="HEAD")
    plan_cmd.add_argument("--out", type=Path, help="write the plan to a file (0600)")
    plan_cmd.add_argument("--json", action="store_true")
    plan_cmd.add_argument("--context", action="append", type=Path, default=[])
    plan_cmd.add_argument("--record", action="store_true",
                          help="save a private run summary in the session store")
    plan_cmd.add_argument("prompt")
    _add_limits(plan_cmd)
    _allow_trust(plan_cmd)
    ask = commands.add_parser("ask", help="answer a question about the repository without editing")
    ask.add_argument("--model")
    ask.add_argument("--repo", type=Path, default=Path.cwd())
    ask.add_argument("--ref", default="HEAD")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--context", action="append", type=Path, default=[])
    ask.add_argument("--record", action="store_true",
                     help="save a private run summary in the session store")
    ask.add_argument("prompt")
    _add_limits(ask)
    _allow_trust(ask)
    review_cmd = commands.add_parser("review", help="review a diff and print structured findings")
    review_cmd.add_argument("--model")
    review_cmd.add_argument("--review-model",
                            help="use a separate model for this review")
    review_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    review_cmd.add_argument("--ref", default="HEAD")
    review_cmd.add_argument("--scope", choices=("worktree", "staged", "branch"), default="worktree")
    review_cmd.add_argument("--include-untracked", action="store_true",
                            help="include safe text files Git does not track (worktree scope)")
    review_cmd.add_argument("--fail-on", choices=("high", "medium", "low"))
    review_cmd.add_argument("--format",
                            choices=("json", "text", "sarif", "markdown", "github"),
                            default="json")
    review_cmd.add_argument("--file", type=Path, help="review a saved patch file instead")
    review_cmd.add_argument("--context", action="append", type=Path, default=[],
                            metavar="FILE",
                            help="attach a UTF-8 context file (repeatable, max 4)")
    _allow_trust(review_cmd)
    apply_cmd = commands.add_parser("apply", help="apply a reviewed patch with safety checks")
    apply_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    apply_cmd.add_argument("--base", help="require HEAD to match this commit prefix")
    apply_cmd.add_argument("--check", action="store_true", help="validate without applying")
    apply_cmd.add_argument("--revert", action="store_true",
                           help="reverse an applied patch (confirmation or --yes)")
    apply_cmd.add_argument("--last", action="store_true",
                           help="revert the most recently applied patch (implies --revert)")
    apply_cmd.add_argument("--verify", action="store_true",
                           help="run configured checks after applying")
    apply_cmd.add_argument("--verify-timeout", type=int, default=300)
    apply_cmd.add_argument("--yes", action="store_true", help="approve verification commands")
    apply_cmd.add_argument("--format", choices=("json", "text"), default="json")
    apply_cmd.add_argument("--include", action="append", default=[], metavar="GLOB",
                           help="apply only files matching this pattern (a rename matches "
                                "either path; repeatable)")
    apply_cmd.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                           help="skip files matching this pattern (repeatable)")
    apply_cmd.add_argument("--allow-secrets", action="store_true",
                           help="apply a patch that looks like it contains credentials")
    apply_cmd.add_argument("patch", type=Path, nargs="?")
    _allow_trust(apply_cmd)
    init_cmd = commands.add_parser("init", help="create starter configuration and a review skill")
    init_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    init_cmd.add_argument("--model")
    init_cmd.add_argument("--force", action="store_true")
    init_cmd.add_argument("--detect", action="store_true",
                          help="detect verify commands from project markers")
    models = commands.add_parser("models", help="list models offered by the configured provider")
    models.add_argument("--repo", type=Path, default=Path.cwd())
    models.add_argument("--format", choices=("json", "text"), default="json")
    _allow_trust(models)
    verify_cmd = commands.add_parser("verify", help="run configured verification commands")
    verify_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    verify_cmd.add_argument("--yes", action="store_true", help="run without an interactive prompt")
    verify_cmd.add_argument("--timeout", type=int, default=300)
    verify_cmd.add_argument("--format", choices=("json", "text"), default="json")
    _allow_trust(verify_cmd)
    doctor = commands.add_parser("doctor", help="check environment, provider and extensions")
    doctor.add_argument("--repo", type=Path, default=Path.cwd())
    doctor.add_argument("--mcp", action="store_true", help="start configured MCP servers")
    doctor.add_argument("--connect", action="store_true",
                        help="verify the provider endpoint and key without a model call")
    doctor.add_argument("--format", choices=("json", "text"), default="json")
    _allow_trust(doctor)
    sessions = commands.add_parser("sessions", help="inspect saved session transcripts")
    sessions.add_argument("action", choices=("list", "show", "export", "delete", "stats",
                                            "prune", "fork", "search"))
    sessions.add_argument("--yes", action="store_true")
    sessions.add_argument("--query", help="filter by transcript text")
    sessions.add_argument("--keep", type=int, default=20, help="transcripts to keep when pruning")
    sessions.add_argument("--limit", type=int, default=20, help="search results to return")
    sessions.add_argument("--model", help="filter sessions by model")
    sessions.add_argument("session_id", nargs="?")
    sessions.add_argument("--repo", type=Path, default=Path.cwd())
    sessions.add_argument("--format", choices=("json", "text", "markdown"),
                          default=None)
    config = commands.add_parser("config", help="inspect the effective configuration")
    config.add_argument("action", choices=("show", "set", "unset"))
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    config.add_argument("--repo", type=Path, default=Path.cwd())
    config.add_argument("--format", choices=("json", "text"), default="json")
    _allow_trust(config)
    prompt_commands = commands.add_parser("commands", help="inspect prompt commands")
    prompt_commands.add_argument("action", choices=("list", "show", "check", "init"))
    prompt_commands.add_argument("name", nargs="?")
    prompt_commands.add_argument("--repo", type=Path, default=Path.cwd())
    prompt_commands.add_argument("--format", choices=("json", "text"), default="json")
    prompt_commands.add_argument("--force", action="store_true")
    prompt_commands.add_argument("--description")
    _allow_trust(prompt_commands)
    plugins = commands.add_parser("plugins", help="inspect user-level tool plugins")
    plugins.add_argument("action", choices=("list", "check", "init"))
    plugins.add_argument("name", nargs="?")
    plugins.add_argument("--repo", type=Path, default=Path.cwd())
    plugins.add_argument("--format", choices=("json", "text"), default="json")
    plugins.add_argument("--force", action="store_true")
    plugins.add_argument("--description")
    _allow_trust(plugins)
    skills = commands.add_parser("skills", help="inspect project skills")
    skills.add_argument("action", choices=("list", "show", "check", "init"))
    skills.add_argument("name", nargs="?")
    skills.add_argument("--repo", type=Path, default=Path.cwd())
    skills.add_argument("--format", choices=("json", "text"), default="json")
    skills.add_argument("--force", action="store_true")
    skills.add_argument("--description")
    _allow_trust(skills)
    mcp = commands.add_parser("mcp", help="inspect configured MCP servers")
    mcp.add_argument("action", choices=("list", "prompts", "prompt", "resources",
                                       "add", "remove", "test", "enable", "disable"))
    mcp.add_argument("name", nargs="?")
    mcp.add_argument("server_command", nargs="*")
    mcp.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    mcp.add_argument("--force", action="store_true")
    mcp.add_argument("--url", help="HTTP MCP endpoint instead of a command")
    mcp.add_argument("--tool", help="tool to call with 'mcp test'")
    mcp.add_argument("--read", help="resource URI to read with 'mcp resources'")
    mcp.add_argument("--header", action="append", default=[], metavar="KEY=VALUE")
    mcp.add_argument("--format", choices=("json", "text"), default="json")
    mcp.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    _allow_trust(mcp)
    mcp.add_argument("--repo", type=Path, default=Path.cwd())
    mcp_serve = commands.add_parser(
        "mcp-serve", help="expose read-only repository tools over MCP (stdio)")
    mcp_serve.add_argument("--repo", type=Path, default=Path.cwd(),
                           help="repository whose committed snapshot is exposed")
    mcp_serve.add_argument("--print-config", action="store_true",
                           help="print an MCP client configuration snippet and exit")
    mcp_serve.add_argument("--http", action="store_true",
                           help="serve JSON-RPC over loopback HTTP instead of stdio")
    mcp_serve.add_argument("--port", type=int, default=8766)
    mcp_serve.add_argument("--token-env", metavar="NAME",
                           help="require Authorization: Bearer from this environment variable")
    scan_cmd = commands.add_parser(
        "scan", help="scan a diff for high-confidence credentials")
    scan_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    scan_cmd.add_argument("--scope", choices=("worktree", "staged", "branch",
                                               "tree"),
                          default="worktree")
    scan_cmd.add_argument("--ref", default=None,
                          help="base ref for --scope branch (required); "
                               "defaults to HEAD for tree")
    scan_cmd.add_argument("--include-untracked", action="store_true",
                          help="include untracked files in a worktree scan")
    scan_cmd.add_argument("--file", type=Path,
                          help="scan a saved patch file instead of Git changes")
    scan_cmd.add_argument("--format",
                          choices=("json", "text", "sarif", "markdown", "github"),
                          default="json")
    completions_cmd = commands.add_parser("completions",
                                          help="print a completion script for your shell")
    completions_cmd.add_argument("shell", choices=("bash", "zsh", "fish"))
    args = parser.parse_args()
    if args.command is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.print_help()
            return 0
        args = parser.parse_args(["tui"])
    if args.command == "mcp-serve":
        from .coding import Repository
        from .mcpserver import serve, serve_http

        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535.")
        if args.token_env and not args.http:
            parser.error("--token-env requires --http.")
        if args.token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",
                                               args.token_env):
            parser.error("--token-env must name an environment variable.")
        if args.print_config:
            root = Path(args.repo).resolve()
            try:
                Repository(root)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            if args.http:
                config = {"type": "http",
                          "url": f"http://127.0.0.1:{args.port}/mcp"}
                if args.token_env:
                    config["headers"] = {
                        "Authorization": f"Bearer ${{{args.token_env}}}"}
                _emit({"mcpServers": {"nalar": config}})
            else:
                _emit({"mcpServers": {"nalar": {
                    "command": "nalar",
                    "args": ["mcp-serve", "--repo", str(root)],
                }}})
            return 0
        if args.http:
            try:
                return serve_http(args.repo, args.port,
                                  token_env=args.token_env)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            except OSError:
                parser.error("Cannot bind the MCP HTTP server.")
        try:
            return serve(args.repo)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        except KeyboardInterrupt:
            return 130
    if args.command == "completions":
        from . import completions

        sys.stdout.write(getattr(completions, args.shell)())
        return 0
    if args.command == "config" and args.action != "show":
        if args.format != "json":
            parser.error("--format text applies to config show only.")
        from .manage import ManageError, set_value, unset_value

        if not args.key:
            parser.error("Usage: nalar config set KEY VALUE | nalar config unset KEY")
        try:
            if args.action == "set":
                if args.value is None:
                    parser.error("Pass a value to set.")
                set_value(args.key, args.value)
            else:
                unset_value(args.key)
        except ManageError as exc:
            parser.error(_line(str(exc))[:1000])
        _emit({"action": args.action, "key": args.key})
        return 0
    if args.command == "config":
        cfg = _repo_config(args, parser)
        payload = {"model": cfg.model, "review_model": cfg.review_model,
                   "subagent_model": cfg.subagent_model,
                   "subagent_max_turns": cfg.subagent_max_turns,
                   "subagent_max_calls": cfg.subagent_max_calls,
                   "subagent_token_budget": cfg.subagent_token_budget,
                   "max_turns": cfg.max_turns, "max_calls": cfg.max_calls,
                   "context_budget": cfg.context_budget, "permission": cfg.permission,
                   "token_budget": cfg.token_budget, "subagents": cfg.subagents,
                   "retries": cfg.retries, "fallback_models": list(cfg.fallback_models),
                   "provider": {"base_url": cfg.provider.base_url,
                                "api_key_env": cfg.provider.api_key_env,
                                "headers": [name for name, _ in cfg.provider.headers],
                                "session_header": cfg.provider.session_header,
                                "max_tokens": cfg.provider.max_tokens,
                                "temperature": cfg.provider.temperature,
                                "timeout": cfg.provider.timeout},
                   "tools": {"allow": list(cfg.tools_allow) if cfg.tools_allow is not None else None,
                             "deny": list(cfg.tools_deny), "ask": list(cfg.tools_ask)},
                   "skills": [str(path) for path in cfg.skills],
                   "commands": [str(path) for path in cfg.commands],
                   "plugins": list(cfg.plugins),
                   "mcp": [{"name": server.name, "command": list(server.command),
                            "env": sorted(server.env), "enabled": server.enabled,
                            "url": server.url, "timeout": server.timeout}
                           for server in cfg.mcp]}
        if args.format == "text":
            for key in ("model", "review_model", "subagent_model", "permission",
                        "max_turns", "max_calls", "subagent_max_turns",
                        "subagent_max_calls", "subagent_token_budget",
                        "context_budget", "token_budget", "subagents", "retries"):
                value = payload[key]
                if isinstance(value, bool):
                    value = "true" if value else "false"
                print(f"{key}: {value if value is not None else 'unset'}")
            print("fallback_models: " + (", ".join(_clean(value) for value in payload["fallback_models"]) or "none"))
            provider = payload["provider"]
            print(f"provider.base_url: {_clean(provider['base_url'])}")
            print(f"provider.api_key_env: {_clean(provider['api_key_env'])}")
            print("provider.headers: " + (", ".join(_clean(value) for value in provider["headers"]) or "none"))
            session_header = provider["session_header"]
            if isinstance(session_header, bool):
                session_header = "true" if session_header else "false"
            print(f"provider.session_header: {session_header}")
            print(f"provider.max_tokens: {provider['max_tokens']}")
            print(f"provider.temperature: {provider['temperature']}")
            print(f"provider.timeout: {provider['timeout']}")
            tools = payload["tools"]
            print("tools.allow: " + (", ".join(_line(value) for value in tools["allow"]) if tools["allow"] is not None else "all"))
            print("tools.deny: " + (", ".join(_line(value) for value in tools["deny"]) or "none"))
            print("tools.ask: " + (", ".join(_line(value) for value in tools["ask"]) or "none"))
            print("skills: " + (", ".join(_line(value) for value in payload["skills"]) or "none"))
            print("commands: " + (", ".join(_line(value) for value in payload["commands"]) or "none"))
            print("plugins: " + (", ".join(_line(value) for value in payload["plugins"]) or "none"))
            print("mcp: " + (", ".join(
                f"{_clean(item['name'])}({'on' if item['enabled'] else 'off'})"
                for item in payload["mcp"]) or "none"))
        else:
            _emit(payload)
        return 0
    if args.command == "commands":
        from .commands import check, discover, find

        cfg = _repo_config(args, parser)
        if args.action == "init":
            from .scaffold import ScaffoldError, create_command

            if not args.name:
                parser.error("Pass a command name.")
            try:
                summary = create_command(args.repo, args.name,
                                         description=args.description,
                                         force=args.force)
            except ScaffoldError as exc:
                print("nalar: error: " + _clean(exc), file=sys.stderr)
                return exc.code
            if args.format == "text":
                print(f"/{summary['name']}: {_clean(summary['created'])}")
            else:
                _emit(summary)
            return 0
        found = discover(cfg.commands)
        if args.action == "check":
            found, problems = check(cfg.commands)
            if args.format == "text":
                for item in found:
                    print(f"{_line(item.name)}: {_line(item.description)}")
                for item in problems:
                    print(f"{_line(item['path'])}: {_line(item['reason'])}")
                print(f"{len(found)} command(s), {len(problems)} problem(s).")
            else:
                _emit({"commands": [
                    {"name": item.name, "description": item.description,
                     "path": str(item.path)} for item in found],
                    "problems": problems, "ok": not problems})
            return 0 if not problems else 1
        if args.action == "show":
            if not args.name:
                parser.error("Pass a command name.")
            item = find(found, args.name)
            if item is None:
                parser.error(f"Unknown command: {args.name}")
            if args.format == "text":
                print(f"{_clean(item.name)}: {_clean(item.description)}")
                print(_clean(item.body))
            else:
                _emit({"name": item.name, "description": item.description,
                       "path": str(item.path), "body": item.body})
            return 0
        if args.format == "text":
            for item in found:
                print(f"{_clean(item.name)}: {_clean(item.description)}")
        else:
            _emit([{"name": item.name, "description": item.description, "path": str(item.path)}
                   for item in found])
        return 0
    if args.command == "plugins":
        from .plugins import entry_point_names, load_plugins

        cfg = _repo_config(args, parser)
        if args.action == "init":
            from .scaffold import ScaffoldError, create_plugin

            if not args.name:
                parser.error("Pass a plugin name.")
            try:
                summary = create_plugin(args.name, description=args.description,
                                        force=args.force)
            except ScaffoldError as exc:
                print("nalar: error: " + _clean(exc), file=sys.stderr)
                return exc.code
            if args.format == "text":
                print(f"{summary['module']}: {_clean(summary['created'])}")
            else:
                _emit(summary)
            return 0
        specs, warnings = load_plugins(cfg.plugins, cfg.plugin_dirs)
        report = {
            "configured": list(cfg.plugins),
            "directories": [str(path) for path in cfg.plugin_dirs],
            "entry_points": entry_point_names(),
            "tools": [{"name": spec["function"]["name"],
                       "description": spec["function"]["description"]}
                      for spec, _ in specs],
            "warnings": list(warnings),
            "ok": not warnings,
        }
        if args.action == "check":
            if args.format == "text":
                for warning in warnings:
                    print("warning: " + _clean(warning))
                print(f"{len(specs)} tool(s), {len(warnings)} warning(s).")
            else:
                _emit(report)
            return 0 if not warnings else 1
        if args.format == "text":
            print("configured: " + (", ".join(_clean(name) for name in cfg.plugins)
                                    or "none"))
            print("directories: " + (", ".join(_clean(str(path))
                                               for path in cfg.plugin_dirs) or "none"))
            print("entry points: " + (", ".join(_clean(name)
                                                for name in report["entry_points"])
                                      or "none"))
            for tool in report["tools"]:
                print(f"{_clean(tool['name'])}: {_clean(tool['description'])}")
            for warning in warnings:
                print("warning: " + _clean(warning))
        else:
            _emit(report)
        return 0
    if args.command == "skills":
        from .skills import check, discover

        cfg = _repo_config(args, parser)
        if args.action == "init":
            from .scaffold import ScaffoldError, create_skill

            if not args.name:
                parser.error("Pass a skill name.")
            try:
                summary = create_skill(args.repo, args.name,
                                       description=args.description,
                                       force=args.force)
            except ScaffoldError as exc:
                print("nalar: error: " + _clean(exc), file=sys.stderr)
                return exc.code
            if args.format == "text":
                print(f"{summary['name']}: {_clean(summary['created'])}")
            else:
                _emit(summary)
            return 0
        found = discover(cfg.skills)
        if args.action == "check":
            found, problems = check(cfg.skills)
            if args.format == "text":
                for skill in found:
                    print(f"{_line(skill.name)}: {_line(skill.description)}")
                for item in problems:
                    print(f"{_line(item['path'])}: {_line(item['reason'])}")
                print(f"{len(found)} skill(s), {len(problems)} problem(s).")
            else:
                _emit({"skills": [
                    {"name": skill.name, "description": skill.description,
                     "path": str(skill.path)} for skill in found],
                    "problems": problems, "ok": not problems})
            return 0 if not problems else 1
        if args.action == "show":
            if not args.name:
                parser.error("Pass a skill name.")
            item = next((skill for skill in found if skill.name == args.name), None)
            if item is None:
                parser.error(f"Unknown skill: {args.name}")
            if args.format == "text":
                print(f"{_clean(item.name)}: {_clean(item.description)}")
                print(_clean(item.body))
            else:
                _emit({"name": item.name, "description": item.description,
                       "path": str(item.path), "body": item.body})
            return 0
        if args.format == "text":
            for skill in found:
                print(f"{_clean(skill.name)}: {_clean(skill.description)}")
        else:
            _emit([{"name": skill.name, "description": skill.description, "path": str(skill.path)}
                   for skill in found])
        return 0
    if args.command == "mcp":
        if args.format != "json" and args.action not in {"list", "prompts", "resources",
                                                         "prompt", "test"}:
            parser.error("--format text applies to read-only MCP actions only.")
        from .manage import ManageError, add_server, remove_server, set_server_enabled
        from .mcp import MCPError, make_client

        if args.action in {"enable", "disable"}:
            if not args.name:
                parser.error(f"Usage: nalar mcp {args.action} NAME")
            try:
                set_server_enabled(args.name, args.action == "enable")
            except ManageError as exc:
                parser.error(_line(str(exc))[:1000])
            _emit({("enabled" if args.action == "enable" else "disabled"): args.name})
            return 0
        if args.action in {"add", "remove"}:
            if not args.name:
                parser.error("Usage: nalar mcp add NAME -- COMMAND [ARGS...] | nalar mcp remove NAME")
            try:
                if args.action == "add":
                    headers = {}
                    for pair in args.header:
                        key, separator, value = pair.partition("=")
                        if not separator or not key:
                            parser.error("--header values use KEY=VALUE.")
                        headers[key] = value
                    environment = {}
                    for pair in args.env:
                        key, separator, value = pair.partition("=")
                        if not separator or not key:
                            parser.error("--env values use KEY=VALUE.")
                        environment[key] = value
                    if args.url:
                        if args.server_command or environment:
                            parser.error("Use either --url or a command with --env, not both.")
                        value = add_server(args.name, url=args.url, headers=headers,
                                           force=args.force)
                        payload = {"added": args.name, "url": args.url}
                    else:
                        command = list(args.server_command)
                        if command and command[0] == "--":
                            command = command[1:]
                        if not command:
                            parser.error("Pass the server command after --, or use --url.")
                        value = add_server(args.name, command, env=environment, headers=headers,
                                           force=args.force)
                        payload = {"added": args.name, "command": command}
                    entry = value.get("mcp", {}).get(args.name, {})
                    if isinstance(entry, dict) and entry.get("enabled") is False:
                        payload["enabled"] = False
                    _emit(payload)
                else:
                    remove_server(args.name)
                    _emit({"removed": args.name})
            except ManageError as exc:
                parser.error(_line(str(exc))[:1000])
            return 0
        cfg = _repo_config(args, parser)
        if args.action == "test":
            if not args.name:
                parser.error("Pass the configured server name.")
            server = next((item for item in cfg.mcp if item.name == args.name), None)
            if server is None:
                parser.error(f"No MCP server named {args.name}.")
            if not server.enabled:
                parser.error(f"MCP server {args.name} is disabled.")
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
                report = {"name": server.name, "tools": tools}
                try:
                    report["resources"] = [item["uri"] for item in client.resources()]
                except MCPError:
                    report["resources"] = []
                try:
                    report["prompts"] = [item["name"] for item in client.prompts()]
                except MCPError:
                    report["prompts"] = []
                try:
                    report["templates"] = [item["uriTemplate"]
                                           for item in client.resource_templates()]
                except MCPError:
                    report["templates"] = []
                if args.tool:
                    if not args.tool.startswith(server.name + "__"):
                        parser.error("The tool must belong to this server, e.g. server__tool.")
                    arguments = {}
                    for pair in args.arg:
                        key, separator, value = pair.partition("=")
                        if not separator or not key:
                            parser.error("--arg values use KEY=VALUE.")
                        arguments[key] = value
                    report["call"] = {"tool": args.tool,
                                      "result": client.call(args.tool, arguments)}
                if args.format == "text":
                    print(f"{_line(report['name'])}: ok")
                    for key in ("tools", "resources", "prompts", "templates"):
                        values = report.get(key) or []
                        if values:
                            print(f"  {key}: " + ", ".join(_line(value)
                                                          for value in values))
                    if "call" in report:
                        print(f"  call: {_line(report['call']['tool'])}")
                        print(_clean(str(report["call"]["result"])))
                else:
                    _emit(report)
                return 0
            except MCPError as exc:
                parser.error(_line(str(exc))[:1000])
            finally:
                if client is not None:
                    client.close()
        if args.action == "resources":
            if not args.name:
                parser.error("Pass the configured server name.")
            server = next((item for item in cfg.mcp if item.name == args.name), None)
            if server is None:
                parser.error(f"No MCP server named {args.name}.")
            if not server.enabled:
                parser.error(f"MCP server {args.name} is disabled.")
            client = None
            try:
                client = make_client(server)
                client.initialize()
                try:
                    resources = client.resources()
                except MCPError:
                    resources = []
                try:
                    templates = client.resource_templates()
                except MCPError:
                    templates = []
                report = {"name": server.name, "resources": resources, "templates": templates}
                if args.read:
                    report["read"] = {"uri": args.read,
                                      "text": client.read_resource(args.read)}
                if args.format == "text":
                    if args.read:
                        print(f"{_clean(args.read)}\n")
                        print(_clean(report["read"]["text"]))
                    else:
                        for item in report["resources"]:
                            print(f"{_line(item['uri'])}: "
                                  f"{_line(str(item.get('name') or ''))}")
                        for item in report["templates"]:
                            print(f"template: {_line(item['uriTemplate'])}")
                else:
                    _emit(report)
                return 0
            except MCPError as exc:
                parser.error(_line(str(exc))[:1000])
            finally:
                if client is not None:
                    client.close()
        if args.action in {"prompts", "prompt"}:
            clients = []
            try:
                for server in cfg.mcp:
                    if not server.enabled:
                        continue
                    client = make_client(server)
                    client.initialize()
                    clients.append((server.name, client))
                if args.action == "prompts":
                    listing = []
                    for name, client in clients:
                        try:
                            available = client.prompts()
                        except MCPError:
                            continue
                        for item in available:
                            listing.append({"server": name, **item,
                                            "command": f"{name}__{item['name']}"})
                    if args.format == "text":
                        for item in listing:
                            print(f"{_line(item['command'])}: {_line(item['description'])}")
                    else:
                        _emit(listing)
                    return 0
                if not args.name:
                    parser.error("Pass a prompt command such as fixture__review.")
                supplied = {}
                for pair in args.arg:
                    key, separator, value = pair.partition("=")
                    if not separator or not key:
                        parser.error("--arg values use KEY=VALUE.")
                    supplied[key] = value
                for name, client in clients:
                    if args.name.startswith(name + "__"):
                        text = client.get_prompt(args.name[len(name) + 2:], supplied)
                        if args.format == "text":
                            print(_clean(text))
                        else:
                            _emit({"command": args.name, "text": text})
                        return 0
                parser.error("Unknown MCP prompt; run 'nalar mcp prompts'.")
            except MCPError as exc:
                parser.error(_line(str(exc))[:1000])
            finally:
                for _, client in clients:
                    client.close()
        report = []
        for server in cfg.mcp:
            entry = {"name": server.name, "enabled": server.enabled, "tools": [], "status": "disabled"}
            if server.enabled:
                client = None
                try:
                    client = make_client(server)
                    client.initialize()
                    try:
                        entry["tools"] = [tool["function"]["name"] for tool in client.tools()]
                    except MCPError as exc:
                        if str(exc) != "no_tools":
                            raise
                        entry["tools"] = []
                    try:
                        entry["resources"] = [item["uri"] for item in client.resources()]
                    except MCPError:
                        entry["resources"] = []
                    try:
                        entry["prompts"] = [item["name"] for item in client.prompts()]
                    except MCPError:
                        entry["prompts"] = []
                    entry["status"] = "ok"
                except MCPError as exc:
                    entry["status"] = str(exc)
                finally:
                    if client is not None:
                        client.close()
            report.append(entry)
        if args.format == "text":
            for entry in report:
                print(f"{_line(entry['name'])}: {_line(entry['status'])}")
                for key in ("tools", "resources", "prompts"):
                    values = entry.get(key) or []
                    if values:
                        print(f"  {key}: " + ", ".join(_line(value) for value in values))
        else:
            _emit(report)
        return 0
    if args.command == "apply":
        from .apply import filter_patch, read_patch_source
        from .coding import Repository, apply_reviewed
        from .verify import run_commands

        if args.last:
            if args.patch is not None:
                parser.error("--last reverts the recorded apply; do not pass a patch file.")
            if args.include or args.exclude:
                parser.error("--last reverts the recorded patch; drop --include/--exclude.")
            if args.base:
                parser.error("--last uses the recorded base; drop --base.")
            args.revert = True
        elif args.patch is None:
            parser.error("Pass a patch file, or --last to revert the most recent apply.")
        if args.check and args.verify:
            parser.error("--verify requires applying; drop --check.")
        if not 1 <= args.verify_timeout <= 1800:
            parser.error("--verify-timeout must be between 1 and 1800 seconds.")
        if args.revert and args.verify:
            parser.error("--revert cannot run verification; drop --verify.")
        cfg = _repo_config(args, parser) if args.verify else None
        if args.verify and not cfg.verify:
            parser.error("Configure verify commands in nalar.json first.")
        if args.verify and not args.yes:
            if not sys.stdin.isatty():
                parser.error("Review the verification commands and pass --yes.")
            print("Nalar will run after applying:", file=sys.stderr)
            for argv in cfg.verify:
                print("  " + " ".join(_line(arg) for arg in argv), file=sys.stderr)
            if not _confirm("Type yes to run: "):
                return 2
        try:
            repository = Repository(args.repo, "HEAD")
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        if args.base:
            base = args.base.strip().lower()
            if not repository.commit.decode().startswith(base):
                parser.error("HEAD does not match --base.")
        record = None
        if args.last:
            from .apply import read_last_apply

            try:
                record = read_last_apply(repository)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            if record["base"] != repository.commit.decode():
                parser.error("HEAD moved since the recorded apply; revert that patch manually.")
        try:
            from .apply import patch_paths

            if record is not None:
                full_patch = record["patch"]
                all_files = patch_paths(full_patch)
                patch = full_patch
                files = all_files
            else:
                full_patch = read_patch_source(args.patch)
                all_files = patch_paths(full_patch)
                patch = filter_patch(full_patch, args.include, args.exclude)
                files = patch_paths(patch)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        from .secrets import scan_diff

        try:
            secret_report = scan_diff(patch)
        except ValueError:
            parser.error("The patch could not be scanned.")
        if secret_report.get("malformed"):
            parser.error("The patch contains malformed hunk headers.")
        if (not args.check and not args.revert and not args.allow_secrets
                and (secret_report["findings"] or secret_report["truncated"])):
            if secret_report["truncated"]:
                parser.error("The patch is too large to scan for credentials; "
                             "review it, then pass --allow-secrets to apply.")
            names = ", ".join(
                f"{_line(item['file'])}:{_line(item['line'])} {_line(item['kind'])}"
                for item in secret_report["findings"][:5])
            parser.error("The patch looks like it contains credentials ("
                         f"{names}). Review it, then pass --allow-secrets to apply.")
        if secret_report["truncated"]:
            print("Warning: patch is too large to scan fully for credentials.",
                  file=sys.stderr)
        if args.revert and not args.check:
            print("Reverse apply: credentials are not gated.", file=sys.stderr)
        if args.revert and not args.check and not args.yes:
            if not sys.stdin.isatty():
                parser.error("Review the reverse patch and pass --yes to revert.")
            print("Nalar will reverse:", file=sys.stderr)
            for name in files:
                print("  " + _line(name), file=sys.stderr)
            if not _confirm("Type yes to revert: "):
                return 2
        try:
            result = apply_reviewed(repository, patch, check_only=args.check,
                                    reverse=args.revert, allowed_files=all_files,
                                    allow_secrets=args.allow_secrets)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        verification = None
        if args.verify:
            verification = run_commands(cfg.verify, repository.root,
                                        timeout=args.verify_timeout,
                                        secret_names=(cfg.provider.api_key_env,))
        payload = {"base": repository.commit.decode(), "files": files,
                   "last": bool(args.last), "secrets": secret_report, **result}
        if verification is not None:
            payload["verification"] = verification
        if args.format == "text":
            action = ("Checked" if args.check
                      else "Reversed" if result.get("reversed") else "Applied")
            print(f"{action}: " + (", ".join(_line(name) for name in files)
                                    or "no files"))
            if secret_report["findings"]:
                print("Warning: possible credentials in added lines: "
                      + ", ".join(
                          f"{_line(item['file'])}:{_line(item['line'])} {_line(item['kind'])}"
                          for item in secret_report["findings"][:5]),
                      file=sys.stderr)
            if verification is not None:
                from .verify import status_text

                for item in verification["commands"]:
                    print(f"$ {' '.join(_line(arg) for arg in item['argv'])} "
                          f"({status_text(item)})")
                    if item["output"]:
                        print(item["output"])
                print("Verification passed." if verification["ok"]
                      else "Verification failed.")
        else:
            _emit(payload)
        applied = result["applied"] if not args.check else result["checked"]
        ok = applied and (verification is None or verification["ok"])
        return 0 if ok else 1
    if args.command == "init":
        from .init import InitError, initialize

        try:
            summary = initialize(args.repo, model=args.model, force=args.force,
                                 detect=args.detect)
        except InitError as exc:
            print("nalar: error: " + _clean(exc), file=sys.stderr)
            return exc.code
        _emit(summary)
        return 0
    if args.command == "models":
        from .core import ProviderError
        from .go import list_models

        cfg = _repo_config(args, parser)
        key = os.environ.get(cfg.provider.api_key_env)
        if not key:
            parser.error(f"Set {cfg.provider.api_key_env} in your environment.")
        if not re.fullmatch(r"[!-~]{1,4096}", key):
            parser.error(f"The value of {cfg.provider.api_key_env} is not a valid API key.")
        try:
            found = list_models(cfg.provider.base_url, key, dict(cfg.provider.headers))
        except ValueError:
            parser.error("The provider configuration is invalid.")
        except ProviderError as exc:
            parser.error(_line(str(exc))[:1000])
        if args.format == "text":
            for name in found:
                print(_line(name))
        else:
            _emit({"provider": cfg.provider.base_url, "models": found})
        return 0
    if args.command == "verify":
        from .verify import run_commands

        cfg = _repo_config(args, parser)
        if not cfg.verify:
            parser.error("No verify commands configured in nalar.json.")
        if not 1 <= args.timeout <= 1800:
            parser.error("--timeout must be between 1 and 1800 seconds.")
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error("Review the commands and pass --yes to run them.")
            print("Nalar will run:", file=sys.stderr)
            for argv in cfg.verify:
                print("  " + " ".join(_line(arg) for arg in argv), file=sys.stderr)

            if not _confirm("Type yes to run: "):
                return 2
        result = run_commands(cfg.verify, args.repo, timeout=args.timeout,
                              secret_names=(cfg.provider.api_key_env,))
        if args.format == "text":
            from .verify import status_text

            for item in result["commands"]:
                print(f"$ {' '.join(_line(arg) for arg in item['argv'])} "
                      f"({status_text(item)})")
                if item["output"]:
                    print(item["output"])
            print("Verification passed." if result["ok"] else "Verification failed.")
        else:
            _emit(result)
        return 0 if result["ok"] else 1
    if args.command == "doctor":
        from .doctor import run_checks

        checks = run_checks(args.repo, include_mcp=args.mcp, trust_project=_trust(args),
                            connect=args.connect)
        ok = all(item["ok"] for item in checks if item["required"])
        if args.format == "text":
            for item in checks:
                label = "OK  " if item["ok"] else ("FAIL" if item["required"] else "WARN")
                print(f"{label} {_line(item['name'])}: {_line(item['detail'])}")
            print(f"Overall: {'OK' if ok else 'FAIL'}")
        else:
            _emit({"checks": checks, "ok": ok})
        return 0 if ok else 1
    if args.command == "sessions":
        from .store import SessionError, SessionStore

        if args.format is None:
            args.format = "markdown" if args.action == "export" else "json"
        if args.format == "markdown" and args.action != "export":
            parser.error("--format markdown applies to sessions export only.")
        if (args.format == "text" and args.action not in
                {"list", "stats", "fork", "show", "export", "search"}):
            parser.error("--format text applies to sessions list, stats, fork, "
                         "show, export and search only.")
        if args.action in {"list", "search"} and not 1 <= args.limit <= 200:
            parser.error("--limit must be between 1 and 200.")
        if args.model is not None:
            label = args.model
            if (not label.strip() or len(label) > 200
                    or any(ord(char) < 32 or ord(char) == 127 for char in label)):
                parser.error("--model must be a non-empty printable label of at "
                             "most 200 characters.")
            if args.action not in {"list", "search"}:
                parser.error("--model applies to sessions list and search only.")
        if args.action == "search" and not args.query:
            parser.error("Pass --query with sessions search.")

        try:
            store = SessionStore()
            if args.action == "prune":
                if not 1 <= args.keep <= 1_000:
                    parser.error("--keep must be between 1 and 1000.")
                if not args.yes:
                    if not sys.stdin.isatty():
                        parser.error("Re-run with --yes to prune transcripts.")
                    if not _confirm(f"Keep the newest {args.keep} transcripts? Type yes: "):
                        return 2
                _emit(store.prune(args.keep, args.repo))
                return 0
            if args.action == "search":
                found = store.search(args.query, args.repo, args.limit,
                                     model=args.model)
                if args.format == "text":
                    for item in found:
                        print(f"{_line(item['id'])}  {_line(item['updated'])}  "
                              f"{_line(item['model'])}  {_line(item['task'][:60])}")
                        for match in item["matches"]:
                            print(f"  {_line(match['kind'])}: "
                                  f"{_line(match['snippet'])}")
                else:
                    _emit(found)
                return 0
            if args.action == "stats":
                stats = store.stats(args.repo)
                if args.format == "text":
                    count = stats["sessions"]
                    print(f"{count} session{'s' if count != 1 else ''} · "
                          f"{stats['tokens']} tokens")
                    print("models: " + (", ".join(_line(m) for m in stats["models"]) or "none"))
                    print("repos: " + (", ".join(_line(r) for r in stats["repos"]) or "none"))
                    if stats.get("by_model"):
                        print("tokens by model: " + ", ".join(
                            f"{_line(model)}: {count}"
                            for model, count in stats["by_model"].items()))
                else:
                    _emit(stats)
                return 0
            if args.action == "fork":
                if not args.session_id:
                    parser.error("Pass a session id to fork.")
                forked = store.fork(args.session_id)
                if args.format == "text":
                    print(f"{_clean(forked.id)} forked from {_clean(args.session_id)}")
                else:
                    _emit({"forked": forked.id, "from": args.session_id})
                return 0
            if args.action == "delete":
                if not args.session_id:
                    parser.error("Pass a session id to delete.")
                if not args.yes:
                    if not sys.stdin.isatty():
                        parser.error("Re-run with --yes to delete the transcript.")
                    if not _confirm("Delete this transcript? Type yes: "):
                        return 2
                store.delete(args.session_id)
                _emit({"deleted": args.session_id})
                return 0
            if args.action in {"show", "export"}:
                if not args.session_id:
                    parser.error("Pass a session id shown by 'nalar sessions list'.")
                session = store.load(args.session_id)
                if args.action == "export" and args.format == "markdown":
                    print(f"# Nalar session {_line(session.id)}", end="\n\n")
                    print(f"- repo: {_line(session.repo)}\n"
                          f"- model: {_line(session.model)}\n"
                          f"- base: {_line(session.base)}\n"
                          f"- updated: {_line(session.updated)}\n")
                    for entry in session.entries:
                        kind = str(entry.get("kind", ""))
                        status = entry.get("status")
                        if kind == "user":
                            label = "User"
                        elif kind == "assistant":
                            label = ("Nalar" + (f" ({_line(status)})"
                                                if isinstance(status, str) and status
                                                else ""))
                        else:
                            label = _line(kind)
                        print(f"## {label}\n\n{_clean(entry['text'])}\n")
                elif args.format == "text":
                    print(f"Session {_line(session.id)}")
                    print(f"repository: {_line(session.repo)}")
                    print(f"model: {_line(session.model)}")
                    print(f"base: {_line(session.base)}")
                    print(f"created: {_line(session.created)}")
                    print(f"updated: {_line(session.updated)}")
                    print(f"entries: {len(session.entries)}")
                    for entry in session.entries:
                        kind = str(entry.get("kind", ""))
                        status = entry.get("status")
                        if kind == "assistant":
                            label = "NALAR" + (f" ({_clean(status)})" if status else "")
                        else:
                            label = _clean(kind.upper())
                        print(f"\n{_clean(label)}")
                        print(_clean(entry.get("text", "")))
                else:
                    _emit({"id": session.id, "repo": session.repo, "model": session.model,
                           "base": session.base, "updated": session.updated,
                           "entries": session.entries})
            else:
                listed = store.list(args.repo, limit=args.limit,
                                    query=args.query, model=args.model)
                if args.format == "text":
                    for item in listed:
                        print(f"{_line(item['id'])}  {_line(item['updated'])}  "
                              f"{_line(item['model'])}  {_line(item['task'][:60])}")
                else:
                    _emit(listed)
        except SessionError as exc:
            parser.error(_session_message(exc))
        return 0
    if args.command == "tui":
        from .coding import Repository
        from .extensions import build_extensions
        from .go import list_models
        from .tui import NalarApp

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("The TUI requires an interactive terminal; use nalar code for scripts.")
        from .store import SessionError, SessionStore

        cfg = _apply_limits(args, _repo_config(args, parser), parser)
        key = os.environ.get("NALAR_API_KEY")
        model = args.model or cfg.model or "not configured"
        store = None
        saved = None
        if args.no_sessions and (args.resume or args.continue_session):
            parser.error("--resume/--continue need the session store; drop --no-sessions.")
        if args.resume and args.continue_session:
            parser.error("Use either --resume or --continue, not both.")
        key = os.environ.get(cfg.provider.api_key_env)
        try:
            if key:
                _client(cfg.provider, model, cfg.fallback_models)
            repo = Repository(args.repo, args.ref)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        try:
            if not args.no_sessions:
                store = SessionStore()
                if args.continue_session:
                    recent = store.list(repo.root, limit=50)
                    match = next((item for item in recent
                                  if item["base"] == repo.commit.decode()), None)
                    if match is not None:
                        saved = store.load(match["id"])
                elif args.resume:
                    saved = store.load(args.resume)
        except SessionError as exc:
            parser.error(_session_message(exc))
        if saved is not None and Path(saved.repo) != repo.root:
            parser.error("That session belongs to another repository.")
        if saved is not None and saved.base != repo.commit.decode():
            parser.error("That session started from another commit; start a new session.")
        from .agent import Budget

        permission = "plan" if args.plan else cfg.permission
        if args.plan:
            cfg = replace(cfg, permission="plan")
        if permission == "plan" and args.no_extensions:
            parser.error("Plan mode needs the extension belt; drop --no-extensions.")
        budget = Budget(cfg.token_budget)
        subagent_holder = {"value": model}
        extensions = None if args.no_extensions else build_extensions(
            repo, cfg,
            chat_factory=(lambda chosen=model: _client(cfg.provider, chosen,
                                                       cfg.fallback_models)),
            subagent_factory=(lambda: _client(
                cfg.provider, cfg.subagent_model or subagent_holder["value"],
                cfg.fallback_models)),
            budget=budget)
        try:
            NalarApp(repo, model, (lambda chosen: _client(cfg.provider, chosen, cfg.fallback_models)) if key else None,
                     extensions=extensions, permission=permission,
                     max_turns=cfg.max_turns, max_calls=cfg.max_calls,
                     context_budget=cfg.context_budget, store=store, session=saved,
                     verify_commands=cfg.verify,
                     secret_env=(cfg.provider.api_key_env,), budget=budget, retries=cfg.retries,
                     token_budget=cfg.token_budget,
                     commands=discover_commands(cfg.commands), fix_config=cfg,
                     review_model=cfg.review_model,
                     subagent_model=subagent_holder,
                     review_factory=((lambda: _client(cfg.provider, cfg.review_model,
                                                      cfg.fallback_models))
                                     if key and cfg.review_model and cfg.review_model != model
                                     else None),
                     models_fetcher=((lambda: list_models(cfg.provider.base_url, key,
                                                            dict(cfg.provider.headers))) if key else None)).run()
        finally:
            if extensions is not None:
                extensions.close()
        return 0
    if args.command == "serve":
        from .config import ConfigError, load_config

        root = args.repo or Path.cwd()
        try:
            cfg = load_config(root, trust_project=_trust(args))
        except ConfigError as exc:
            parser.error(_line(str(exc))[:1000])
        provider = cfg.provider
        key = os.environ.get(provider.api_key_env)
        factory = ((lambda model: _client(provider, model, cfg.fallback_models))
                   if key else None)
        try:
            if key:
                _client(provider, cfg.model or "beam-free", cfg.fallback_models)
            if args.repo is not None:
                from .webcode import make_server as make_coding_server

                limits = {"max_turns": cfg.max_turns, "max_calls": cfg.max_calls,
                          "context_budget": cfg.context_budget,
                          "token_budget": cfg.token_budget,
                          "retries": cfg.retries}
                with make_coding_server(args.state, args.repo, port=args.port,
                                        chat_factory=factory,
                                        review_factory=((lambda: _client(
                                            provider, cfg.review_model,
                                            cfg.fallback_models))
                                            if key and cfg.review_model else None),
                                        review_model=cfg.review_model,
                                        default_model=cfg.model or "beam-free",
                                        limits=limits,
                                        verify_commands=(cfg.verify if _trust(args)
                                                         else ()),
                                        secret_env=(cfg.provider.api_key_env,),
                                        verify_timeout=args.verify_timeout) as server:
                    print(f"Nalar Coding: http://127.0.0.1:{server.server_port}/",
                          flush=True)
                    server.serve_forever()
            else:
                from .web import make_server

                with make_server(args.state, port=args.port,
                                 chat_factory=factory) as server:
                    print(f"Nalar Agent: http://127.0.0.1:{server.server_port}/",
                          flush=True)
                    server.serve_forever()
        except KeyboardInterrupt:
            return 0
        except (OSError, ValueError, OverflowError) as exc:
            parser.error(_line(str(exc))[:1000])
    if args.command == "fix":
        from .agent import Budget
        from .apply import check_patch_target, write_patch
        from .coding import Repository
        from .context import ContextError, prompt_block as context_block
        from .context import read_context
        from .extensions import build_extensions
        from .fix import run_fix
        from .preflight import PreflightError, verify_snapshot

        if not 0 <= args.max_fix_attempts <= 3:
            parser.error("--max-fix-attempts must be between 0 and 3.")
        if not 1 <= args.verify_timeout <= 1800:
            parser.error("--verify-timeout must be between 1 and 1800 seconds.")
        cfg = _apply_limits(args, _repo_config(args, parser), parser)
        if not cfg.verify:
            parser.error("Configure verify commands in nalar.json first.")
        try:
            attached = context_block(read_context(args.context))
        except ContextError as exc:
            parser.error(_line(str(exc))[:1000])
        if len(args.instructions) > 4000:
            parser.error("--instructions must be at most 4000 characters.")
        if len(attached) > 32_000:
            parser.error("Attached context exceeds 32000 characters.")
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error("Review the verification commands and pass --yes.")
            print("Nalar will run on a temporary snapshot:", file=sys.stderr)
            for argv in cfg.verify:
                print("  " + " ".join(_line(arg) for arg in argv), file=sys.stderr)
            if not _confirm("Type yes to run: "):
                return 2
        if args.patch_out is not None:
            try:
                check_patch_target(args.patch_out)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
        try:
            repository = Repository(args.repo, args.ref)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        events = None
        handle = None
        if args.events:
            try:
                fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                             | os.O_NOFOLLOW, 0o600)
                handle = os.fdopen(fd, "w", encoding="utf-8")
            except OSError as exc:
                parser.error(f"Cannot write events: {exc.strerror or 'file error'}")

            def emit(event, handle=handle):
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")
                handle.flush()

            events = emit
        try:
            initial = verify_snapshot(
                repository, cfg.verify, timeout=args.verify_timeout,
                secret_names=(cfg.provider.api_key_env,))
        except PreflightError as exc:
            initial = {"ok": False, "error": str(exc), "commands": []}
        if events is not None:
            events({"type": "verify", "ok": bool(initial.get("ok")), "attempt": 0})
        extensions = None
        payload = None
        try:
            if initial.get("ok") or args.max_fix_attempts == 0:
                payload = {
                    "base": repository.commit.decode(),
                    "attempts": 0,
                    "verification": initial,
                    "patch": "",
                    "status": "ok" if initial.get("ok") else "failed",
                    "already_ok": bool(initial.get("ok")),
                }
            else:
                model = args.model or cfg.model
                if not model:
                    parser.error("Set a model in nalar.json or pass --model.")
                if not os.environ.get(cfg.provider.api_key_env):
                    parser.error(f"Set {cfg.provider.api_key_env} in your environment.")
                try:
                    chat = _client(cfg.provider, model, cfg.fallback_models)
                except ValueError as exc:
                    parser.error(_line(str(exc))[:1000])
                budget = Budget(cfg.token_budget)
                belt = None
                if not args.no_extensions:
                    extensions = build_extensions(
                        repository, cfg,
                        chat_factory=lambda: _client(cfg.provider, model,
                                                     cfg.fallback_models),
                        subagent_factory=lambda: _client(
                            cfg.provider, cfg.subagent_model or model,
                            cfg.fallback_models),
                        budget=budget)
                else:
                    from .coding import TOOLS
                    from .tools import ToolBelt

                    belt = ToolBelt()
                    for spec in TOOLS:
                        name = spec["function"]["name"]
                        belt.add(spec, lambda args, name=name: repository.call(name, args))
                    belt.apply_policy(cfg.tools_allow, cfg.tools_deny, cfg.tools_ask)
                payload = run_fix(
                    repository, chat, cfg, initial=initial,
                    attempts=args.max_fix_attempts, timeout=args.verify_timeout,
                    on_event=events, budget=budget,
                    approve=_approver(args),
                    belt=extensions.belt if extensions else belt,
                    system=extensions.system if extensions else None,
                    instructions=args.instructions, context=attached)
        finally:
            if extensions is not None:
                extensions.close()
            if handle is not None:
                handle.close()
        payload["model"] = args.model or cfg.model
        if patch := (payload.get("patch") or ""):
            from .secrets import scan_diff

            try:
                payload["secrets"] = scan_diff(patch)
            except ValueError:
                payload["secrets"] = {"findings": [], "truncated": True,
                                      "capped": False, "malformed": True}
            if payload["secrets"]["findings"]:
                print("Warning: possible credentials in added lines: "
                      + ", ".join(
                          f"{_line(item['file'])}:{_line(item['line'])} {_line(item['kind'])}"
                          for item in payload["secrets"]["findings"][:5]),
                      file=sys.stderr)
        if extensions is not None:
            payload["skills"] = [skill.name for skill in extensions.skills]
            payload["warnings"] = list(extensions.warnings)
        patch = payload.get("patch") or ""
        if args.patch_out is not None:
            if patch:
                try:
                    payload["patch_path"] = write_patch(args.patch_out, patch)
                except ValueError as exc:
                    parser.error(_line(str(exc))[:1000])
            else:
                payload["patch_path"] = None
        if args.record:
            try:
                from .apply import patch_paths
                from .store import SessionStore

                usage = take_usage(extensions.subagent_usage) if extensions else []
                delegated = sum(usage_totals(usage).values())
                session = SessionStore().record_run(
                    repo=repository.root, model=payload.get("model", ""),
                    base=payload["base"],
                    task="Verify and fix the committed snapshot",
                    status=payload.get("status", "failed"),
                    text=payload.get("error") or payload.get("status", ""),
                    tokens=max(0, payload.get("tokens", 0) - delegated),
                    turns=payload.get("turns", 0),
                    calls=payload.get("calls", 0),
                    verification=payload.get("verification"),
                    diff=patch, files=patch_paths(patch) if patch else (),
                    usage=usage)
                payload["session"] = session.id
            except (ValueError, OSError) as exc:
                payload["record_error"] = _clean(_session_message(exc))[:200]
                print("Warning: the run could not be recorded.", file=sys.stderr)
        if args.format == "text":
            if payload.get("already_ok"):
                print("Verified: the committed snapshot already passes.")
            elif payload.get("status") == "ok":
                print("Verified: the proposed patch passes; review and apply it.")
            else:
                print("Not verified: the proposal still fails.", file=sys.stderr)
            summary = f"base {payload['base']}  ·  attempts {payload['attempts']}"
            if payload.get("tokens"):
                summary += f"  ·  {payload['tokens']} tokens"
            if payload.get("turns"):
                summary += f"  ·  {payload['turns']} turns"
            if payload.get("calls"):
                summary += f"  ·  {payload['calls']} calls"
            if payload.get("session"):
                summary += f"  ·  session {payload['session']}"
            print(summary)
            if patch and not _print_patch(patch):
                return 1
            return 0 if payload.get("status") == "ok" else 1
        _emit(payload)
        return 0 if payload.get("status") == "ok" else 1
    if args.command == "scan":
        from .apply import read_patch_source
        from .review import ReviewError, collect_diff
        from .secrets import MAX_SCAN, sarif as sarif_doc
        from .secrets import scan_diff, scan_repository

        if args.file is not None:
            if (args.scope != "worktree" or args.ref is not None
                    or args.include_untracked):
                parser.error("--file cannot be combined with --scope, --ref or "
                             "--include-untracked.")
            from .apply import read_patch

            try:
                diff = read_patch_source(args.file)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            if len(diff.encode("utf-8")) > MAX_SCAN:
                parser.error("The patch is too large to scan.")
            try:
                report = scan_diff(diff)
            except ValueError:
                parser.error("The patch could not be scanned.")
            scope = "file"
        elif args.include_untracked and args.scope != "worktree":
            parser.error("--include-untracked applies only to the worktree scope.")
        elif args.scope == "branch" and args.ref is None:
            parser.error("--ref is required for --scope branch.")
        ref = args.ref or "HEAD"
        stats = {}
        if args.file is not None:
            pass
        elif args.scope == "tree":
            from .coding import Repository

            try:
                repository = Repository(args.repo, ref)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            report = scan_repository(repository)
            scope = "tree"
        else:
            try:
                scope, diff = collect_diff(
                    args.repo, args.scope, ref, args.include_untracked,
                    allow_empty=True, max_diff=10_000_000,
                    too_large="diff is too large to scan", stats=stats)
            except ReviewError as exc:
                parser.error(_line(str(exc))[:1000])
            try:
                report = scan_diff(diff)
            except ValueError:
                parser.error("The diff could not be scanned.")
            if stats.get("skipped"):
                report["skipped_untracked"] = stats["skipped"]
            if stats.get("unsafe"):
                report["unsafe_untracked"] = stats["unsafe"]
        incomplete = (report["truncated"] or bool(report.get("capped"))
                      or bool(report.get("malformed"))
                      or bool(report.get("skipped_untracked"))
                      or bool(report.get("skipped")))
        if args.format == "text":
            for item in report["findings"]:
                print(f"{_line(item['file'])}:{_line(item['line'])} {_line(item['kind'])}")
            if "files" in report:
                print(f"Scanned {report['files']} committed text file(s).")
            if report.get("binary"):
                print(f"Skipped {report['binary']} binary file(s).", file=sys.stderr)
            if report.get("skipped"):
                print(f"Skipped {report['skipped']} oversize file(s).",
                      file=sys.stderr)
            where = ("the saved patch" if scope == "file" else
                     "the committed tree" if scope == "tree" else f"{scope} changes")
            if report["findings"]:
                print(f"{len(report['findings'])} potential credential(s) in {where}.")
            else:
                print(f"No credentials found in {where}.")
            if report["truncated"]:
                print("The scan stopped at its size limit.", file=sys.stderr)
            if report.get("malformed"):
                print("The scan could not parse a hunk header.", file=sys.stderr)
            if report.get("skipped_untracked"):
                print(f"Skipped {report['skipped_untracked']} untracked file(s) "
                      "(count or size limit).", file=sys.stderr)
            if report.get("unsafe_untracked"):
                print(f"Skipped {report['unsafe_untracked']} untracked file(s) "
                      "by design (symlink, hardlink or binary).", file=sys.stderr)
            if report["capped"]:
                print("Additional findings were capped.", file=sys.stderr)
            if incomplete:
                print("The scan is incomplete; narrow the scope or scan files "
                      "directly.", file=sys.stderr)
        elif args.format == "sarif":
            _emit(sarif_doc(report, scope=scope, incomplete=incomplete))
        elif args.format == "markdown":
            from .report import scan_markdown

            print(scan_markdown(report, scope=scope), end="")
        elif args.format == "github":
            from .report import scan_github

            sys.stdout.write(scan_github(report, incomplete=incomplete))
        else:
            _emit({"scope": scope, "ok": not report["findings"] and not incomplete,
                   **report})
        if report["findings"]:
            return 1
        return 2 if incomplete else 0
    cfg = _repo_config(args, parser) if args.command in {"code", "chat", "review", "ask", "plan"} else None
    model = getattr(args, "model", None) or (cfg.model if cfg else None)
    if not model:
        parser.error("Pass --model or set model in nalar.json.")
    from .config import Provider

    provider = cfg.provider if cfg else Provider()
    if not os.environ.get(provider.api_key_env):
        parser.error(f"Set {provider.api_key_env} in your environment.")
    try:
        chat = _client(provider, model)
    except ValueError as exc:
        parser.error(_line(str(exc))[:1000])
    if args.command == "plan":

        from .agent import AgentLoop, Budget
        from .coding import Repository
        from .context import ContextError, prompt_block as context_block
        from .context import read_context
        from .extensions import build_extensions

        try:
            repository = Repository(args.repo, args.ref)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        try:
            attachment = context_block(read_context(args.context))
        except ContextError as exc:
            parser.error(_line(str(exc))[:1000])
        cfg = _apply_limits(args, cfg, parser)
        plan_cfg = replace(cfg, permission="plan")
        budget = Budget(cfg.token_budget)
        extensions = build_extensions(
            repository, plan_cfg,
            chat_factory=lambda: _client(cfg.provider, model,
                                         cfg.fallback_models),
            subagent_factory=lambda: _client(
                cfg.provider, cfg.subagent_model or model, cfg.fallback_models),
            budget=budget)
        for warning in extensions.warnings:
            print("warning: " + _clean(warning), file=sys.stderr)
        try:
            prompt = (args.prompt + "\n\nReturn a concrete implementation plan: the files to "
                      "change, ordered steps, risks, and how to verify it. Do not edit.")
            if attachment:
                prompt = f"{prompt}\n\n{attachment}"
            streamed = []

            def stream(piece):
                streamed.append(piece)
                sys.stdout.write(_clean(piece))
                sys.stdout.flush()

            result = AgentLoop(chat, extensions.belt, system=extensions.system,
                               max_turns=cfg.max_turns, max_calls=cfg.max_calls,
                               context_budget=cfg.context_budget,
                               token_budget=cfg.token_budget, budget=budget,
                               retries=cfg.retries).run(
                prompt, on_text=None if args.json or not sys.stdout.isatty() else stream)
        finally:
            extensions.close()
        written = None
        if args.out:
            target = args.out.expanduser()
            if target.is_symlink():
                parser.error("Refusing to write the plan through a symlink.")
            if not target.parent.is_dir():
                parser.error("The plan directory does not exist.")
            if result.status != "ok":
                print("warning: the plan run failed; no plan file was written.",
                      file=sys.stderr)
            else:
                from .atomic import write_text as _atomic_write_text

                try:
                    _atomic_write_text(target, _clean(result.text),
                                       mode=0o600, prefix=".nalar-plan-")
                except (OSError, UnicodeError):
                    parser.error("The plan could not be written.")
                written = str(args.out)
        session_id = None
        record_error = None
        if args.record:
            try:
                from .store import SessionStore

                usage = take_usage(extensions.subagent_usage) if extensions else []
                delegated = sum(usage_totals(usage).values())
                session = SessionStore().record_run(
                    repo=repository.root, model=model,
                    base=repository.commit.decode(), task=args.prompt,
                    status=result.status, text=result.text or "",
                    tokens=max(0, (result.tokens or 0) - delegated),
                    turns=result.turns, calls=result.calls, usage=usage)
                session_id = session.id
            except (ValueError, OSError) as exc:
                record_error = _clean(_session_message(exc))[:200]
                print("Warning: the run could not be recorded.", file=sys.stderr)
        if args.json:
            payload = {"status": result.status, "text": result.text,
                       "tokens": result.tokens, "turns": result.turns,
                       "calls": result.calls,
                       "warnings": list(extensions.warnings), "out": written}
            if record_error:
                payload["record_error"] = record_error
            if session_id:
                payload["session"] = session_id
            _emit(payload)
        elif not streamed:
            print(_clean(result.text))
        return 0 if result.status == "ok" else 1
    if args.command == "ask":
        from .agent import AgentLoop
        from .coding import Repository
        from .context import ContextError, prompt_block as context_block
        from .context import read_context
        from .extensions import build_extensions

        try:
            repository = Repository(args.repo, args.ref)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        try:
            attachment = context_block(read_context(args.context))
        except ContextError as exc:
            parser.error(_line(str(exc))[:1000])
        question = f"{args.prompt}\n\n{attachment}" if attachment else args.prompt
        from .agent import Budget

        cfg = _apply_limits(args, cfg, parser)
        plan = replace(cfg, permission="plan")
        budget = Budget(cfg.token_budget)
        extensions = build_extensions(
            repository, plan,
            chat_factory=lambda: _client(cfg.provider, model,
                                         cfg.fallback_models),
            subagent_factory=lambda: _client(
                cfg.provider, cfg.subagent_model or model, cfg.fallback_models),
            budget=budget)
        for warning in extensions.warnings:
            print("warning: " + _clean(warning), file=sys.stderr)
        try:
            streamed = []

            def stream(piece):
                streamed.append(piece)
                sys.stdout.write(_clean(piece))
                sys.stdout.flush()

            result = AgentLoop(chat, extensions.belt, system=extensions.system,
                               max_turns=cfg.max_turns, max_calls=cfg.max_calls,
                               context_budget=cfg.context_budget,
                               token_budget=cfg.token_budget, budget=budget,
                               retries=cfg.retries).run(
                question,
                on_text=None if args.json or not sys.stdout.isatty() else stream)
        finally:
            extensions.close()
        session_id = None
        record_error = None
        if args.record:
            try:
                from .store import SessionStore

                usage = take_usage(extensions.subagent_usage) if extensions else []
                delegated = sum(usage_totals(usage).values())
                session = SessionStore().record_run(
                    repo=repository.root, model=model,
                    base=repository.commit.decode(), task=args.prompt,
                    status=result.status, text=result.text or "",
                    tokens=max(0, (result.tokens or 0) - delegated),
                    turns=result.turns, calls=result.calls, usage=usage)
                session_id = session.id
            except (ValueError, OSError) as exc:
                record_error = _clean(_session_message(exc))[:200]
                print("Warning: the run could not be recorded.", file=sys.stderr)
        if args.json:
            payload = {"status": result.status, "text": result.text,
                       "tokens": result.tokens, "turns": result.turns,
                       "calls": result.calls, "trace": list(result.trace),
                       "skills": [skill.name for skill in extensions.skills],
                       "warnings": list(extensions.warnings)}
            if record_error:
                payload["record_error"] = record_error
            if session_id:
                payload["session"] = session_id
            _emit(payload)
        elif not streamed:
            print(_clean(result.text))
        return 0 if result.status == "ok" else 1
    if args.command == "review":
        from .agent import Budget
        from .apply import read_patch_source
        from .review import ReviewError, collect_diff, review

        if args.review_model is not None:
            from .core import MODEL_ID

            if not MODEL_ID.fullmatch(args.review_model):
                parser.error("--review-model must be a provider model ID.")
        if args.include_untracked and (args.file is not None or args.scope != "worktree"):
            parser.error("--include-untracked applies only to the worktree scope.")
        if args.scope == "branch" and args.ref == "HEAD":
            parser.error("--scope branch needs a base ref, e.g. --ref main.")
        if args.file is not None:
            if args.scope != "worktree" or args.ref != "HEAD":
                parser.error("--file cannot be combined with --scope or --ref.")
            try:
                diff = read_patch_source(args.file)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
            from .review import MAX_DIFF

            if len(diff.encode("utf-8")) > MAX_DIFF:
                parser.error("The saved patch is too large to review.")
            scope = "file"
        else:
            try:
                scope, diff = collect_diff(args.repo, args.scope, args.ref,
                                           include_untracked=args.include_untracked)
            except ReviewError as exc:
                parser.error(_line(str(exc))[:1000])
        from .core import ProviderError

        context = ""
        if args.context:
            from .context import ContextError, prompt_block, read_context

            try:
                context = prompt_block(read_context(args.context))
            except ContextError as exc:
                parser.error(_line(str(exc))[:1000])
            if len(context) > 32_000:
                parser.error("Attached context exceeds 32000 characters.")
        review_model = args.review_model or cfg.review_model
        used_model = review_model or model
        review_chat = chat
        if review_model and review_model != model:
            review_chat = _client(cfg.provider, review_model, cfg.fallback_models)
        try:
            result = review(review_chat, diff, model=used_model,
                            retries=cfg.retries, budget=Budget(cfg.token_budget),
                            context=context)
        except ProviderError as exc:
            parser.error(_line(str(exc))[:1000])
        result["scope"] = scope
        if args.format == "sarif":
            from .review import sarif

            _emit(sarif(result, model=used_model, scope=scope))
        elif args.format == "markdown":
            from .report import review_markdown

            print(review_markdown(result, model=used_model, scope=scope), end="")
        elif args.format == "github":
            from .report import review_github

            sys.stdout.write(review_github(result))
        elif args.format == "text":
            print(f"Review · {_line(scope)} · {_line(used_model)} · "
                  f"{_line(result['status'])}")
            if result["status"] != "ok":
                print("The review response could not be parsed.")
                if result.get("text"):
                    print(_line(result["text"]))
            elif not result["findings"]:
                print("No concrete defects reported.")
            for item in result["findings"]:
                print(f"[{_line(item['severity'])}] {_line(item['file'])}:"
                      f"{_line(item['line'])} {_line(item['title'])}\n"
                      f"  {_line(item['detail'])}")
        else:
            _emit(result)
        if result["status"] != "ok":
            return 2 if args.fail_on else 0
        if args.fail_on:
            rank = {"low": 1, "medium": 2, "high": 3}
            if any(rank[item["severity"]] >= rank[args.fail_on] for item in result["findings"]):
                return 1
        return 0
    if args.command == "chat":
        from .coding import Repository
        from .session import interactive
        from .store import SessionError, SessionStore

        store = session = saved = None
        if args.no_sessions and (args.resume or args.continue_session):
            parser.error("--resume/--continue need the session store; drop --no-sessions.")
        if args.resume and args.continue_session:
            parser.error("Use either --resume or --continue, not both.")
        try:
            repository = Repository(args.repo, args.ref)
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        try:
            if not args.no_sessions:
                store = SessionStore()
                if args.continue_session:
                    recent = store.list(repository.root, limit=50)
                    match = next((item for item in recent
                                  if item["base"] == repository.commit.decode()), None)
                    if match is not None:
                        saved = store.load(match["id"])
                    else:
                        print("No matching saved session; starting a new one.",
                              file=sys.stderr)
                elif args.resume:
                    saved = store.load(args.resume)
        except SessionError as exc:
            parser.error(_session_message(exc))
        if saved is not None and Path(saved.repo) != repository.root:
            parser.error("That session belongs to another repository.")
        if saved is not None and saved.base != repository.commit.decode():
            parser.error("That session started from another commit; start a new session.")
        if store is not None and saved is None:
            saved = store.create(repository.root, model, repository.commit.decode())
        prior = [entry["text"] for entry in saved.entries if entry["kind"] == "user"][-4:] if saved else []
        from .extensions import build_extensions

        from .agent import Budget

        cfg = _apply_limits(args, cfg, parser)
        key = os.environ.get(cfg.provider.api_key_env)
        state = {"model": model}
        budget = Budget(cfg.token_budget)

        def chat_client():
            return _client(cfg.provider, state["model"], cfg.fallback_models)

        def switch_model(value, active=None):
            state["model"] = value
            target = active or saved
            if store is not None and target is not None:
                target.model = value
                try:
                    store.save(target)
                except (SessionError, OSError, ValueError):
                    pass

        chat_holder = {"value": model}
        extensions = build_extensions(
            repository, cfg, chat_factory=chat_client,
            subagent_factory=lambda: _client(
                cfg.provider, cfg.subagent_model or chat_holder["value"],
                cfg.fallback_models),
            budget=budget)
        try:
            interactive(repository, chat_client,
                        sys.stdin, sys.stdout, prior=prior, store=store, session=saved,
                        belt=extensions.belt, system=extensions.system,
                        max_turns=cfg.max_turns, max_calls=cfg.max_calls,
                        context_budget=cfg.context_budget, token_budget=cfg.token_budget,
                        retries=cfg.retries, model=model, set_model=switch_model,
                        verify_commands=cfg.verify, commands=discover_commands(cfg.commands),
                        models_fetcher=((lambda: list_models(cfg.provider.base_url, key,
                                                             dict(cfg.provider.headers))) if key else None),
                        prompts=extensions.prompts, skills=extensions.skills,
                        warnings=extensions.warnings,
                        secret_env=(cfg.provider.api_key_env,), budget=budget,
                        fix_config=cfg, model_holder=chat_holder,
                        review_model=cfg.review_model,
                        subagent_usage=extensions.subagent_usage,
                        review_factory=((lambda: _client(
                            cfg.provider, cfg.review_model, cfg.fallback_models))
                            if key and cfg.review_model else None))
        except KeyboardInterrupt:
            return 130
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        finally:
            extensions.close()
        return 0
    if args.command == "code-bench":
        import subprocess

        from .code_benchmark import evaluate
        from .config import Provider

        bench_provider = Provider()
        if not os.environ.get(bench_provider.api_key_env):
            parser.error(f"Set {bench_provider.api_key_env} in your environment.")
        try:
            report = evaluate(lambda: _client(bench_provider, args.model), model=args.model)
        except (ValueError, OSError, subprocess.SubprocessError):
            parser.error("Cannot initialize coding benchmark; check the Git installation.")
        _emit(report)
        return int(any(not row["passed"] for row in report["runs"]))
    if args.command == "code":
        from .coding import Repository, solve
        from .context import ContextError, prompt_block as context_block
        from .context import read_context
        from .extensions import build_extensions
        from .preflight import PreflightError, verify_patch

        if not 0 <= args.fix_attempts <= 3:
            parser.error("--fix-attempts must be between 0 and 3.")
        if args.fix_attempts and not args.verify:
            parser.error("--fix-attempts requires --verify.")
        if args.review_fail_on and not args.review:
            parser.error("--review-fail-on requires --review.")
        if (args.review_fix_attempts or args.review_fix_on != "high") and not args.review:
            parser.error("--review-fix-attempts/--review-fix-on require --review.")
        if not 0 <= args.review_fix_attempts <= 2:
            parser.error("--review-fix-attempts must be between 0 and 2.")
        if not 1 <= args.verify_timeout <= 1800:
            parser.error("--verify-timeout must be between 1 and 1800 seconds.")
        if args.review_model is not None:
            from .core import MODEL_ID

            if not MODEL_ID.fullmatch(args.review_model):
                parser.error("--review-model must be a provider model ID.")
            if not args.review:
                parser.error("--review-model requires --review.")
        if args.plan and (args.verify or args.review or args.fix_attempts
                          or args.review_fix_attempts or args.patch_out is not None):
            parser.error("--plan is read-only; drop --verify, --review, repair flags "
                         "and --patch-out.")
        if args.patch_out is not None:
            from .apply import check_patch_target

            try:
                check_patch_target(args.patch_out)
            except ValueError as exc:
                parser.error(_line(str(exc))[:1000])
        extensions = None
        verification = None
        used_attempts = 0
        review_attempts = 0
        review_history = []
        try:
            cfg = _apply_limits(args, cfg, parser)
            repository = Repository(args.repo, args.ref)
            try:
                attachment = context_block(read_context(args.context))
            except ContextError as exc:
                parser.error(_line(str(exc))[:1000])
            initial_prompt = f"{args.prompt}\n\n{attachment}" if attachment else args.prompt
            if args.plan:
                cfg = replace(cfg, permission="plan")
            if cfg.permission == "plan" and args.no_extensions:
                parser.error("Plan mode needs the extension belt; drop --no-extensions.")
            if args.verify and not cfg.verify:
                parser.error("Configure verify commands in nalar.json first.")
            if args.verify and not args.yes:
                if not sys.stdin.isatty():
                    parser.error("Review the verification commands and pass --yes.")
                print("Nalar will run:", file=sys.stderr)
                for argv in cfg.verify:
                    print("  " + " ".join(_line(arg) for arg in argv), file=sys.stderr)
                if not _confirm("Type yes to run: "):
                    return 2
            from .agent import Budget

            budget = Budget(cfg.token_budget)
            if not args.no_extensions:
                extensions = build_extensions(
                    repository, cfg,
                    chat_factory=lambda: _client(cfg.provider, model,
                                                 cfg.fallback_models),
                    subagent_factory=lambda: _client(
                        cfg.provider, cfg.subagent_model or model,
                        cfg.fallback_models),
                    budget=budget)
            events = None
            handle = None
            if args.events:
                try:
                    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                             | os.O_NOFOLLOW, 0o600)
                    handle = os.fdopen(fd, "w", encoding="utf-8")
                except OSError as exc:
                    parser.error(f"Cannot write events: {exc.strerror or 'file error'}")

                def emit(event, handle=handle):
                    handle.write(json.dumps(event, ensure_ascii=True) + "\n")
                    handle.flush()
                events = emit

            def on_event(event):
                if events is not None:
                    events(event)
                if not sys.stderr.isatty():
                    return
                kind = event.get("type")
                if kind == "tool":
                    print(f"  · {_clean(event['name'])} {_clean(event['status'])}",
                          file=sys.stderr, flush=True)
                elif kind == "retry":
                    print(f"  · retry {_clean(event['code'])} (attempt {event['attempt']})",
                          file=sys.stderr, flush=True)

            usage = {"turns": 0, "calls": 0}

            def run_once(prompt):
                result, diff = solve(
                    chat, repository, prompt, mode=args.mode,
                    belt=extensions.belt if extensions else None,
                    system=extensions.system if extensions else None,
                    max_turns=cfg.max_turns, max_calls=cfg.max_calls,
                    context_budget=cfg.context_budget,
                    token_budget=cfg.token_budget,
                    approve=_approver(args), on_event=on_event,
                    budget=budget, retries=cfg.retries)
                usage["turns"] += result.turns
                usage["calls"] += result.calls
                return result, diff

            def check(patch):
                if not patch:
                    return {"ok": False, "error": "no_patch", "commands": []}
                try:
                    return verify_patch(repository, patch, cfg.verify,
                                        timeout=args.verify_timeout,
                                        secret_names=(cfg.provider.api_key_env,))
                except PreflightError as exc:
                    return {"ok": False, "error": str(exc), "commands": []}

            outcome = None
            review_error = None
            try:
                result, diff = run_once(initial_prompt)
                if args.verify:
                    verification = check(diff)
                    if events is not None:
                        events({"type": "verify", "ok": verification.get("ok"), "attempt": 0})
                    while not verification.get("ok") and used_attempts < args.fix_attempts:
                        error = str(verification.get("error") or "")
                        if verification.get("error") in {"apply_failed"} or "Cannot export" in error:
                            break
                        used_attempts += 1
                        if events is not None:
                            events({"type": "fix_attempt", "attempt": used_attempts})
                        prompt = ("Your previous patch failed the configured verification. "
                                  "Fix the code and produce a corrected patch; do not claim tests "
                                  "you did not run.\n\n" + _verification_feedback(verification, diff))
                        repository.max_edits = len(repository.changed) + 12
                        new_result, new_diff = run_once(prompt)
                        if new_result.status != "ok" or not new_diff:
                            break
                        result, diff = new_result, new_diff
                        verification = check(diff)
                        if events is not None:
                            events({"type": "verify", "ok": verification.get("ok"),
                                    "attempt": used_attempts})
                if args.review:
                    from .core import ProviderError
                    from .review import review as review_patch

                    rank = {"low": 1, "medium": 2, "high": 3}
                    review_model = args.review_model or cfg.review_model
                    used_review_model = review_model or model
                    review_chat = chat
                    if review_model and review_model != model:
                        review_chat = _client(cfg.provider, review_model,
                                              cfg.fallback_models)

                    def run_review():
                        nonlocal outcome, review_error
                        if not diff or result.status != "ok":
                            outcome = {"status": "no_patch" if not diff else "skipped",
                                       "findings": [], "text": "", "tokens": None,
                                       "model": used_review_model}
                        else:
                            try:
                                outcome = review_patch(review_chat, diff,
                                                       model=used_review_model,
                                                       retries=cfg.retries, budget=budget)
                            except ProviderError as exc:
                                review_error = str(exc)
                                outcome = {"status": "provider_error",
                                           "error": review_error, "findings": [],
                                           "text": "", "tokens": None,
                                           "model": used_review_model}
                        if events is not None:
                            events({"type": "review", "status": outcome["status"]})
                        review_history.append({
                            "status": outcome["status"],
                            "findings": len(outcome["findings"]),
                            "tokens": outcome.get("tokens") or 0,
                            "model": outcome.get("model", "")})

                    run_review()
                    while (outcome is not None and outcome["status"] == "ok" and diff
                           and review_attempts < args.review_fix_attempts):
                        blocking = [item for item in outcome["findings"]
                                    if rank[item["severity"]] >= rank[args.review_fix_on]]
                        if not blocking:
                            break
                        review_attempts += 1
                        if events is not None:
                            events({"type": "review_fix", "attempt": review_attempts,
                                    "findings": len(blocking)})
                        repository.max_edits = len(repository.changed) + 12
                        new_result, new_diff = run_once(_review_fix_prompt(diff, blocking))
                        if new_result.status != "ok" or not new_diff:
                            break
                        result, diff = new_result, new_diff
                        if args.verify:
                            verification = check(diff)
                            if events is not None:
                                events({"type": "verify", "ok": verification.get("ok"),
                                        "attempt": used_attempts, "phase": "review"})
                            if not verification.get("ok"):
                                # The repaired patch is not the one the previous
                                # review described; never report stale findings.
                                outcome = {"status": "skipped", "findings": [],
                                           "text": "", "tokens": None,
                                           "model": used_review_model}
                                review_history.append({
                                    "status": "skipped", "findings": 0,
                                    "tokens": 0,
                                    "model": used_review_model})
                                break
                        run_review()
            finally:
                if handle is not None:
                    handle.close()
        except ValueError as exc:
            parser.error(_line(str(exc))[:1000])
        finally:
            if extensions is not None:
                extensions.close()
        metadata = {"base": repository.commit.decode(), "model": model,
                    "warnings": list(extensions.warnings) if extensions else [],
                    **asdict(result), "tokens": budget.spent,
                    "turns": usage["turns"], "calls": usage["calls"],
                    "patch_created": bool(diff)}
        if diff:
            from .secrets import scan_diff

            try:
                metadata["secrets"] = scan_diff(diff)
            except ValueError:
                metadata["secrets"] = {"findings": [], "truncated": True,
                                       "capped": False, "malformed": True}
            if metadata["secrets"]["findings"]:
                print("Warning: possible credentials in added lines: "
                      + ", ".join(
                          f"{_line(item['file'])}:{_line(item['line'])} {_line(item['kind'])}"
                          for item in metadata["secrets"]["findings"][:5]),
                      file=sys.stderr)
        patch_error = None
        if args.patch_out is not None:
            from .apply import write_patch

            if diff:
                try:
                    metadata["patch_path"] = write_patch(args.patch_out, diff)
                except ValueError as exc:
                    patch_error = str(exc)
                    metadata["patch_path"] = None
            else:
                metadata["patch_path"] = None
        if verification is not None:
            metadata["verification"] = verification
            metadata["fix_attempts"] = used_attempts
        review_tokens_total = sum(item.get("tokens", 0)
                                  for item in review_history)
        if outcome is not None:
            metadata["review"] = {**outcome, "tokens": review_tokens_total}
        if (args.review and outcome is not None
                and outcome.get("status") in {"ok", "unparsed", "provider_error",
                                               "skipped"}):
            metadata["review_model"] = (args.review_model or cfg.review_model
                                        or model)
        if review_history:
            metadata["review_history"] = review_history
        if review_attempts:
            metadata["review_fix_attempts"] = review_attempts
        if review_error is not None:
            metadata["review_error"] = review_error
        if args.record:
            try:
                from .apply import patch_paths
                from .store import SessionStore

                files = patch_paths(diff) if diff else ()
                review_tokens = review_tokens_total
                review = metadata.get("review")
                usage = take_usage(extensions.subagent_usage) if extensions else []
                delegated = sum(usage_totals(usage).values())
                session = SessionStore().record_run(
                    repo=repository.root, model=model,
                    base=repository.commit.decode(), task=args.prompt,
                    status=result.status, text=result.text or "",
                    tokens=max(0, metadata["tokens"] - review_tokens - delegated),
                    turns=metadata["turns"], calls=metadata["calls"],
                    verification=verification,
                    review=review, diff=diff, files=files, usage=usage)
                metadata["session"] = session.id
            except (ValueError, OSError) as exc:
                metadata["record_error"] = _clean(_session_message(exc))[:200]
                print("Warning: the run could not be recorded.", file=sys.stderr)
        if sys.stderr.isatty():
            if result.plan:
                for line in _plan_text(result.plan):
                    print(line, file=sys.stderr, flush=True)
            if result.text:
                print(_clean(result.text), file=sys.stderr, flush=True)
            if outcome is not None and outcome.get("status") == "ok":
                for line in _review_text(outcome):
                    print(line, file=sys.stderr, flush=True)
        if args.json:
            metadata["patch"] = diff
            print(json.dumps(metadata, ensure_ascii=True))
        else:
            print(json.dumps(metadata, ensure_ascii=True), file=sys.stderr)
            if diff and not _print_patch(diff):
                return 1
        if patch_error is not None:
            parser.error(patch_error)
        if args.plan:
            ok = result.status == "ok"
        else:
            ok = bool(diff) and (verification is None or verification.get("ok"))
        if review_error is not None:
            ok = False
        if outcome is not None:
            if outcome["status"] != "ok":
                ok = False
            elif args.review_fail_on:
                rank = {"low": 1, "medium": 2, "high": 3}
                if any(rank[item["severity"]] >= rank[args.review_fail_on]
                       for item in outcome["findings"]):
                    ok = False
        return 0 if ok else 1
    if args.command == "bench":
        report = benchmark(chat, model=model)
        report["provider"] = "hosted"
        _emit(report)
        return int(any(row["status"] == "provider_error" for row in report["runs"]))
    handle = None
    events = None
    if args.events:
        try:
            fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except OSError as exc:
            parser.error(f"Cannot write events: {exc.strerror or 'file error'}")

        def emit(event, handle=handle):
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")
            handle.flush()
        events = emit
    limits = _limit_overrides(args, parser)
    try:
        with Workspace(args.workspace) as workspace:
            result = run(chat, workspace, args.prompt, mode=args.mode, on_event=events,
                         max_turns=limits.get("max_turns", 8),
                         max_calls=limits.get("max_calls", 12),
                         token_budget=limits.get("token_budget", 0),
                         retries=limits.get("retries", 0))
    except OSError:
        print("Cannot open workspace; choose an existing, accessible directory.", file=sys.stderr)
        return 1
    finally:
        if handle is not None:
            handle.close()
    _emit(asdict(result))
    return int(result.status != "ok")


if __name__ == "__main__":
    sys.exit(main())
