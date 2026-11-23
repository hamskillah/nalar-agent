"""Shell completion scripts generated from one subcommand table."""

# Keep in sync with the argparse subcommands in __main__. A test asserts every
# name appears in `nalar --help`.
SUBCOMMANDS = {
    "run": "perform a task with a scoped workspace",
    "bench": "grade baseline and recovery on artifact tasks",
    "serve": "start the single-user local web interface",
    "code": "propose a patch against a committed Git snapshot",
    "fix": "verify the snapshot and propose a patch for failures",
    "chat": "revise and review coding proposals interactively",
    "code-bench": "evaluate patches on synthetic Git tasks",
    "tui": "open the full-screen coding workspace",
    "plan": "produce a read-only implementation plan",
    "ask": "answer a question about the repository without editing",
    "review": "review a diff and print structured findings",
    "apply": "apply a reviewed patch with safety checks",
    "init": "create starter configuration and a review skill",
    "models": "list models offered by the configured provider",
    "verify": "run configured verification commands",
    "doctor": "check environment, provider and extensions",
    "sessions": "inspect saved session transcripts",
    "config": "inspect the effective configuration",
    "plugins": "inspect user-level tool plugins",
    "skills": "inspect project skills",
    "commands": "inspect prompt commands",
    "mcp": "inspect configured MCP servers",
    "mcp-serve": "expose read-only repository tools over MCP (stdio)",
    "scan": "scan a diff for high-confidence credentials",
    "completions": "print a completion script for your shell",
}

GLOBAL_FLAGS = ("--help", "--version")

# First positional values offered for action-style subcommands.
FLAG_VALUES = {
    "--scope": ("worktree", "staged", "branch"),
    "--mode": ("recover", "baseline"),
    "--format": ("json", "text"),
    "--fail-on": ("high", "medium", "low"),
    "--review-fail-on": ("high", "medium", "low"),
    "--review-fix-on": ("high", "medium", "low"),
}
SUBCOMMAND_FLAG_VALUES = {
    "review:--format": ("json", "text", "sarif", "markdown", "github"),
    "scan:--format": ("json", "text", "sarif", "markdown", "github"),
    "scan:--scope": ("worktree", "staged", "branch", "tree"),
    "sessions:--format": ("json", "text", "markdown"),
}


def flag_values(name, flag):
    return SUBCOMMAND_FLAG_VALUES.get(f"{name}:{flag}", FLAG_VALUES.get(flag))

VALUES = {
    "completions": ("bash", "zsh", "fish"),
    "sessions": ("list", "show", "export", "delete", "stats", "prune", "fork",
                 "search"),
    "config": ("show", "set", "unset"),
    "commands": ("list", "show", "check", "init"),
    "skills": ("list", "show", "check", "init"),
    "plugins": ("list", "check", "init"),
    "mcp": ("list", "prompts", "prompt", "resources", "add", "remove", "test",
            "enable", "disable"),
}

# Options offered per subcommand; a test keeps them in sync with `--help`.
FLAGS = {
    'run': (
        '--model',
        '--workspace',
        '--mode',
        '--events',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
    ),
    'bench': (
        '--model',
    ),
    'serve': (
        '--port',
        '--state',
        '--repo',
        '--verify-timeout',
        '--trust-project',
    ),
    'fix': (
        '--repo',
        '--model',
        '--ref',
        '--max-fix-attempts',
        '--verify-timeout',
        '--yes',
        '--patch-out',
        '--events',
        '--context',
        '--instructions',
        '--no-extensions',
        '--format',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
        '--record',
    ),
    'code': (
        '--model',
        '--repo',
        '--ref',
        '--mode',
        '--no-extensions',
        '--plan',
        '--yes',
        '--events',
        '--verify',
        '--fix-attempts',
        '--review',
        '--review-model',
        '--review-fail-on',
        '--review-fix-attempts',
        '--review-fix-on',
        '--verify-timeout',
        '--context',
        '--patch-out',
        '--json',
        '--record',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
    ),
    'chat': (
        '--model',
        '--repo',
        '--ref',
        '--resume',
        '-c',
        '--continue',
        '--no-sessions',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
    ),
    'code-bench': (
        '--model',
    ),
    'tui': (
        '--model',
        '--repo',
        '--ref',
        '--no-extensions',
        '--plan',
        '--no-sessions',
        '--resume',
        '-c',
        '--continue',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
    ),
    'plan': (
        '--model',
        '--repo',
        '--ref',
        '--out',
        '--json',
        '--context',
        '--record',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
    ),
    'ask': (
        '--model',
        '--repo',
        '--ref',
        '--json',
        '--context',
        '--record',
        '--max-turns',
        '--max-calls',
        '--token-budget',
        '--retries',
        '--trust-project',
    ),
    'review': (
        '--model',
        '--review-model',
        '--repo',
        '--ref',
        '--scope',
        '--include-untracked',
        '--fail-on',
        '--format',
        '--file',
        '--context',
        '--trust-project',
    ),
    'apply': (
        '--repo',
        '--base',
        '--check',
        '--revert',
        '--last',
        '--verify',
        '--verify-timeout',
        '--yes',
        '--include',
        '--exclude',
        '--allow-secrets',
        '--format',
        '--trust-project',
    ),
    'init': (
        '--repo',
        '--model',
        '--force',
        '--detect',
    ),
    'models': (
        '--repo',
        '--format',
        '--trust-project',
    ),
    'verify': (
        '--repo',
        '--yes',
        '--timeout',
        '--format',
        '--trust-project',
    ),
    'doctor': (
        '--repo',
        '--mcp',
        '--connect',
        '--format',
        '--trust-project',
    ),
    'sessions': (
        '--yes',
        '--query',
        '--keep',
        '--limit',
        '--model',
        '--repo',
        '--format',
    ),
    'config': (
        '--repo',
        '--format',
        '--trust-project',
    ),
    'commands': (
        '--repo',
        '--format',
        '--force',
        '--description',
        '--trust-project',
    ),
    'plugins': (
        '--repo',
        '--format',
        '--force',
        '--description',
        '--trust-project',
    ),
    'skills': (
        '--repo',
        '--format',
        '--force',
        '--description',
        '--trust-project',
    ),
    'mcp': (
        '--arg',
        '--force',
        '--url',
        '--tool',
        '--read',
        '--header',
        '--env',
        '--format',
        '--repo',
        '--trust-project',
    ),
    'mcp-serve': (
        '--repo',
        '--print-config',
        '--http',
        '--port',
        '--token-env',
    ),
    'scan': (
        '--repo',
        '--scope',
        '--ref',
        '--include-untracked',
        '--file',
        '--format',
    ),
    'completions': (
    ),
}

# Subcommands that take a patch file as their last argument.
FILE_ARGUMENT = {"apply", "review"}


def bash(prog="nalar"):
    words = " ".join([*SUBCOMMANDS, *GLOBAL_FLAGS])
    flag_cases = "\n".join(
        f'        {name}) options="{" ".join(items)}" ;;'
        for name, items in FLAGS.items())
    value_cases = "\n".join(
        f'            {name}) COMPREPLY=( $(compgen -W "{" ".join(items)}" -- "$cur") ); return 0 ;;'
        for name, items in VALUES.items())
    flag_value_cases = "\n".join(
        f'            {name}:{flag}) COMPREPLY=( $(compgen -W "{" ".join(flag_values(name, flag))}" '
        f'-- "$cur") ); return 0 ;;'
        for name, flags in FLAGS.items() for flag in flags if flag_values(name, flag))
    file_cases = "\n".join(
        f'        {name}) compopt -o default ;;' for name in sorted(FILE_ARGUMENT))
    return f'''# {prog} completion for bash; add: source <({prog} completions bash)
_{prog}() {{
    local cur sub options
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "{words}" -- "$cur") )
        return 0
    fi
    sub="${{COMP_WORDS[1]}}"
    options=""
    case "$sub" in
{flag_cases}
    esac
    if [ "$COMP_CWORD" -ge 3 ] && [[ "$cur" != -* ]]; then
        case "$sub:${{COMP_WORDS[COMP_CWORD-1]}}" in
{flag_value_cases}
        esac
    fi
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
        return 0
    fi
    if [ "$COMP_CWORD" -eq 2 ]; then
        case "$sub" in
{value_cases}
        esac
    fi
    case "$sub" in
{file_cases}
    esac
}}
complete -F _{prog} {prog}
'''


def zsh(prog="nalar"):
    GLOBAL = " ".join(GLOBAL_FLAGS)
    entries = "\n".join(f"        '{name}:{text}'" for name, text in SUBCOMMANDS.items())
    flag_cases = "\n".join(
        f"        {name}) flags=({' '.join(items)}) ;;" for name, items in FLAGS.items())
    value_cases = "\n".join(
        f"            {name}) values=({' '.join(items)}) ;;" for name, items in VALUES.items())
    flag_value_cases = "\n".join(
        f"            {name}:{flag}) values=({' '.join(flag_values(name, flag))}) ;;"
        for name, flags in FLAGS.items() for flag in flags if flag_values(name, flag))
    return f'''#compdef {prog}
_{prog}() {{
    local -a subcommands flags values
    flags=()
    values=()
    subcommands=(
{entries}
    )
    if (( CURRENT == 2 )); then
        if [[ "$words[CURRENT]" == -* ]]; then
            compadd -- {GLOBAL}
        else
            _describe 'command' subcommands
        fi
        return
    fi
    case "$words[2]" in
{flag_cases}
    esac
    if (( CURRENT > 2 )) && [[ "$words[CURRENT]" != -* ]]; then
        case "$words[2]:$words[CURRENT-1]" in
{flag_value_cases}
        esac
        if (( ${{#values}} )); then
            compadd -- $values
            return
        fi
    fi
    if [[ "$words[CURRENT]" == -* ]]; then
        if (( ${{#flags}} )); then
            compadd -- $flags
        fi
        return
    fi
    if (( CURRENT == 3 )); then
        case "$words[2]" in
{value_cases}
        esac
        if (( ${{#values}} )); then
            compadd -- $values
            return
        fi
    fi
    _files
}}
if [ "$funcstack[1]" = "_{prog}" ]; then
    _{prog} "$@"
elif (( $+functions[compdef] )); then
    compdef _{prog} {prog}
fi
'''


def fish(prog="nalar"):
    lines = [f"complete -c {prog} -f"]
    lines.append(f"complete -c {prog} -l help -d 'show help'")
    lines.append(f"complete -c {prog} -l version -d 'show version'")
    for name, text in SUBCOMMANDS.items():
        lines.append(f"complete -c {prog} -n __fish_use_subcommand -a {name} "
                     f"-d '{text}'")
    for name, items in FLAGS.items():
        for flag in items:
            if flag.startswith("--"):
                lines.append(f"complete -c {prog} "
                             f"-n '__fish_seen_subcommand_from {name}' -l {flag[2:]}")
            elif flag.startswith("-") and len(flag) == 2:
                lines.append(f"complete -c {prog} "
                             f"-n '__fish_seen_subcommand_from {name}' -s {flag[1:]}")
    for name, items in VALUES.items():
        lines.append(f"complete -c {prog} -n '__fish_seen_subcommand_from {name}' "
                     f"-a '{' '.join(items)}'")
    flag_subs = {}
    for name, flags in FLAGS.items():
        for flag in flags:
            values = flag_values(name, flag)
            if values:
                flag_subs.setdefault((flag, tuple(values)), []).append(name)
    for (flag, values), subs in flag_subs.items():
        lines.append(f"complete -c {prog} -n '__fish_seen_subcommand_from {' '.join(subs)}' "
                     f"-l {flag[2:]} -x -a '{' '.join(values)}'")
    for name in sorted(FILE_ARGUMENT):
        lines.append(f"complete -c {prog} -n '__fish_seen_subcommand_from {name}' -F")
    return "\n".join(lines) + "\n"
