# Browser workspace

`nalar serve` is a single-user, loopback-only HTTP interface. It never binds a
public address; Host, Origin, CSRF, request-size and content-type checks are
enforced before a task runs.

## Generic workspace

```bash
nalar serve --state ~/.local/share/nalar
```

Each task gets a private flat directory. The UI can attach one small text file,
list/read/create/search/replace/delete/rename workspace files, inspect the tool
trace, and download produced files. These edit tools are destructive to the
selected workspace, so use a dedicated directory.

## Coding repository

```bash
nalar serve --repo /path/to/repo
```

Runs use the committed Git snapshot and the same patch-first harness as the
CLI. A run produces a proposed diff; nothing is written to the checkout until
the explicit **Terapkan patch** (Apply patch) action. Apply re-checks the base
commit and a clean worktree, exactly like `nalar apply`.

Optional actions on a proposal:

- **Unduh patch** downloads the unified diff.
- **Review patch** asks the model for structured findings.
- **Jalankan verifikasi** lists and runs the configured verification commands
  on a temporary snapshot with the applied patch; results are shown on the
  page and the checkout is not polluted with test artifacts.
- **Batalkan dan usulkan perbaikan** appears after a failed applied
  verification. It reverts the applied patch, runs the verification-driven fix
  loop on the committed base, and stages the new proposal for review; it never
  applies it automatically. If the repair cannot start or proposes nothing, the
  previous applied patch is restored when possible.

The recent-run list on the home page has a `?q=` filter over prompts and model
names. Matching is literal and case-insensitive, the query is capped at 100
characters and HTML-escaped, and an empty result shows the query rather than
every run.

The model key stays in the server process environment. History and both
databases live under `--state` (default `~/.local/share/nalar`). Do not expose
the server through a public bind, proxy or tunnel.
