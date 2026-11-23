# Status

Nalar is a local-first, patch-first coding agent: CLI, Textual TUI, browser
workspace, read-only MCP server and Python SDK.

## Current release

**0.7.281** — Apache-2.0, self-hosted, BYOK through any OpenAI-compatible
endpoint.

## Verified

- `python -m unittest discover -s tests -q`: **1006 tests, OK**.
- `ruff check --select E9,F63,F7,F82,F601 nalar tests`: clean.
- Full suite in CI on Linux Python 3.12 and 3.13.
- Compatibility smoke in CI on Ubuntu, macOS and Windows, Python 3.12 and 3.13.
- Wheel install smoke: version, Apache-2.0 metadata, bundled `LICENSE`/`NOTICE`,
  no bytecode caches, every subcommand `--help`.
- Local provider wire matrix: three profiles, text/tools/SSE/models/fallback/
  retries/auth/timeout/session headers.

## Supported surfaces

- **Runtime:** turn/call/token budgets, retries with `Retry-After`, cancellation,
  context compaction, parallel read tools, repair loops, subagents, approvals,
  NDJSON events.
- **Coding:** read/search/repo-map/git tools, replace, multi-file patches,
  create/delete/rename/chmod, staged diff.
- **Review/repair:** model review, separate reviewer model, `fix` with bounded
  attempts, configured verification.
- **Sessions:** private transcripts, search, stats, export, fork, prune,
  incremental index, per-model token totals.
- **Extensions:** MCP stdio/HTTP clients, skills, prompt commands, trusted
  plugins, read-only MCP server.
- **Interfaces:** CLI, TUI, browser workspace, SDK.

## Boundaries

- Full-suite CI is Linux; macOS and Windows run the compatibility smoke.
- The provider wire matrix uses local stubs. Paid/cross-provider model quality is
  not claimed.
- Same-session appends are single-writer.
- Verification commands run only when explicitly requested.
- Credential scanning is a guardrail, not a complete secret scanner.
- Published documentation describes the current release; historical review
  notes are not part of the public docs.
