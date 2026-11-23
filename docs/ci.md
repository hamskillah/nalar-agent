# CI and pipelines

Nalar never writes a checkout unless you run `apply` without `--check`. Patch
input can come from a file or stdin (`-`), and the readers are bounded: 10 MB
for `apply`, 200 KB for `review` and 2 MB for `scan`.

```bash
# Scan a pull-request patch without applying it.
git diff origin/main...HEAD > pr.diff
nalar scan --file pr.diff --format sarif > nalar.sarif
nalar scan --file - < pr.diff --format markdown
nalar scan --file pr.diff --format github   # inline annotations, no SARIF upload

# Ask a model to review the same patch; fail CI on high findings.
nalar review --file pr.diff --format markdown --fail-on high
nalar review --file pr.diff --format github --fail-on high

# Validate a patch from a pipeline before applying it deliberately.
nalar apply - --check --base "$BASE_SHA" < pr.diff

# Produce a proposal and split the patch from metadata.
nalar code --repo . --model "$MODEL" --json 'Fix the failing test' \
  | tee proposal.json | jq -r .patch > proposal.diff
```

`--format github` prints GitHub Actions workflow annotations (`::error`/`::warning`
with escaped file, line and title) instead of SARIF; scan annotations never
include a credential value, and an incomplete scan adds a warning.

Exit codes: `scan` returns 0 clean, 1 findings, 2 errors or an incomplete scan;
`review` returns 0 clean/unthresholded, 1 findings at `--fail-on`, 2 an
unparseable review when `--fail-on` is set; `code` returns 0 when a patch was
produced, 1 when it was not or a requested gate failed. `code --json` includes
`status`, `base`, `model`, `patch`, token/turn/call counts and any requested
verification/review metadata; the patch is never written to the checkout.

Provider credentials are never required for `scan` or `apply`. `review` and
`code` use the configured provider key, and temporary verification strips it
from the child environment.
