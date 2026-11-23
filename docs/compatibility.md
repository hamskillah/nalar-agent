# Platform compatibility

Full verification is currently Linux x86_64 with Python 3.12. The paid/cross-
provider matrix and Windows/macOS runs still need an owner decision or a CI
environment; they are not claimed as verified.

## Local smoke

Run this on any supported OS from the repository root:

```bash
python scripts/compat-smoke.py
```

It uses only the standard library and local stubs:

- imports the package and prints the version;
- creates/lists/searches/stats/prunes a private session store;
- runs `nalar --version` and `nalar --help`;
- runs the 12-case provider wire matrix with no real network or provider key.

## CI

`.github/workflows/compat.yml` runs that smoke on Ubuntu, macOS and Windows
with Python 3.12 and 3.13. It is intentionally narrower than
`scripts/release-check.sh` (the full suite, wheel install and `--help` matrix)
so it can run on every platform. A green CI run is the evidence needed to
replace the "unverified" note; a local Linux run is not evidence for macOS or
Windows.

## Current status

| Platform | Status |
| --- | --- |
| Linux x86_64, Python 3.12 | Full suite, lint, wheel and live free-model ask verified |
| macOS | Compatibility smoke passed on 0.7.279 (run 45130575182) |
| Windows | Compatibility smoke passed on 0.7.279 (run 45130575182) |
| Python 3.13 | Full suite passed in CI run 45130575237; compatibility smoke also passed |
