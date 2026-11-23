# Runtime

Nalar's runtime is deliberately small: one agent loop, a typed tool belt, a
provider adapter, and a private session recorder. Interfaces (CLI, TUI,
browser, SDK) feed the same loop.

## Request lifecycle

1. **Resolve config.** User config and an optional project `nalar.json` are
   merged. Provider/MCP/plugin settings from a project need `--trust-project`.
2. **Snapshot.** The repository is resolved at a commit; coding turns work on an
   in-memory snapshot, never on the dirty checkout.
3. **Build tools.** Built-in coding tools, skills, prompt commands, plugins and
   MCP tools are loaded into a `ToolBelt`. MCP/plugin failures become warnings,
   not silent capability changes.
4. **Run the loop.** `AgentLoop` sends messages + tool schemas to an
   OpenAI-compatible endpoint. Tool calls are validated, executed, and appended
   as tool results. The loop stops on a final answer, budget, cancellation,
   repeated failure or provider error.
5. **Record.** If enabled, a private session summary is written: task, outcome,
   turns/calls, per-model token totals and patch hashes — never patch bytes,
   provider prompts or keys.

## Budgets and recovery

- Turn, tool-call and token budgets are enforced per run. The shared `Budget`
  also includes delegated subagent spend.
- Transient provider errors are retried with backoff. `Retry-After` is honored
  and capped. Auth/404/model-missing errors are not hidden behind retries.
- Empty completions and `length`/`content_filter` finishes are treated as
  failures, not successful answers.
- Context compaction replaces old tool output with bounded notices; repeated
  identical failures stop the run rather than looping.
- `Esc`/cancel sets a cancellation token; the loop stops, discards partial
  edits, and keeps the previous proposal.

## Tools and approvals

Read-only tools (`list_files`, `search`, `read_lines`, `find_symbol`,
`repo_map`, `git_log`, `git_show`, `use_skill`) may run in parallel. Writes and
approval-gated tools stay sequential and ordered. Tool arguments are validated
against strict path and UTF-8 rules; repository content is untrusted data and
cannot change policy.

Applying a patch is outside the loop: `nalar apply` validates base, clean tree,
patch file, credentials, Git filters and the per-worktree lock. The agent never
commits.

## Sessions and index

`SessionStore` keeps one JSON transcript per session under a `0700` directory,
mode `0600`. A private `index.json` caches summaries keyed by
`mtime_ns`/`ctime_ns`/inode/size plus a summary digest, so list/stats/prune parse
only changed files. Cumulative token totals survive the 200-entry transcript
window. Deleting the index is safe; it is rebuilt from transcripts.

Same-session appends are single-writer by contract. Scans and index replacement
are concurrency-safe.

## Extensions

- **Skills:** markdown instructions discovered from user/project paths; exposed
  through `use_skill`.
- **Prompt commands:** templates under `.nalar/commands`; rendered into a task.
- **Plugins:** trusted user plugins register tools; conflicting names are
  rejected.
- **MCP clients:** stdio and loopback/HTTPS HTTP; tools/resources/prompts,
  deadlines, framing limits and `--print-config`.
- **MCP server:** `nalar mcp-serve` exposes read-only tools, resources and
  prompts.

Extension text is untrusted. It can guide the model but cannot change
permissions, review policy or enable auto-apply.

## Failure semantics

| Failure | Result |
| --- | --- |
| Provider 429/5xx | bounded retry; then typed `provider_error` |
| Empty/truncated completion | retried within budget, then error |
| Tool error | returned to the model as a tool result for recovery |
| Budget exhausted | `token_limit`/`turns`/`calls` status, no silent success |
| Cancellation | `cancelled`, partial edits discarded |
| Verification failure | `failed`; `fix` can propose a bounded repair |
| Review parse failure | `unparsed`, raw text kept, not “no findings” |
| Session write failure | run still returns; `record_error` reported |

## Testing

- 1006 unit/integration tests cover the loop, tools, apply/revert, sessions,
  MCP framing, provider wire behavior, TUI and SDK.
- `tests/test_provider_matrix.py` runs a local OpenAI-compatible stub across
  three profiles: text, tools, SSE, models, 404 fallback, 429/500 retries,
  auth, timeout and session headers.
- `scripts/compat-smoke.py` runs on Linux/macOS/Windows; CI additionally runs
  the full suite on Linux 3.12/3.13.

See [architecture](architecture.md) for module boundaries and
[security](security.md) for the trust model.
