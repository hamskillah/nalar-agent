#!/usr/bin/env bash
# Reproducible release gate: tests, lint, wheel install smoke, provider matrix.
# Usage: scripts/release-check.sh [--live]
#   --live  also run one free-model `ask` against the configured provider key.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-python3}
RUFF=${RUFF:-ruff}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/nalar-release-XXXXXX")
trap 'rm -rf "$TMP"' EXIT

cd "$ROOT"
echo "== tests"
"$PYTHON" -m unittest discover -s tests -q

echo "== lint"
"$RUFF" check --select E9,F63,F7,F82,F601 nalar tests

echo "== provider matrix"
"$PYTHON" -m unittest tests.test_provider_matrix -q

echo "== wheel"
"$PYTHON" -m pip wheel . --no-deps -w "$TMP/wheel" -q
"$PYTHON" -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --no-deps -q "$TMP"/wheel/*.whl
"$TMP/venv/bin/nalar" --version
for sub in $("$TMP/venv/bin/nalar" --help |
        sed -n '/positional arguments/,/options/p' |
        grep -E '^    [a-z]' | awk '{print $1}'); do
    "$TMP/venv/bin/nalar" "$sub" --help >/dev/null
done
python3 - "$TMP"/wheel/*.whl <<'PY'
import sys
import zipfile

wheel = sys.argv[1]
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
assert any("/licenses/LICENSE" in name for name in names)
assert any("/licenses/NOTICE" in name for name in names)
print("wheel contents ok")
PY

if [ "${1:-}" = "--live" ]; then
    if [ -z "${NALAR_API_KEY:-}" ]; then
        echo "NALAR_API_KEY is not set; skipping live ask." >&2
        exit 1
    fi
    echo "== live ask"
    repo="$TMP/repo"
    mkdir -p "$repo"
    git -C "$repo" init -q --template=
    printf 'answer = 41\n' > "$repo/app.py"
    git -C "$repo" add app.py
    git -C "$repo" -c user.name=Release \
        -c user.email=release@example.test commit -qm fixture
    NALAR_GLOBAL_CONFIG="$TMP/none.json" \
        "$TMP/venv/bin/nalar" ask --repo "$repo" --model beam-free --json \
        "Reply with exactly OK"
fi

echo "release checks passed"
