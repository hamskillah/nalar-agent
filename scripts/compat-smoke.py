#!/usr/bin/env python3
"""Cross-platform smoke: stdlib, session store, CLI help and provider matrix.

Run from the repository root on Linux, macOS or Windows:

    python scripts/compat-smoke.py

This does not call a real provider. It is intentionally narrower than
``scripts/release-check.sh`` so it can run on every supported OS while the
full suite remains Linux-verified.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(argv, env=None):
    return subprocess.run(
        argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120)


def main() -> int:
    import nalar
    from nalar.store import SessionStore

    print(f"python: {sys.version.split()[0]} {sys.platform}")
    print(f"nalar: {nalar.__version__}")

    with tempfile.TemporaryDirectory() as tmp:
        sessions = SessionStore(Path(tmp) / "sessions")
        repo = str((Path(tmp) / "repo").resolve())
        first = sessions.create(repo, "model", "base")
        sessions.append(first, "user", "task")
        sessions.append(first, "assistant", "done", tokens=5, model="model")
        assert [item["id"] for item in sessions.list(repo)] == [first.id]
        assert sessions.stats(repo)["tokens"] == 5
        assert sessions.search("task", repo)[0]["id"] == first.id
        sessions.create(repo, "model", "base")
        result = sessions.prune(keep=0, repo=repo)
        assert result["kept"] == 0
        assert sessions.list(repo) == []
        print("session store: ok")

    env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(Path(tempfile.gettempdir()) / "none.json")}
    env.pop("NALAR_API_KEY", None)
    result = run([sys.executable, "-m", "nalar", "--version"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(nalar.__version__)
    result = run([sys.executable, "-m", "nalar", "--help"], env)
    assert result.returncode == 0, result.stderr
    print("cli: ok")

    result = run([sys.executable, "-m", "unittest", "tests.test_provider_matrix", "-q"], env)
    assert result.returncode == 0, result.stderr
    print("provider matrix: ok")
    print("compatibility smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
