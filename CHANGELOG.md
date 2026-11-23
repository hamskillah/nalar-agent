# Changelog

## 0.7.281

- Public-facing cleanup: provider branding is generic, the default API-key
  environment variable is `NALAR_API_KEY`, and internal process/review
  documents were removed from the public tree. The bundled default endpoint is
  now a local OpenAI-compatible server at `127.0.0.1:11434`; hosted gateways are
  configured explicitly.

## 0.7.280

- New public README and banner, runtime and architecture documentation, issue
  and pull-request templates, and GitHub topics/description. Detailed command
  reference consolidated into the README and docs.

## 0.7.272–0.7.279

- Cross-platform CI hardening: Windows atomic writes, session-directory checks,
  temp-path handling, deterministic compatibility smoke, and Python 3.13 test
  portability. CI now covers Linux 3.12/3.13 and a compatibility smoke on
  Ubuntu, macOS and Windows.

## 0.7.270–0.7.271

- Single-line hardening for diagnostics, apply/verify/chat argv and MCP
  metadata. Recorded a small paid-model smoke (ask and code) on the default
  provider.

## 0.7.260–0.7.269

- TUI/MCP hardening: prompt content and metadata, tool errors, picker markup,
  URL/port validation, modal task gates, and review/verify output escaping.
  `doctor --mcp` now marks requested MCP checks as required.

## 0.7.250–0.7.259

- Session index and cumulative totals, threaded TUI scans, truthful prune
  accounting, UTF-8/TOCTOU handling, provider and base-URL validation, and
  review/apply line hardening.

## 0.7.248–0.7.249

- Apache-2.0 licensing, filtered session model labels, local provider wire
  matrix, and the first recorded live free-model smoke (ask, code, fix,
  apply-check, code-bench).

## 0.7.240–0.7.247

- Review/repair attribution, SDK review recording, per-model usage, session
  model filters, token accounting on failures, and the initial public-release
  documentation.

## Earlier

- CLI/TUI/browser/SDK interfaces, local coding tools, patch apply/revert,
  credential scanning, MCP/skills/plugins, sessions, config trust gates and the
  test suite.
