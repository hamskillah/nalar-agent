"""Scoped flat-directory read/edit tools for `nalar run` and the web UI."""

import errno
import os
import re
import secrets
import stat

MAX_BYTES = 16_384
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
FIELDS = {
    "list": (),
    "read": ("name",),
    "create": ("name", "text"),
    "search": ("query",),
    "replace": ("name", "before", "after"),
    "delete": ("name",),
    "rename": ("name", "new_name"),
}
DESCRIPTIONS = {
    "list": "List readable text file names in the workspace.",
    "read": "Read a workspace file as UTF-8.",
    "create": "Create a new UTF-8 file. Never overwrites an existing name.",
    "search": "Find literal text in workspace files; returns name, line and text.",
    "replace": "Replace one exact, uniquely occurring span in a workspace file.",
    "delete": "Delete one workspace file.",
    "rename": "Rename one workspace file without overwriting.",
}
TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": DESCRIPTIONS[name],
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {field: {"type": "string"} for field in fields},
                       "required": list(fields)},
    }}
    for name, fields in FIELDS.items()
]


class ToolError(Exception):
    def __init__(self, code, nonfatal=False, detail=None):
        super().__init__(code)
        self.nonfatal = nonfatal
        self.detail = detail


def _eligible(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_BYTES


def _manageable(info):
    """A regular file we may delete or rename, regardless of its size."""
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


class Workspace:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def call(self, tool, args):
        if tool not in FIELDS:
            raise ToolError("unknown_tool")
        if (not isinstance(args, dict) or args.keys() != set(FIELDS[tool])
                or any(not isinstance(value, str) for value in args.values())):
            raise ToolError("invalid_arguments")
        if "name" in args and not NAME.fullmatch(args["name"]):
            raise ToolError("invalid_name")
        if "new_name" in args and not NAME.fullmatch(args["new_name"]):
            raise ToolError("invalid_name")
        try:
            if tool == "list":
                return self._list()
            if tool == "read":
                return self._read(args["name"])
            if tool == "create":
                self._create(args["name"], args["text"])
                return {"created": args["name"]}
            if tool == "search":
                return self._search(args["query"])
            if tool == "replace":
                return self._replace(args["name"], args["before"], args["after"])
            if tool == "delete":
                return self._delete(args["name"])
            return self._rename(args["name"], args["new_name"])
        except FileNotFoundError:
            raise ToolError("not_found") from None
        except FileExistsError:
            raise ToolError("already_exists") from None
        except UnicodeError:
            raise ToolError("invalid_utf8") from None
        except OSError:
            raise ToolError("file_unavailable") from None

    def _list(self):
        names = []
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if len(names) >= 1_000:
                    break
                if not NAME.fullmatch(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _eligible(info):
                    names.append(entry.name)
        return sorted(names)

    def _read(self, name):
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not _eligible(info):
            raise ToolError("invalid_file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            if not _eligible(os.fstat(stream.fileno())):
                raise ToolError("invalid_file")
            data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ToolError("file_too_large")
            return data.decode("utf-8")

    def _sync(self):
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def _create(self, name, text):
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ToolError("file_too_large")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        self._sync()

    def _search(self, query):
        if not 0 < len(query) <= 120 or "\n" in query:
            raise ToolError("invalid_query")
        matches = []
        skipped = 0
        for name in self._list():
            try:
                text = self._read(name)
            except (ToolError, UnicodeError, OSError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    if len(matches) >= 30:
                        return {"matches": matches, "truncated": True,
                                "skipped": skipped}
                    start = max(0, line.find(query) - 60)
                    matches.append({"name": name, "line": number,
                                    "text": line[start:start + 200]})
        return {"matches": matches, "truncated": False, "skipped": skipped}

    def _replace(self, name, before, after):
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not _eligible(info):
            raise ToolError("invalid_file")
        mode = stat.S_IMODE(info.st_mode)
        text = self._read(name)
        if not before:
            raise ToolError("empty_match")
        first = text.find(before)
        if first < 0:
            raise ToolError("no_match")
        if text.find(before, first + 1) >= 0:
            raise ToolError("ambiguous_match")
        updated = text[:first] + after + text[first + len(before):]
        data = updated.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ToolError("file_too_large")
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            raise ToolError("file_changed",
                            detail="The file changed while it was being edited; "
                                   "re-read it first.")
        self._write_atomic(name, data, mode)
        return {"replaced": name}

    def _delete(self, name):
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not _manageable(info):
            raise ToolError("invalid_file")
        os.unlink(name, dir_fd=self.fd)
        self._sync()
        return {"deleted": name}

    def _rename(self, name, new_name):
        if name == new_name:
            raise ToolError("invalid_name")
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not _manageable(info):
            raise ToolError("invalid_file")
        try:
            os.link(name, new_name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
        except FileExistsError:
            raise ToolError("already_exists") from None
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS,
                                 errno.EXDEV}:
                raise
            try:
                os.stat(new_name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ToolError("already_exists") from None
            os.rename(name, new_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self._sync()
            return {"renamed": {"from": name, "to": new_name}}
        try:
            os.unlink(name, dir_fd=self.fd)
        except OSError:
            try:
                linked = os.stat(new_name, dir_fd=self.fd, follow_symlinks=False)
                if (linked.st_dev, linked.st_ino) == (info.st_dev, info.st_ino):
                    os.unlink(new_name, dir_fd=self.fd)
            except OSError:
                pass
            raise
        self._sync()
        return {"renamed": {"from": name, "to": new_name}}

    def _write_atomic(self, name, data, mode=0o600):
        temporary = ".nalar-" + secrets.token_hex(8) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            os.fchmod(fd, mode)
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            try:
                os.fsync(self.fd)
            except OSError:
                pass
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except OSError:
                pass
            raise
