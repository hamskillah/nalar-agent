# Security model

Nalar is a local, single-user coding agent. It treats the repository, model
output, MCP servers, plugins and browser input as separate trust boundaries. It
is not a sandbox: explicit user actions still run code and write files.

## Trust boundaries

### Repository content is untrusted

Committed text is data, not instructions. The model reads it through bounded
tools; `NALAR.md`, `AGENTS.md`, skills and MCP guidance cannot grant
permissions, change budgets or waive review.

- Edits are staged in memory against the committed snapshot. The checkout is
  unchanged until an explicit apply.
- Tool paths are repository-relative and validated; absolute paths, `..`,
  `.git`, symlinks, hardlinks and binary/oversized blobs are rejected or
  skipped as appropriate.
- A project `nalar.json` may set safe limits, but `provider`, `mcp`, `tools`,
  `subagents`, `token_budget`, `fallback_models` and `plugins` require
  `--trust-project`. `permission: "plan"` is the only privileged key accepted
  without trust because it removes capabilities.

### Model output is untrusted

Tool calls are schema-validated, argument-validated and dispatched through a
policy belt. Malformed calls become bounded errors; repeated identical failures
are refused. Several read-only calls may run in parallel; writes and
approval-gated calls stay sequential.

- `tools.allow`/`deny`/`ask` globs are applied to built-in, MCP, skill and
  plugin tools. Denied tools are absent from the request and rejected at
  dispatch.
- Terminal approval prompts show the tool name and bounded arguments; `--yes`
  approves listed tools for scripts, and non-interactive runs without an
  approver fail closed.
- The model never edits the checkout directly and never commits.

### Apply is a guarded, explicit operation

`nalar apply`, TUI apply, web apply and SDK `apply()` share one implementation:

- requires the Git top-level, the reviewed base and an unchanged `HEAD`;
- requires a clean worktree unless reversing a known patch;
- refuses configured Git filters that could execute code;
- uses a per-worktree lock, `git apply --check`, then `git apply`;
- applies only whole file sections selected by include/exclude or hunks;
- scans added lines for credentials first and refuses unless the caller passes
  an explicit override;
- records the last successful patch under `.git` (0600, best effort) for undo;
- never commits or pushes.

### Verification runs code by design

Verification commands are user-configured argv arrays, never shell strings.
They run:

- on a temporary export of the committed snapshot for `code --verify`,
  `fix`, and review-time checks; the real checkout is untouched;
- with provider credentials and common secret variables removed from the
  environment;
- with a timeout that kills the whole process group and bounded output;
- only after an explicit `--yes` or an interactive confirmation.

The web apply option runs configured checks in the real checkout after an
explicit apply and confirmation; the commands are shown before opt-in.

### Credential handling

- Keys are read from the environment variable named by `provider.api_key_env`.
  They are never stored in configuration or written to transcripts.
- Stdio MCP servers and verification children do not inherit the provider key
  or common secret variables.
- `nalar scan` and `apply` detect high-confidence credentials in added lines
  or the committed tree (private keys, cloud/provider tokens, password-style
  assignments, credential-bearing database URLs). Findings contain file, line
  and kind only—never the value.
- The scanner is bounded (2 MB diff, file/byte caps, capped findings) and
  reports incomplete scans. It is a guardrail, not a complete secret scanner.
- `--allow-secrets` (or the web confirmation checkbox, TUI dialog, SDK
  `allow_secrets=True`) is the only way to proceed after a finding.

### MCP

Client:

- stdio children get a minimal environment, not provider credentials;
- HTTP endpoints require TLS except loopback; redirects are not followed;
- request bodies, tool results and timeouts are bounded; tool lists are capped;
- project MCP configuration requires `--trust-project`.

Server (`mcp-serve`):

- read-only by construction: no write/apply tools, no shell, no provider key,
  no project-config loading;
- reads Git objects only, never the working tree;
- validates paths, caps lines/results, drains oversized requests, ignores blank
  transport lines and shuts down cleanly on a closed stdout;
- exposes committed instructions/skills as resources and commands as prompts,
  labelled as untrusted data;
- negotiates supported MCP versions and serves batches only for pre-2025-06-18
  sessions.

### Browser workspace

- binds IPv4 loopback only; `Host`, `Origin`, form content type and CSRF are
  checked before any action;
- no cookies, no CORS, `nosniff`, CSP and `no-store` headers;
- request bodies, forms, patch sizes and model prompts are bounded;
- runs and state are scoped to the served repository; apply re-checks base and
  worktree; a moved base disables apply and offers repair;
- credential findings require a separate confirmation checkbox;
- the key stays in the server process environment and is never sent to the
  browser.

### Plugins, skills and commands

- Plugins execute arbitrary Python and therefore load only from user-level
  configuration/directories, never from a cloned repository.
- Skills, commands, project instructions and MCP prompts are untrusted guidance
  data; they cannot change tools, budgets, approval or review policy.
- `skills check`, `commands check` and `plugins check` surface rejected files
  and warning states for authors and CI.

### State and transcripts

Sessions and coding-web state live in a private `0700` directory (default
`~/.local/share/nalar`, or the directory served by `--state`). Transcripts
contain prompts and model responses. Do not expose the state directory, web
server or TUI through a public bind, proxy or tunnel; use synthetic data when
sharing transcripts.

## Known limits

- Nalar is not a sandbox for untrusted users, repositories or plugins. Running a
  configured verification command executes project code.
- Secret detection has false positives and false negatives; review the findings.
- Model reviews and fixes can be wrong; the patch-first gate is the control.
- Linux is the tested platform; other systems are unverified.
- The read-only MCP server intentionally cannot modify a repository.

See [providers](providers.md), [extensions](extensions.md),
[mcp-server](mcp-server.md) and [browser](browser.md) for details.
