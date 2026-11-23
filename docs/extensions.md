# Extensions

Nalar is a harness first: the model may only act through the tools it is
given. Extensions add tools; they never bypass the Git-only write policy or
the review step.

## Where a setting may live

| Setting | Project `nalar.json` | User config |
| --- | --- | --- |
| model, review_model, subagent_model/max_turns/max_calls/token_budget, turn/call budgets, retries, skills, instructions, verify | yes | yes |
| mcp servers, provider, tools, subagents, token_budget | only with `--trust-project` | yes |
| permission | only `plan` | yes |
| plugins | rejected | yes |

Project configuration is untrusted by default: cloning a repository must not
start processes, silence review, or redirect your provider key. `--trust-project`
(or `NALAR_TRUST_PROJECT=1`) is an explicit decision to trust that checkout.

## Provider

The `provider` object selects the OpenAI-compatible endpoint (user-level, or
project-level with `--trust-project`):

```json
{"provider": {"base_url": "https://gateway.example/v1/chat/completions",
              "api_key_env": "GATEWAY_API_KEY",
              "headers": {"X-Organisation": "team"},
              "session_header": false}}
```

`api_key_env` names the environment variable holding the key; the value is
never written to config or transcripts. Header values support `{env:NAME}`.
`fallback_models` (up to five) are tried only when the primary model returns
HTTP 404; other errors propagate. After a failover the client keeps using the
working model and advances again only if that model starts returning 404. `session_header` controls the the default provider
routing header. `max_tokens`
(1–32768, default 1536), `temperature` (0–2, default 0) and `timeout`
(1–600 s, default 60) tune requests. Non-loopback
plain-http endpoints are rejected. Streaming and non-streaming requests share
the same endpoint, tool-call assembly and retry policy.

## Configuration

`nalar.json` in the repository, or `~/.config/nalar/config.json` for user
defaults. `NALAR_GLOBAL_CONFIG` overrides the user file path.

```json
{
  "model": "beam-free",
  "max_turns": 12,
  "max_calls": 24,
  "context_budget": 120000,
  "permission": "ask",
  "skills": ["skills"],
  "mcp": {
    "docs": {
      "command": ["npx", "-y", "@example/docs-mcp"],
      "env": {"DOCS_TOKEN": "{env:DOCS_TOKEN}"}
    },
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer {env:REMOTE_TOKEN}"}
    }
  }
}
```

Unknown keys and invalid values are rejected rather than silently ignored.
`{env:NAME}` is resolved at load time; a missing variable is an error and no
secret is written to disk by Nalar. Inspect the result with
`nalar config show`; change user values with `nalar config set KEY VALUE` and
`nalar config unset KEY`. Edits are validated against the full schema and
written atomically with mode `0600`; an invalid change leaves the file intact.

`permission` is `ask` (default: every proposal needs explicit approval) or
`auto` (apply a successful proposal immediately if HEAD and the working tree
still match the reviewed base). Auto mode still refuses dirty checkouts.

## Skills

Skills are directories with a `SKILL.md`:

```
.nalar/skills/review/SKILL.md
---
name: review
description: Review a change for regressions
---
Read the patch, then check error paths first.
```

The default directory is `.nalar/skills`; `skills` adds more relative
directories. Names, sizes and frontmatter are validated. The system prompt
lists the names and descriptions; the model must call `use_skill` to read a
body. Skill text is untrusted project data: it cannot grant permissions,
change tool limits, or waive the patch review. `nalar skills list` shows what
would load. `nalar skills init NAME [--description TEXT]` writes a starter
`.nalar/skills/NAME/SKILL.md` without overwriting an existing file (use
`--force` to replace it) and refuses symlinked components.

## MCP servers

Nalar speaks MCP over stdio (a `command`) or streamable HTTP (a `url`).
Exactly one of the two is required. It performs the `initialize` handshake,
imports tools as `<server>__<tool>`, and closes a stdio process when the
session ends. HTTP endpoints must use TLS except for loopback addresses, so
configured headers never travel in clear text; redirects are not followed and
response bodies are bounded (4 MB, 15 s). Both JSON and
`text/event-stream` responses are supported, and a returned `Mcp-Session-Id`
is echoed on later requests.

- A stdio child receives only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
  `TZ` plus its configured `env`. Provider credentials are not inherited.
- HTTP header values support `{env:NAME}` but are sent only to the configured
  endpoint, which must be TLS (or loopback).
- Tool results are capped at 32 KB; requests time out after 15 seconds; tool
  lists are capped at 128 entries.
- A server that fails to start or misbehaves becomes a warning. The rest of
  the session continues.
- `nalar mcp list` reports per-server status, tools, resources and prompts
  without starting a model request.
- `nalar mcp add NAME --url URL [--header K=V]` or
  `nalar mcp add NAME [--env K=V] -- CMD` writes the user config with the same
  validation; `mcp remove NAME` deletes it.
- `mcp test NAME [--tool NAME --arg k=v]` initializes one enabled server and
  reports its tools/resources/prompts (a tools-less server reports `[]`), with
  one optional tool call.
- Prompts are listed by `nalar mcp prompts` and can be inserted from the TUI;
  `nalar mcp prompt <server>__<name> --arg key=value` prints the rendered text.
- Text resources are exposed as `<server>__read_resource`, with the URI list
  shown in the tool description and validated at call time. Binary or missing
  content becomes a structured tool error.

Only configure servers you trust: MCP tools execute with your user account.

### Serving Nalar over MCP

`nalar mcp-serve --repo .` exposes the committed snapshot to another agent over
MCP stdio. It advertises only read-only tools (`list_files`, `search`,
`read_lines`, `find_symbol`, `repo_map`, `git_log`, `git_show`); write tools and unknown
names return an `isError` result. Paths are validated against the repository,
results are bounded JSON, one request is one newline-delimited JSON-RPC message,
and request lines, repository access and failures are capped. An oversized
physical line is drained instead of being re-read as a second request, content
limits exclude the trailing newline, blank transport lines are ignored, and a
closed stdout shuts down without an interpreter traceback. Batch arrays are
served for negotiated versions before 2025-06-18 and rejected for that version.
An unknown resource URI returns the MCP `-32002` resource-not-found error. `initialize` negotiates any of the
supported protocol versions (2024-11-05, 2025-03-26, 2025-06-18) and returns
`-32602` for malformed params. Notifications never produce a response. The
server never edits, applies patches, runs commands or reads outside the
snapshot, so it needs no provider key and no project trust.

`nalar mcp-serve --repo . --print-config` prints an `mcpServers` snippet for a
client. The full reference is in [mcp-server.md](mcp-server.md).

`resources/list` and `resources/read` expose committed `NALAR.md` and `AGENTS.md`
as `nalar://instructions/<name>` markdown resources, plus committed project
skills (`.nalar/skills/<name>/SKILL.md`) as `nalar://skills/<name>` resources
with their frontmatter description and body. They are labelled untrusted data,
omitted when missing, invalid, unreadable or oversized, and never read from the
working tree; an unknown URI is a `-32002` resource-not-found error.

`prompts/list` and `prompts/get` expose committed `.nalar/commands/*.md` as MCP
prompts. Each prompt takes one optional `arguments` string substituted for
`$ARGUMENTS` (or appended as `Arguments:` when the body has no placeholder);
the rendered text is capped by the same 6 KB command limit, and unknown names or
arguments are invalid-params errors. Uncommitted commands and working-tree edits
are never read.

## Python tool plugins

Plugins are executable code, so they load **only** from the user-level
configuration. A repository's `nalar.json` cannot enable one — otherwise a
cloned repository could run code on your machine.

```json
{"plugins": ["my_nalar_tools"]}

Modules can also be dropped as `.py` files into the `plugins/` directory beside
the global config (`~/.config/nalar/plugins/`). Up to eight files of 256 KiB are
loaded; symlinks, invalid names and failures are skipped with a warning.
`nalar plugins list` shows the configured modules, the directory, loaded tool
names and load warnings. `nalar plugins init NAME` writes a starter module
(mode 0600) to that directory; it loads automatically on the next run, so
review it first. Plugin code runs with your account when tools are loaded, so
only install modules and files you trust.
```

Installed packages may declare a `nalar.tools` entry point returning a module
or a `tools()` callable; these load only from packages you installed.

The module exposes `tools()` returning entries like:

```python
{"name": "lookup_ticket", "description": "Read a ticket",
 "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                "required": ["id"]},
 "handler": lambda args: fetch(args["id"])}
```

Names must be unique and match `[A-Za-z0-9_-]{1,64}`; descriptions are capped
at 1024 characters. Handler exceptions become a generic tool error, never a
traceback in the transcript.

## SDK

```python
from nalar import Agent

with Agent("/path/to/repo") as agent:          # model from nalar.json
    result = agent.run("Fix the empty-list case",
                       on_event=lambda event: print(event["type"]))
    print(result["status"], result["patch"])
```

`run()` returns the status, final text, token count, trace, base commit,
skill names, warnings and the proposed unified patch. Pass `on_text=` to stream
text pieces when the provider adapter has `stream()`; `command()` forwards it too.
Events are
`request`, `reply`, `tool`, `retry`, `approval` and `final`. The TUI renders
`request`/`tool`/`retry` events in its status bar while a task runs, and the CLI
`chat` prints `· name status` progress lines when stdout is a terminal. Pass `chat=` to use another
provider adapter, or `start_mcp=False` for an offline test.

The SDK mirrors the interactive flow:

```python
with Agent("/path/to/repo") as agent:
    agent.command("audit", "src/parser.py")   # a .nalar/commands template
    print(agent.patch)                        # staged proposal
    agent.review()                            # structured findings
    # or Agent(..., review_chat=second_client) for a separate reviewer
    agent.apply()                             # apply with clean-tree checks
    agent.verify()                            # configured checks
    agent.reset()                             # drop the proposal

with Agent("/path/to/repo", record=True) as agent:
    agent.run("Fix the empty-list case")      # private run summary in sessions

with WorkspaceAgent("/path/to/notes", record=True) as agent:
    agent.run("Summarize the open items")     # task/status/usage only
```

`commands` lists discovered prompt commands. `record=True` stores task, status,
tokens/turns/calls, verification/review summaries and the patch hash/file names
in the session store; patch contents and command output are never stored. `apply()`, `review()` and
`verify()` reuse the CLI checks and raise `ValueError` when there is nothing to
work on or no commands are configured. A failed `run()` still rolls back its
staged edits.

## Models

`nalar models` derives `<base_url without /chat/completions>/models`, calls it
with the provider key and extra headers, and returns sorted IDs. Failures are
reported as `http_*`, `request_timeout`, `transport_unavailable` or
`invalid_response` without echoing the response body. The TUI can list models
from the command menu.

## Review

`nalar code --review` optionally asks the model to review the final patch and
returns the findings in metadata; `--review-fail-on` gates the exit code.
`--review-model`, or `review_model` in `nalar.json`, reviews with a separate
model from the same provider while fixes continue on the primary model. The
same default applies to the TUI review action and the browser review button;
the SDK accepts `Agent(..., review_chat=...)` for an explicit reviewer.
`--review-fix-attempts N` (max 2) repairs findings at or above
`--review-fix-on` (default `high`), then re-checks the patch and reviews again.
Metadata reports `review_history` and the repair count; `--review-fail-on` is
applied to the final review.
Applying remains a separate, explicit step.

`nalar review --scope worktree --include-untracked` adds new text files Git
does not track: at most 20, each at most 32 KB, only safe (non-hidden,
non-secret) paths, within the 200 KB diff budget. `nalar review` sends a bounded
diff (up to 200 KB) to the model with no tools and expects a JSON array of
findings. Diff text conversion and external diff
drivers are disabled, so reviewing cannot execute repository-configured code.
The answer is validated strictly: any malformed item makes the whole result
`unparsed`, and `--fail-on` then exits non-zero rather than passing CI.
`--context FILE` (repeatable, max four) adds bounded user-supplied context to
the review prompt as untrusted data; the combined block is capped at 32 KB.
Transient provider errors are retried; permanent errors become a coded CLI
error. The command
never writes to the repository, which keeps review and implementation as
separate, auditable steps. `--fail-on high|medium|low` makes CI fail when a
finding at or above that severity exists.

## Plan mode

`nalar plan` is the explicit planning workflow; `ask` answers questions with
the same read-only belt.

`permission: plan` (project-level allowed) or `--plan` filters the belt to
read-only tools (plus `update_plan`) and appends a plan instruction to the
system prompt. No edit
tool is exposed and `replace`/`apply_patch`/`create_file`/`delete_file`/
`rename_file` dispatch fails as `unknown_tool`.
This is a workflow control, not a sandbox: an allowed MCP read tool still runs
server code.

## Subagents

`build_extensions(..., chat_factory=..., subagent_factory=...)` adds a
`delegate` tool when `subagents` is true (default). Its handler runs a nested
`AgentLoop` with a fresh read-only belt (`list_files`, `search`, `read_lines`,
`find_symbol`, `repo_map`, `git_log`, `git_show`, `use_skill`), a separate
system prompt, an 80k context and a 40k token budget. `subagent_model` selects
a separate model when the caller supplies `subagent_factory`;
`subagent_max_turns`, `subagent_max_calls` and `subagent_token_budget`
(defaults 6/8/40000) bound each delegation. The nested belt has no `delegate`, so recursion stops. A failed or
empty sub-agent becomes a structured tool error (`subagent_<status>`). The
main run's tool policy (`allow`/`deny`/`ask`) applies to `delegate` itself,
not inside the sub-agent, which is always read-only.

## Tool policy

```json
{"tools": {"allow": ["read_*", "docs__*"], "deny": ["docs__delete_*"]}}
```

The allow/deny globs are applied to the assembled tool belt, including MCP,
skill and plugin tools. Deny wins. Filtered tools are not included in the
provider request and their handlers are removed, so a hallucinated call fails
as `unknown_tool`. This is a usability and safety control, not a sandbox: MCP
servers and plugins still run with your account when their tools are allowed.

`tools.ask` adds per-call approval. The TUI opens a modal, `chat` prompts on the
terminal, `code` requires `--yes`, and the SDK accepts
`approve(name, arguments)`. A missing or negative decision becomes
`approval_required` and skips the rest of the batch. Decisions are per call and
never remembered.

## Task context

`nalar code|plan|ask --context FILE` (repeatable, up to four files, 32 KB
each) appends explicit user-supplied material to the task as data. Symlinks and oversized or
non-UTF-8 files are rejected. This is separate from repository instructions,
which come only from the committed snapshot.

Repository instructions are committed `NALAR.md`/`AGENTS.md` at the root plus
up to eight nested files matching those basenames (shallowest first, 16 KB each,
32 KB total). Nested guidance applies to its directory; root guidance wins on
conflicts, and all of it remains untrusted data that cannot grant permission.

## Applying a reviewed patch

`nalar apply` reads a unified diff (at most 10 MB, regular file), optionally
checks that HEAD matches a base prefix, and runs the same root, cleanliness,
Git-filter and lock checks as the interactive apply. `--check` validates
without writing. Keep the patch outside the checkout so it does not make the
tree dirty. `--verify` runs the configured checks in the real checkout after a
successful apply, with the same confirmation and secret-stripping rules as
`nalar verify`. `--include GLOB`/`--exclude GLOB` (repeatable) keep only the
matching file sections before applying; excludes win, sections are never split,
and an empty selection is an error. `--revert` runs the reverse patch, asks for confirmation (or
`--yes` in scripts), refuses staged changes and modified paths outside that
patch, and cannot be combined with `--verify`; `--check --revert` validates
without writing. Rename sections are allowed through both their pre- and
post-image paths.

## Pre-flight verification and repair

`nalar code --verify` exports the committed tree to a temporary directory,
applies the proposed patch there, and runs the configured `verify` commands
after printing them for confirmation (`--yes` in scripts). Provider keys and
common secret variables (`*_TOKEN`, `*_SECRET`, `AWS_*`, `SSH_AUTH_SOCK`, ...)
are removed; timeouts kill the whole process group. Executable bits from the
commit are preserved. Symlinks and special files are not extracted. With
`--fix-attempts N` (0–3), a failed check is fed back to the model, which may
produce a corrected patch; the loop shares the run's token budget and stops on
the first success or on a failed model turn; each attempt gets a fresh edit
allowance. The real checkout is never modified, and nothing is applied
automatically. The optional model review gate runs after the final patch, not
on a discarded attempt.

## Verification commands

`verify` lists argv arrays. They run only when the user invokes `nalar verify`
(or confirms in the TUI), never automatically after an apply. No shell is
involved, output is capped, the timeout is bounded, and the configured provider
key variable is stripped from the child environment. This keeps "the agent
cannot claim tests it did not run" true while letting you run the checks you
choose.

## Repository map

The system prompt starts with a compact map of top-level directories and root
files from the committed snapshot (bounded to 60 lines and 4 KB). It is
orientation only; `list_files` and `search` remain the source of truth. Large
trees are truncated with a count.

## Repository navigation tools

`repo_map` returns bounded file/language counts and the 15 files with the most
definitions (eight each, in source order), so an unfamiliar repository can be
oriented before paging through `list_files`. Counts cover readable tracked text
candidates. The symbol index filters to symbol-capable files before its 300-file
cap, reports `skipped_symbol_files` for unreadable or unparseable candidates,
`symbol_files`, and an exact `omitted_symbols` total; `truncated` is true when
any of those limits bind. `find_symbol` searches definitions by name: Python
files use `ast`, other
common languages use a line regex; results are capped at 50 and the index is
invalidated when a proposal stages an edit. `git_log` returns up to 20 recent
commits, optionally for one safe path. `git_show` returns one commit's message
and diff capped at 16 KB. `read_lines` adds `next_start` when more lines
exist; `search` reports how many files it scanned and why results were cut. All three are read-only and available in plan mode
and sub-agents.

## Prompt commands

`.nalar/commands/*.md` and any directories listed under `commands` become
user-invoked prompts. Names must match `[a-z0-9][a-z0-9-]{0,63}`; front matter
`description:` is optional (the first body line is the fallback). Bodies are
capped at 6 KB (the task limit) and `$ARGUMENTS` is substituted; when a
body has no placeholder, supplied arguments are appended as `Arguments: ...`. They are available in `chat`
and the TUI, listed by `nalar commands list`, and are guidance text only, so
they cannot alter tools, permissions or budgets. Symlinked, duplicate,
oversized or unsafe-named entries are ignored. Built-in slash commands win over a
same-named prompt command. `nalar commands init NAME` writes a starter
`.nalar/commands/NAME.md` with `$ARGUMENTS` and a description; existing files
are kept unless `--force` is passed.

## Shell completions

`nalar completions bash|zsh|fish` prints a script generated from the real
subcommand table. Bash: `source <(nalar completions bash)`. Zsh: save the
output as `_nalar` on `$fpath`, or source it after `compinit`; the script only
calls `compdef` when it exists and only dispatches when running as `_nalar`.
Scripts offer subcommand names and per-subcommand option flags, and fall back to
file completion for `apply` and `review`. A test keeps the flag table in sync
with `--help`.

`nalar code --patch-out FILE` saves the proposed diff atomically (refusing
symlinks) so it can be reviewed and applied later. Keep it outside the
checkout; a patch file inside the tree makes the working tree dirty.

## MCP resources and templates

`nalar mcp resources NAME [--read URI]` reports concrete resources and
`resources/templates/list` entries, and can print one resource's text for
diagnostics. During a run, the generated `server__read_resource` tool accepts a
listed URI or one matching an advertised RFC 6570 template (`{var}` matches one
path segment, `{+var}` matches the rest); anything else is refused before the
server is contacted. Templates and resources paginate with `nextCursor`.

## Harness behavior

- Transient provider failures (`transport_unavailable`, `request_timeout`,
  HTTP 5xx) are retried twice with backoff; permanent errors are not.
- When the context exceeds `context_budget`, the oldest tool results are
  replaced with a truncation marker, keeping the system prompt and recent
  turns intact. The count is reported as `compacted`.
- Streaming requests ask for usage accounting (`stream_options.include_usage`)
  so token counts stay available in the TUI and chat.
- CLI overrides `--max-turns`, `--max-calls`, `--token-budget` and `--retries`
  take precedence over config for that run; chat resets the token budget for
  each accepted task, as the TUI does.
- Streamed responses with `finish_reason` `length`/`content_filter` raise
  `incomplete_response` rather than being accepted.
- A completely empty completion (no text, no tool calls) is retried up to
  `retries` times before the run fails with `empty_response`; tokens from
  discarded empty replies still count against the budget.
- The pre-flight export becomes a throwaway Git repository, so Git-based
  checks work; infrastructure failures stop the repair loop without a model
  call, and a rejected patch includes its diff in the repair prompt.
- Repository reads are cached with a 50-entry/2 MB bound; staged edits bypass
  the cache.
- When a reply requests two or more read-only calls without an approval rule,
  they run concurrently (max four) in recovery mode. Results are appended in
  the model's call order, and a failed read does not cancel its siblings.
  Batches containing writes or approval-gated calls remain sequential.
- `token_budget` (0 = unlimited) stops the run with `token_limit` when usage
  exceeds it. When a provider omits usage, tokens are estimated from the payload
  and `tokens_estimated` is set. Sub-agent spend is charged to the parent budget.
  HTTP 429 joins timeouts and 5xx as retryable; `Retry-After` (seconds or
  HTTP-date, capped at 60 s) sets the delay. `retries` (0–5, default 2) bounds
  transient attempts. MCP servers accept `timeout` (1–600 s, default 15).
- `nalar code --events PATH` and `nalar run --events PATH` write `reply`, `tool`,
  `approval`, `retry` and `final` events as newline-delimited JSON (`0600`).
- Cancellation is cooperative and checked between turns and tool calls; a
  cancelled task discards its staged edits.
- A failed tool call marks the rest of that batch `skipped_after_error` so a
  model cannot write from input it never received.

## Sessions

Transcripts are stored per user in a private directory (`0700`, files `0600`)
and are capped at 200 entries and 4,000 characters each. Records contain the
task, the assistant's final text and the outcome status. Tool arguments, file
contents and credentials are not written. `NALAR_SESSIONS` moves the store,
for example to an encrypted volume. A session is bound to its repository
root; resuming it against another repository is refused.

## Staged patch tool

In addition to `replace`, the agent can call `apply_patch` with a unified diff
that edits one or more existing text files in one step. The diff is parsed with
the same section parser used by `nalar apply`; hunks are matched against the
committed snapshot (or earlier staged edits), applied bottom-up, and committed
to the in-memory proposal only when every file and hunk succeeds. Creating,
deleting, renaming, binary and mode-changing sections are refused; new files
use `create_file`, deletions use `delete_file` and renames use `rename_file`.
The staged patch preserves the tracked executable bit. The 12-file edit limit,
the 96 KiB per-file limit and the read-only plan policy still apply.
