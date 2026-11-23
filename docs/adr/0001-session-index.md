# ADR 0001: Session index, cumulative totals and threaded scans

Status: accepted (2026-11-22)

## Context

Nalar stores one JSON transcript per session. `list`, `count`, `stats` and
`prune` scanned every file and parsed every transcript. At 100,000 sessions a
single scan took tens of seconds, froze the Textual UI, and made `doctor`
unpleasant. A session also retains at most 200 entries, so token totals for
older work disappeared even though the session file remained.

## Decision

1. Maintain a private `index.json` next to the transcripts. Each entry stores
   the session id, file `mtime_ns`/`ctime_ns`/inode/size, and the cached
   summary. Reads stat every file (cheap) and parse only new or changed
   transcripts. The index is written atomically with mode 0600, ignored if
   corrupt, a symlink, wrongly permissioned, or schema-invalid, and rebuilt
   from transcripts when absent. Appends do not rewrite the index; the next
   scan reconciles it. Concurrent scans and index replacement are safe because
   a changed file is detected by the stat tuple. Concurrent **appends to the
   same session** are single-writer by contract: the last writer wins, so two
   processes must not append to one transcript at the same time.
2. Persist cumulative token totals (`tokens`, `by_model`) in each session file.
   `append` updates them before the 200-entry retention window evicts older
   entries, so `stats` sees the whole session. Old files without totals fall
   back to their retained entries.
3. Run session list and resume-latest lookups in Textual workers using
   `asyncio.to_thread`, with a session-generation guard so a slow scan cannot
   replace a session after a task starts. Fork and single-session loads stay
   synchronous because they touch one file. The first index build therefore
   does not freeze the event loop, though brief GIL pauses remain.
4. `prune` uses the reconciled summaries, deletes every valid matching session
   beyond `keep`, and reports `skipped` for corrupt or symlinked files instead
   of pretending they were handled.

## Non-goals

- Search still parses candidate transcripts because it returns matching text;
  it is already threaded in the TUI. `sessions list --query` also bypasses the
  cached summaries.
- The index is a cache, never the source of truth. Deleting it is safe.
- No cross-process lock is added. Scans and index replacement are safe, but
  same-session appends remain single-writer.
- If the serialized index exceeds `MAX_INDEX` (64 MB), Nalar stops persisting
  it and falls back to full scans; it does not warn.
- Cumulative totals are authoritative for summaries. Hand-editing entries
  without updating totals changes neither `stats` nor `summary()`.

## Consequences

- First scan on a cold 100k store still costs one full parse; subsequent
  scans cost only stat calls plus changed files.
- New sessions preserve totals beyond 200 entries. Sessions written before
  this change cannot recover evicted totals.
- Index file contains the same metadata as transcripts (repo, model, task
  snippet, tokens), stays inside the 0700 session directory and is 0600.
