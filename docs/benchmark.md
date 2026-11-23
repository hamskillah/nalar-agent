# Benchmarks

Nalar ships a small paired benchmark for the coding loop: a **baseline** run
gets the task once; a **recovery** run gets the same task with tool-error
feedback. Both run in fresh synthetic Git fixtures, and a separate grader checks
the artifact exactly. `ok` means the loop terminated, not that the artifact is
correct.

## Code-patch smoke

```bash
nalar code-bench --model <model-id>
```

The suite covers text replacement, multi-file edits, file creation, renames,
deletions, executable-bit changes and version synchronization. Each case is
scored `exact`, `no_patch` or a tool/transport failure.

The public CI does not call a real provider. Provider behavior is exercised by
the local wire matrix in `tests/test_provider_matrix.py`; model quality requires
user-supplied credentials and is not claimed here.

## Free-model smoke

A single free-model smoke on a configured OpenAI-compatible provider produced:

| Check | Result |
| --- | --- |
| `ask` | `OK`, 883 tokens, 1 turn |
| `code` 41→42 | `ok`, 5 turns, 4 calls, 6,237 tokens; correct patch; checkout unchanged |
| `fix` with failing verification | `ok`, 1 attempt, 8 turns, 8 calls, 11,996 tokens; verification passed; checkout unchanged |
| `apply --check` | `checked`, one file, no credential findings |
| `code-bench` | 13/14 exact; one baseline tool-use miss, its recovery passed |

This is a smoke test, not a ranking. It does not measure provider latency, rate
limits, cross-provider behavior or long-horizon tasks.

## Artifact benchmark

`nalar bench` runs a smaller workspace benchmark with create/read/replace
artifacts. Historical local-model runs used Ollama with a 0.6B model; they are
kept only as a note that termination is not correctness. The project does not
recommend a model based on these numbers.
