# Security model

Nalar runs with the user's own credentials on the user's machine. The model is
untrusted: repository text, skill bodies, MCP responses and model output are
all treated as data, never as instructions that can widen permissions.

## Trust boundaries

| Component | Trust | Control |
| --- | --- | --- |
| Model output | untrusted | only validated tool calls are executed |
| Repository content | untrusted | read-only Git blobs; bounded size; no execution |
| Skills (`SKILL.md`) | untrusted guidance | listed in the prompt, loaded on demand, cannot grant permissions |
| Project instructions | untrusted, committed only | read from safe tracked blobs at the reviewed commit; never from the working tree |
| MCP servers | user-configured, executable | started only from config, minimal environment or TLS endpoint, timeouts and output caps |
| Python plugins | user-configured, executable | loaded only from user config, never from a repository |
| Provider | configured by user | HTTPS (or loopback HTTP), key from environment |
| Web UI | loopback, single user | host/origin checks, CSP, HTML escaping, no key exposure |

## Project configuration

A repository's `nalar.json` is untrusted. It may set a model, turn/call
budgets, skills, verification commands and instruction file names (resolved
only against safe tracked blobs). It may **not** set `permission` (except the
read-only `plan`), `plugins`, `mcp`, `provider`, `tools`, `subagents` or
`token_budget` unless the user passes `--trust-project` (or
`NALAR_TRUST_PROJECT=1`). This prevents a cloned repository from starting
processes, disabling review or approval, widening budgets, or sending the
provider key to another host.

Verification commands from a repository only run after the user sees them and
confirms: `nalar verify` prompts, `code --verify` prints the commands and
requires `--yes` in scripts, and the TUI asks in a modal. Provider keys and
common secret variables are removed from the child environment, and a timeout
kills the process group; a lingering child outside the group is cut off after a
short cleanup window so verification cannot hang forever.

## Repository writes

- `nalar code` and `nalar tui` read committed Git objects. The working tree,
  index and untracked files are untouched until apply.
- Apply is explicit, previewed per file, and only runs when the repository
  root matches, `HEAD` equals the reviewed base, the working tree is clean,
  and no Git clean/smudge filters are configured (they could execute).
- Paths are restricted to safe ASCII components inside the snapshot; symlinks,
  hard links, FIFOs, submodules, hidden components and common secret filenames
  are excluded from read candidates and from edits.
- A rejected or cancelled run discards its partial edits.
- `nalar init` refuses to write its configuration or example skill through a
  symlink, so a cloned repository cannot redirect those writes.
- Nalar never commits, pushes, force-updates or deletes branches.

## Credentials and data

- Keys are read from the environment variable named in `provider.api_key_env`.
  They are never written to config, sessions, traces or prompts.
- MCP child processes receive only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
  `TZ` plus their configured `env`. Provider keys are not inherited.
- MCP HTTP endpoints must be `https://` (plain `http://` only for loopback),
  may not embed credentials or fragments, do not follow redirects, and receive
  only the configured headers. Each request has an absolute 15 s deadline
  (including slow-drip and open SSE streams), responses are capped at 4 MB, and
  multi-line SSE `data:` events are joined before parsing.
- Session transcripts (`0700` directory, `0600` files) contain tasks, final
  text and outcomes — not keys or file contents. `NALAR_SESSIONS` can point at
  an encrypted volume.
- The configured provider receives prompts, the diffs or files the model
  reads, and tool results. No telemetry is sent anywhere else.
- The local web UI binds `127.0.0.1` only and must not be exposed through a
  proxy or tunnel. It is single-user and has no authentication.

## Terminal and browser output

Model text is never trusted markup. The TUI strips control characters before
display, the diff viewer neutralises terminal escapes, the web UI escapes HTML
and serves a strict Content-Security-Policy, and downloads are sent as
attachments with `nosniff`.

## Limits

- Agent loop: bounded turns and tool calls; oversized context compacts the
  oldest tool results; no automatic shell or network tools.
- MCP: 15 s request timeout, 1 MB line cap, 32 KB result cap, 128 tools and 10
  list pages.
- Provider: 512 KB non-streamed / 4 MB streamed response cap.
- Diff review: 200 KB and 50 findings.
- Sessions: 200 entries, 4,000 characters each, 1 MB file.

## Known gaps

- No sandbox: MCP servers and plugins run with the user's privileges. Only
  enable sources you trust.
- Git filters are detected at apply time; review disables textconv and
  external diff drivers. Nalar disables fsmonitor for its own Git calls.
- Apply takes a per-worktree lock before the base, cleanliness and filter
  checks, so two Nalar sessions cannot apply at once. External writers can
  still race it; do not edit the tree while approving.
- A sub-agent is read-only by construction: it receives no edit, apply, shell
  or delegate tool, and its turns, calls and token budget are bounded. It still
  consumes provider tokens.
- Retry delays from `Retry-After` (seconds or HTTP-date) are capped at 60 s in
  both the provider client and the loop.
- Tool policy (`tools.allow` / `tools.deny`) limits which tools are exposed
  and dispatchable; it does not sandbox an allowed MCP server or plugin.
  `tools.ask` requires an explicit per-call decision and fails closed when no
  approver is available.
- Installed Python packages can add tools through the `nalar.tools` entry point
  group. They are code you installed; a repository cannot register one.
- The web UI has no multi-user authentication and is not intended for public
  deployment.
- Live provider evaluation and platform verification are still pending; see
  `docs/release-checklist.md`.
