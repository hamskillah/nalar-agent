"""Durable atomic file writes shared by configuration, sessions and init."""

import errno
import os
import tempfile

from pathlib import Path

TOLERATED = {errno.EINVAL, errno.ENOTSUP, errno.ENODEV, errno.EBADF}


def _default_mode():
    """0o666 masked by the process umask; reading umask is briefly global."""
    try:
        current = os.umask(0)
        os.umask(current)
        return 0o666 & ~current
    except OSError:
        return 0o644


def _fsync_directory(directory):
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


def write(path, data, *, mode=None, prefix=".nalar-write-"):
    """Write bytes through a fsynced temporary file and an atomic rename.

    An existing file keeps its permission bits; a new file uses the umask.
    The temporary file is removed on every failure, including interrupts.
    """
    path = Path(path)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = _default_mode()
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(handle, mode)
        except OSError:
            pass
        with os.fdopen(handle, "wb") as stream:
            handle = None
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError as exc:
                if exc.errno not in TOLERATED:
                    raise
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_text(path, text, *, mode=None, prefix=".nalar-write-"):
    write(path, text.encode("utf-8"), mode=mode, prefix=prefix)
