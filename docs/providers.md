# Providers and BYOK

Nalar never ships or stores a provider key. It reads the key named by
`provider.api_key_env` from the process environment at request time. There is no
telemetry and no shared key.

## Any OpenAI-compatible endpoint

Nalar is self-hosted and BYOK: you choose and pay the provider. The provider
must expose OpenAI-style `POST /chat/completions` and a compatible `/models`
endpoint. Nalar defaults to a local OpenAI-compatible server at
`http://127.0.0.1:11434/v1/chat/completions`. Set `provider.base_url` for a
hosted gateway:

```json
{
  "model": "vendor/model",
  "provider": {
    "base_url": "https://gateway.example.com/v1/chat/completions",
    "api_key_env": "PROVIDER_API_KEY",
    "headers": {"X-Organization": "{env:ORG_ID}"},
    "session_header": true,
    "max_tokens": 2048,
    "temperature": 0.0,
    "timeout": 60
  },
  "fallback_models": ["vendor/backup-model"],
  "retries": 2
}
```

```bash
export PROVIDER_API_KEY="..."
nalar code --repo . "Fix the failing test"
```

A missing key is a configuration error, not a silent fallback: CLI commands exit
2 with the variable name, and the TUI opens read-only.

## Configuration rules

Provider settings belong in the user config (`~/.config/nalar/config.json`). A
repository's `nalar.json` may set them only with `--trust-project`, because a
cloned repository must not redirect requests or receive prompts meant for your
provider.

- `base_url`: full chat-completions URL. `https` anywhere; plain `http` only for
  `127.0.0.1`, `localhost` or `::1`. Credentials, fragments, control characters,
  non-ASCII targets and invalid ports are rejected.
- `api_key_env`: an environment variable name, never the key itself.
- `headers`: at most eight headers; transport headers and control characters are
  refused; values are Latin-1 and support `{env:NAME}`.
- `session_header`: `true` sends a rotating `x-nalar-session` header; a string
  such as `"x-provider-session"` uses that header name; `false` disables it.
- `max_tokens` (1–32768), `temperature` (0–2), `timeout` (1–600 s).
- `fallback_models`: tried only when the provider reports the model as missing
  (`http_404`). Auth, rate-limit and transport errors are surfaced.
- `retries`: transient-error retries (0–5); `Retry-After` is honored and capped.

## Local gateways

Local endpoints are supported when they speak the same protocol:

```json
{
  "provider": {
    "base_url": "http://127.0.0.1:11434/v1/chat/completions",
    "api_key_env": "LOCAL_KEY"
  }
}
```

Use a placeholder key if the local server ignores authentication. See
[provider matrix](provider-matrix.md) for the stub-tested compatibility profile.
