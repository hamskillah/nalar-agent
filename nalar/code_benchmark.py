"""Paired correctness benchmark on synthetic, nonexecuted coding fixtures."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from .coding import Repository, apply_reviewed, git_environment, solve

CASES = (
    ("empty_mean", {"src/stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n"},
     "Make mean return 0 for an empty list. Do not change other behavior.",
     {"src/stats.py": "def mean(xs):\n    if not xs:\n        return 0\n    return sum(xs) / len(xs)\n"}),
    ("sync_version", {"src/version.py": 'VERSION = "1.0.0"\n', "README.md": "Version: 1.0.0\n"},
     "Change the version from 1.0.0 to 1.1.0 in both src/version.py and README.md. Change nothing else.",
     {"src/version.py": 'VERSION = "1.1.0"\n', "README.md": "Version: 1.1.0\n"}),
    ("guard_divide", {"src/math.py": "def divide(a, b):\n    return a / b\n"},
     "Make divide return None when b is zero. Change nothing else.",
     {"src/math.py": "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b\n"}),
    ("remove_obsolete", {"src/old.py": "obsolete = True\n"},
     "Delete src/old.py. Change nothing else.", {}),
    ("rename_module", {"src/a.py": "value = 1\n"},
     "Rename src/a.py to src/b.py. Change nothing else.",
     {"src/b.py": "value = 1\n"}),
    ("two_flags",
     {"src/flags.py": "".join(f"flag{index} = False\n" for index in range(20))},
     "Set flag0 and flag19 to True. Change nothing else.",
     {"src/flags.py": "".join(
         f"flag{index} = {'True' if index in (0, 19) else 'False'}\n"
         for index in range(20))}),
    ("make_executable",
     {"scripts/run.sh": "#!/bin/sh\necho hi\n"},
     "Make scripts/run.sh executable. Change nothing else.",
     {"scripts/run.sh": "#!/bin/sh\necho hi\n"},
     {"scripts/run.sh": True}),
)


def evaluate(chat_factory, *, model):
    rows = []
    for index, case in enumerate(CASES):
        name, inputs, task, expected, *rest = case
        expected_modes = rest[0] if rest else {}
        modes = ("baseline", "recover") if index % 2 == 0 else ("recover", "baseline")
        for mode in modes:
            with TemporaryDirectory(prefix="nalar-code-bench-") as directory:
                root = Path(directory)
                for path_name, text in inputs.items():
                    path = root / path_name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                def git(*args):
                    return subprocess.run(["git", "-C", directory, *args],
                                          env=git_environment(), check=True, capture_output=True,
                                          timeout=8).stdout
                git("init", "-q", "--template=")
                git("add", "--", *inputs)
                git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "-c", "user.name=Nalar Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "-qm", "benchmark fixture")
                repo = Repository(root)
                result, diff = solve(chat_factory(), repo, task, mode=mode)
                grade = "no_patch"
                if diff:
                    try:
                        apply_reviewed(repo, diff)
                    except ValueError:
                        grade = "patch_rejected"
                    else:
                        files = {p.relative_to(root).as_posix(): p.read_bytes()
                                 for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}
                        exact = files == {k: v.encode() for k, v in expected.items()}
                        for path, executable in expected_modes.items():
                            try:
                                info = (root / path).stat()
                            except OSError:
                                exact = False
                                break
                            if bool(info.st_mode & 0o111) != executable:
                                exact = False
                                break
                        grade = "exact" if exact else "artifact_mismatch"
                rows.append({"case": name, "mode": mode, "passed": grade == "exact", "grade": grade,
                             "status": result.status, "turns": result.turns, "calls": result.calls,
                             "tokens": result.tokens, "elapsed_ms": result.elapsed_ms, "trace": result.trace})
    return {"suite": "coding-patch-smoke-v1", "model": model, "runs": rows}
