# Architecture

```
CLI (__main__)  TUI (tui)  Web (web)  SDK (sdk)
        \           |          |        /
         +---------- agent.AgentLoop ----------+
         |               |                     |
   tools.ToolBelt   extensions.build      store.SessionStore
         |               |                     |
 repository tools   skills / MCP / plugins   transcripts (JSON)
         |               |                     |
   coding.Repository  mcp.MCPProcess      config.load_config
                      mcp.MCPHttp          subagent (read-only belt)
         |
   Git plumbing (read-only objects, patch apply)
         |
   provider client (go.ChatCompletions): OpenAI-compatible HTTP + SSE
```

## Invariants

1. The model acts only through `ToolBelt` tools. Unknown names and malformed
   arguments become structured `ToolError` results.
2. The first failed call in a batch marks the rest `skipped_after_error`; a
   model cannot write from data it never received.
3. Repository edits live in memory (`Repository.changed`) until an explicit,
   verified apply. Failed and cancelled runs roll back staged edits.
4. Provider failures are classified: transient ones retry with backoff unless
   output was already streamed; permanent ones surface a content-free code.
5. Configuration is validated strictly; secrets come from the environment.
   Prompt commands are guidance text and cannot change tools or policy.
6. Extensions cannot weaken the write policy: skills are prompt data, MCP and
   plugins add tools but apply still requires human approval.

## Modules

| Module | Responsibility |
| --- | --- |
| `agent.py` | turn loop, parallel read batches, budgets, compaction, retries, cancellation, events |
| `core.py` | provider-independent types, `run()` for workspace tools |
| `tools.py` | tool registry, policy filters, approval checks, recovery hints |
| `coding.py` | Git snapshot reader, edit staging, patch export, safe apply |
| `apply.py` | unified-diff parsing/filtering, atomic patch output |
| `preflight.py` | throwaway export, patch check and configured verification |
| `verify.py` | bounded verification commands with process-group cleanup |
| `atomic.py` | durable, interrupt-safe file writes shared by config/sessions/init |
| `extensions.py` | assembles repository + skill + MCP + plugin tools |
| `config.py` | layered, strict configuration and provider settings |
| `go.py` | streaming/non-streaming OpenAI-compatible client |
| `mcp.py` | stdio JSON-RPC client with timeouts and env isolation |
| `mcpserver.py` | read-only MCP stdio server for the committed snapshot |
| `plugins.py` | user-level Python tool modules |
| `skills.py` | `SKILL.md` discovery and validation |
| `review.py` | read-only diff review with validated findings |
| `secrets.py` | bounded credential scanning for patches, committed blobs and SARIF |
| `fix.py` | verification-driven repair loop (stages a proposal, never applies) |
| `store.py` | private session transcripts, cumulative totals and incremental index |
| `tui.py` | full-screen terminal workspace |
| `web.py` | loopback single-user web workspace |
| `webcode.py` | coding web workspace: apply/revert/verify/review/repair |
| `commands.py` | `.nalar/commands` prompt templates with `$ARGUMENTS` |
| `hunks.py` | split and filter unified diffs for per-hunk review |
| `completions.py` | bash/zsh/fish completion scripts kept in sync with the CLI |
| `session.py` | interactive chat REPL and slash commands |
| `context.py` | attached-context file loading |
| `instructions.py` | project instruction discovery and prompt block |
| `workspace.py` | scoped flat-directory read/edit tools |
| `manage.py` | validated user-settings and MCP server edits |
| `init.py` | starter `nalar.json` and review skill without clobbering work |
| `doctor.py` | environment, provider, TUI and optional MCP diagnostics |
| `benchmark.py` | workspace artifact benchmark |
| `code_benchmark.py` | paired patch benchmark on synthetic Git fixtures |
| `sdk.py` | embedding API with multi-turn conversations |

## Flow (coding task)

1. Load config; open the repository at the requested commit.
2. Discover skills, start enabled MCP servers, load user plugins.
3. Stream a request to the provider with the tool schemas.
4. Execute tool calls against the Git snapshot; independent read-only calls in
   one turn run concurrently, writes and approval-gated calls stay ordered.
5. On a final answer, export a unified patch. With `--verify`, apply it to a
   temporary export and run configured checks; optionally feed failures back
   for a bounded repair. `--review` adds read-only findings, and
   `--review-fix-attempts` can repair blocking findings before re-reviewing.
   Nothing is written to the checkout.
6. Human reviews the diff (the TUI can apply only selected hunks); apply
   verifies base, cleanliness and include/exclude filters, then `git apply`.
   `apply --revert` reverses an applied patch after confirmation. The user runs
   project verification and commits.

Review and implementation are separate commands by design: a review cannot
edit, and an implementation cannot claim tests it did not run.
