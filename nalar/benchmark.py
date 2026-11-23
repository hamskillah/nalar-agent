"""Paired smoke benchmark; scores files, not model claims."""

from pathlib import Path
from tempfile import TemporaryDirectory

from .core import run
from .workspace import ToolError, Workspace

CASES = (
    ("create_collision", {"ready.txt": "reserved\n"},
     ("Try creating ready.txt with exactly READY followed by a single newline. "
      "If the create tool reports that the name already exists, write that content to "
      "ready-alt.txt instead."),
     "ready-alt.txt", "READY\n"),
    ("sum_file", {"numbers.txt": "4\n5\n9\n"},
     ("Read numbers.txt and sum the integers. Create total.txt containing only the "
      "decimal sum followed by a single newline."), "total.txt", "18\n"),
    ("copy_value", {"source.txt": "alpha\n"},
     ("Read source.txt and create copy.txt containing exactly the same text. "
      "Change nothing else."), "copy.txt", "alpha\n"),
    ("replace_value", {"note.txt": "DRAFT\n"},
     "Replace DRAFT with FINAL in note.txt. Change nothing else.",
     "note.txt", "FINAL\n"),
    ("rename_value", {"old.txt": "moved\n"},
     "Rename old.txt to new.txt. Change nothing else.", "new.txt", "moved\n"),
    ("delete_value", {"obsolete.txt": "obsolete\n"},
     "Delete obsolete.txt. Change nothing else.", "obsolete.txt", None),
)


def benchmark(chat, *, model):
    rows = []
    for index, (task, inputs, prompt, target, expected) in enumerate(CASES):
        modes = ("baseline", "recover") if index % 2 == 0 else ("recover", "baseline")
        for mode in modes:
            with TemporaryDirectory(prefix="nalar-bench-") as directory:
                for name, text in inputs.items():
                    (Path(directory) / name).write_text(text, encoding="utf-8")
                with Workspace(directory) as workspace:
                    result = run(chat, workspace, prompt, mode=mode)
                    if expected is None:
                        passed = target not in workspace.call("list", {})
                    else:
                        try:
                            passed = workspace.call("read", {"name": target}) == expected
                        except ToolError:
                            passed = False
            rows.append({"task": task, "mode": mode, "passed": passed, "status": result.status,
                         "elapsed_ms": result.elapsed_ms, "turns": result.turns, "calls": result.calls,
                         "tokens": result.tokens, "trace": result.trace})
    return {"suite": "artifact-smoke-v1", "model": model, "runs": rows}
