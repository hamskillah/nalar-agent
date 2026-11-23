# Read-only MCP server

`nalar mcp-serve --repo PATH` exposes the committed Git snapshot of one
repository to another agent over MCP (stdio transport). It is deliberately
read-only: it never edits files, applies patches, runs commands, or reads the
working tree, so it needs no provider key and no project trust.

```bash
nalar mcp-serve --repo /path/to/repo --print-config
```

prints a client configuration snippet:

```json
{
  "mcpServers": {
    "nalar": {
      "command": "nalar",
      "args": ["mcp-serve", "--repo", "/path/to/repo"]
    }
  }
}
```

Paste it into the MCP configuration of the client that should consume the
repository. Register one server entry per repository you want to expose.

## HTTP transport

For clients that prefer HTTP, start the same read-only server on loopback:

```bash
nalar mcp-serve --repo /path/to/repo --http --port 8766
nalar mcp-serve --repo /path/to/repo --http --port 8766 --print-config
export NALAR_MCP_TOKEN='a-long-random-token'
nalar mcp-serve --repo /path/to/repo --http --port 8766 \
  --token-env NALAR_MCP_TOKEN
```

```json
{
  "mcpServers": {
    "nalar": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

With `--token-env NAME`, every request must send
`Authorization: Bearer $NAME`; a missing or wrong token gets `401` and
`WWW-Authenticate: Bearer`, compared in constant time. The token is read from
the environment only, never printed, and `--print-config` writes a
`${NAME}` placeholder rather than a value. The listener binds `127.0.0.1` only
and validates the `Host` header against the bound port, so a rebinding hostname
cannot reach it. Requests are JSON-RPC 2.0
`POST`s with `Content-Type: application/json` to `/mcp` (or `/`), capped at
1 MB and 15 s; notifications return `202` with no body, and `GET` returns `405`
because there is no server-initiated stream. The transport is sessionless and
read-only: the same snapshot tools, resources and prompts are exposed, and
working-tree content is never read. Invalid JSON, oversized bodies, wrong
content types, unknown paths and mismatched hosts get JSON-RPC errors with an
HTTP status.

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_files` | `prefix`, `offset` | tracked text paths, 100 per page |
| `search` | `query`, `prefix` | literal matches with path and line |
| `read_lines` | `path`, `start`, `end` | up to 120 lines and `next_start` |
| `find_symbol` | `query` | Python AST/regex definitions by name |
| `repo_map` | — | bounded file/language counts and hot files |
| `git_log` | `path`, `limit` | recent commits |
| `git_show` | `sha`, `path` | one commit's message and bounded diff |

Paths are repository-relative and validated; absolute paths, parent traversal,
`.git`, symlinks and non-text blobs are rejected. Results are bounded JSON, and
unknown or write-capable tool names return an `isError` result.

## Resources

- `nalar://instructions/NALAR.md` and `nalar://instructions/AGENTS.md`: project
  guidance committed at the snapshot, labelled as untrusted data.
- `nalar://instructions/<path>`: bounded nested `NALAR.md`/`AGENTS.md` files
  (shallowest first, eight files, 16 KB each, 32 KB total) committed below the
  root; root guidance still wins conflicts.
- `nalar://skills/<name>`: committed `.nalar/skills/<name>/SKILL.md` bodies with
  their frontmatter description.

Only committed files are exposed. Working-tree edits and untracked files are
ignored; missing, oversized, invalid UTF-8 and symlinked files are omitted. An
unknown URI returns the MCP `-32002` resource-not-found error.

## Prompts

Committed `.nalar/commands/*.md` files appear as MCP prompts. Each prompt accepts
one optional `arguments` string substituted for `$ARGUMENTS` (or appended as
`Arguments:` when the body has no placeholder). Rendered text respects the
normal 6 KB command limit; unknown names or arguments are invalid-params errors.

## Protocol and limits

- One JSON-RPC 2.0 message per newline-delimited line; notifications never get a
  response.
- `initialize` negotiates `2024-11-05`, `2025-03-26` or `2025-06-18`. Batch
  arrays are served for the older versions and rejected for `2025-06-18`.
- Request content is capped at 1,000,000 bytes excluding the newline. Oversized
  physical lines are drained completely, so they cannot be re-read as a second
  request. Blank transport lines are ignored.
- A closed stdout shuts the server down without a traceback.
- Tools declare read-only/idempotent annotations.

## Security boundary

The server reads Git objects only. It has no write/apply tools, no shell, no
provider access and no project-config loading. Repository text is untrusted
data: prompts and resources tell a client what the project says, never what it
may do. A non-Git or unreadable `--repo` exits with status 2 before serving.
