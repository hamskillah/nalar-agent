# Contributing

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .          # installs the Textual dependency
python -m unittest discover -s tests -v
```

Python 3.12+ and Git are required. No network access is needed for the test
suite: provider and MCP fixtures bind loopback servers, and no real API key is
used. `NALAR_API_KEY` must never appear in tests, logs or fixtures.

## Before sending a change

- Run the full suite: `python -m unittest discover -s tests -v`.
- Run `python -m compileall -q nalar tests`.
- Run `ruff check --select E9,F63,F7,F82,F601 nalar tests` (installed with `pip install -e '.[dev]'`).
- Keep the security boundaries intact. In particular: no new way for a
  repository to start processes or silence review without `--trust-project`;
  no path that applies a patch without the base/clean/filter checks and the
  apply lock; no tool that widens the model's permissions.
- Add a regression test that fails without the change.
- Provider calls must be bounded (timeout, response caps) and errors must not
  echo secrets or bodies.

## Extending

- **Skills**: add `.nalar/skills/<name>/SKILL.md` with `name` and
  `description` frontmatter. Treat them as untrusted guidance.
- **MCP**: stdio (`command`) or HTTP (`url`, `https` or loopback). Validate
  inputs in the client, not only in the server.
- **Plugins**: user-config only, either a module with `tools()` or a
  `nalar.tools` entry point. Handlers must be bounded and raise ordinary
  exceptions; `ToolBelt` converts them into structured errors.
- **Providers**: keep the OpenAI-compatible request/streaming contract and
  content-free `ProviderError` codes.

## Style

Standard library first; add a dependency only with a clear reason. Keep modules
focused, use `ToolError` for model-visible failures and `ValueError`-derived
errors for user-facing configuration problems. Log metadata, never prompts,
file contents or credentials.

## License of contributions

Nalar is Apache-2.0. By submitting a contribution you agree that it is
licensed under the same terms and that you have the right to submit it.

## Reporting security issues

Do not open a public issue with exploit details. Describe the impact and a
minimal reproduction privately to the maintainer, and rotate any exposed key.
