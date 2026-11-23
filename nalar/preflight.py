"""Apply a proposed patch to a temporary snapshot and run verification.

The real checkout is never modified: the committed tree is exported to a
temporary directory, the patch is applied there, and the configured commands
run with provider credentials removed from the environment.
"""

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .coding import git_environment
from .verify import run_commands

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_MEMBERS = 20_000


class PreflightError(ValueError):
    pass


def _export(repository, target):
    try:
        raw = repository._git("archive", "--format=tar", repository.commit.decode())
    except ValueError:
        raise PreflightError("Cannot export the committed snapshot.") from None
    if len(raw) > MAX_ARCHIVE:
        raise PreflightError("The committed snapshot is too large for pre-flight checks.")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError:
        raise PreflightError("The snapshot archive is invalid.") from None
    count = 0
    with archive:
        for member in archive:
            count += 1
            if count > MAX_MEMBERS:
                raise PreflightError("The snapshot has too many files for pre-flight checks.")
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            destination = target / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with open(destination, "wb") as handle:
                    handle.write(source.read())
                os.chmod(destination, 0o700 if member.mode & 0o100 else 0o600)
            # Symlinks, devices and other special entries are intentionally skipped.


def verify_patch(repository, diff, commands, *, timeout=300, secret_names=()):
    """Apply `diff` to an exported snapshot and run verification commands.

    An empty diff verifies the committed snapshot itself.
    """
    if not commands:
        raise PreflightError("No verification commands are configured.")
    with tempfile.TemporaryDirectory(prefix="nalar-preflight-") as directory:
        target = Path(directory)
        _export(repository, target)
        environment = git_environment()
        environment["GIT_CEILING_DIRECTORIES"] = str(target.parent)
        for args in (["git", "init", "-q"],
                     ["git", "add", "-A"],
                     ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Nalar",
                      "-c", "user.email=nalar@example.test", "commit", "--allow-empty",
                      "-qm", "snapshot"]):
            try:
                setup = subprocess.run(args, cwd=str(target), env=environment,
                                       capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                return {"ok": False, "error": "apply_failed", "commands": []}
            if setup.returncode != 0:
                return {"ok": False, "error": "apply_failed", "commands": []}
        if diff:
            try:
                process = subprocess.run(
                    ["git", "apply", "-p1", "-"], cwd=str(target), input=diff.encode(),
                    env=environment, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                return {"ok": False, "error": "apply_failed", "commands": []}
            if process.returncode != 0:
                return {"ok": False, "error": "patch_rejected", "commands": [],
                        "detail": process.stderr.decode("utf-8", "replace")[-2_000:]}
        result = run_commands(commands, target, timeout=timeout, secret_names=secret_names)
        result["error"] = None
        return result


def verify_snapshot(repository, commands, *, timeout=300, secret_names=()):
    return verify_patch(repository, "", commands, timeout=timeout,
                        secret_names=secret_names)
