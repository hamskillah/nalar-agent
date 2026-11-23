# Local provider matrix

The paid/cross-provider evaluation still needs owner approval for credentials and
spend. Until then, the provider boundary is exercised with a local
OpenAI-compatible stub and the recorded profiles in
`tests/fixtures/providers/matrix.json`.

## Profiles

| Profile | Base URL | Model | Session header | Extra header |
| --- | --- | --- | --- | --- |
| `hosted` | `https://api.example.com/.../chat/completions` | `vendor/model` | yes | none |
| `gateway` | `https://gateway.example/v1/chat/completions` | `@cf/meta/llama:free` | no | `X-Organization: team` |
| `loopback` | `http://127.0.0.1:{port}/v1/chat/completions` | `local/model` | no | none |

`tests/test_provider_matrix.py` runs every profile against a loopback HTTP
server and patches only the HTTPS transport, so the real request construction
is exercised.

## Scenarios

- plain text completion and request shape (`model`, `Authorization`, custom
  headers, session header);
- tool-call decoding;
- SSE streaming text, tool arguments and usage;
- `/models` discovery with headers and an explicit per-profile path;
- fallback to the next model only on `http_404`;
- `429` with `Retry-After`, `500` retries and negative SSE usage through `AgentLoop`;
- `401` is not retried;
- invalid JSON maps to `invalid_response`;
- socket timeout maps to `request_timeout`;
- session headers rotate per conversation.

## Run

```bash
python -m unittest tests.test_provider_matrix -v
```

The suite is also part of the normal `unittest discover` run.

## Limits

This is a wire/contract matrix, not a model-quality matrix. It does not measure
real provider latency, rate limits, billing, quota behavior, model tool-use
quality, or non-Linux networking. Those remain in the paid/cross-provider and
OS-matrix blockers. Recorded scenarios are deterministic; add a new response to
`tests/fixtures/providers/matrix.json` and a case to the test file when a real
provider exposes a new response shape.
