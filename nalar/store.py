"""Durable, private session transcripts.

Only conversation metadata is stored: tasks, final texts and outcomes. API
keys, prompts to the provider beyond the task text, and file contents are not
written here.
"""

import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .atomic import write as _atomic_write
from .usage import usage_totals

ID = re.compile(r"[a-f0-9]{16}")
MAX_FILE = 1_000_000
MAX_ENTRIES = 200
MAX_TEXT = 4_000
INDEX_FILE = "index.json"
INDEX_VERSION = 1
MAX_INDEX = 64_000_000


class SessionError(ValueError):
    """A session cannot be read or written."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token_value(value):
    if isinstance(value, bool):
        return 0
    try:
        amount = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(amount, 10**12) if amount > 0 else 0


def _valid_summary(summary, session_id):
    if not isinstance(summary, dict) or summary.get("id") != session_id:
        return False
    required = ("id", "repo", "model", "base", "updated", "entries",
                "tokens", "by_model", "task")
    if set(summary) != set(required):
        return False
    if not all(isinstance(summary[key], str)
               for key in ("id", "repo", "model", "base", "updated", "task")):
        return False
    if (not isinstance(summary["tokens"], int)
            or isinstance(summary["tokens"], bool) or summary["tokens"] < 0
            or not isinstance(summary["entries"], int)
            or isinstance(summary["entries"], bool) or summary["entries"] < 0):
        return False
    by_model = summary["by_model"]
    if not isinstance(by_model, dict):
        return False
    return all(isinstance(name, str) and name and name != "None"
               and isinstance(amount, int) and not isinstance(amount, bool)
               and amount >= 0 for name, amount in by_model.items())


def _summary_digest(summary):
    payload = json.dumps(summary, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _totals_from_entries(entries, default_model):
    tokens = 0
    by_model = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        amount = _token_value(entry.get("tokens"))
        raw_model = entry.get("model")
        if not amount and not raw_model:
            continue
        tokens += amount
        model = str(raw_model or default_model)[:200]
        by_model[model] = by_model.get(model, 0) + amount
    return {"tokens": tokens, "by_model": by_model}


@dataclass
class Session:
    id: str
    repo: str
    model: str
    base: str
    created: str
    updated: str
    entries: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    def summary(self):
        tasks = [entry.get("text", "")[:80] for entry in self.entries
                 if isinstance(entry, dict) and entry.get("kind") == "user"]
        if self.totals:
            tokens = _token_value(self.totals.get("tokens"))
            by_model = {}
            for model, amount in (self.totals.get("by_model") or {}).items():
                by_model[str(model)[:200]] = _token_value(amount)
        else:
            tokens = 0
            by_model = {}
            for entry in self.entries:
                if not isinstance(entry, dict):
                    continue
                amount = _token_value(entry.get("tokens"))
                raw_model = entry.get("model")
                if not amount and not raw_model:
                    continue
                tokens += amount
                model = raw_model or self.model
                if not isinstance(model, str):
                    model = str(model)
                by_model[model[:200]] = by_model.get(model[:200], 0) + amount
        return {"id": self.id, "repo": self.repo, "model": self.model, "base": self.base,
                "updated": self.updated, "entries": len(self.entries),
                "tokens": tokens, "by_model": by_model,
                "task": tasks[-1] if tasks else ""}


class SessionStore:
    def __init__(self, directory=None):
        selected = (directory or os.environ.get("NALAR_SESSIONS")
                    or Path.home() / ".local" / "share" / "nalar" / "sessions")
        self.directory = Path(selected).expanduser()
        parent = self.directory.parent
        state = Path.home() / ".local" / "share" / "nalar"
        try:
            if self.directory.is_symlink():
                raise SessionError("Session directory must be private and not a symlink.")
            if parent == state:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if parent.stat().st_mode & 0o077:
                    os.chmod(parent, 0o700)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (self.directory.is_symlink()
                    or (os.name == "posix"
                        and self.directory.stat().st_mode & 0o077)):
                raise SessionError("Session directory must be private and not a symlink.")
        except OSError:
            raise SessionError("session_directory_failed") from None

    def _path(self, session_id):
        if not isinstance(session_id, str) or not ID.fullmatch(session_id):
            raise SessionError("invalid_session_id")
        return self.directory / f"{session_id}.json"

    def create(self, repo, model, base):
        session_id = uuid4().hex[:16]
        now = _now()
        session = Session(session_id, str(repo), str(model), str(base), now, now)
        self.save(session)
        return session

    def fork(self, session_id):
        source = self.load(session_id)
        now = _now()
        try:
            entries = [copy.deepcopy(entry) for entry in source.entries]
            totals = copy.deepcopy(source.totals)
            session = Session(uuid4().hex[:16], source.repo, source.model,
                              source.base, now, now, entries, totals)
            self.save(session)
        except RecursionError:
            raise SessionError("session_too_complex") from None
        return session

    def save(self, session):
        path = self._path(session.id)
        payload = json.dumps({
            "id": session.id, "repo": session.repo, "model": session.model,
            "base": session.base, "created": session.created, "updated": session.updated,
            "entries": session.entries[-MAX_ENTRIES:],
            "totals": session.totals,
        }, ensure_ascii=True).encode()
        if len(payload) > MAX_FILE:
            raise SessionError("session_too_large")
        try:
            _atomic_write(path, payload, mode=0o600, prefix=".tmp-")
        except OSError:
            raise SessionError("session_write_failed") from None

    def load(self, session_id):
        path = self._path(session_id)
        try:
            if (path.is_symlink() or not path.is_file()
                    or path.stat().st_size > MAX_FILE):
                raise SessionError("session_not_found")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise SessionError("session_not_found") from None
        try:
            value = json.loads(text)
        except (UnicodeError, ValueError, RecursionError):
            raise SessionError("session_not_found") from None
        if not isinstance(value, dict) or value.get("id") != session_id:
            raise SessionError("session_not_found")
        entries = value.get("entries", [])
        if not isinstance(entries, list):
            raise SessionError("session_not_found")
        clean = []
        for entry in entries[-MAX_ENTRIES:]:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            kind = entry.get("kind")
            if not isinstance(text, str) or not isinstance(kind, str):
                continue
            item = {key: value for key, value in entry.items()
                    if (isinstance(value, (str, int, bool)) or value is None
                        or (isinstance(value, float) and math.isfinite(value)))}
            item["text"] = text[:MAX_TEXT]
            item["kind"] = kind[:32]
            if "model" in item:
                if item["model"] is None:
                    item.pop("model")
                elif not isinstance(item["model"], str):
                    item["model"] = str(item["model"])[:200]
            clean.append(item)
        totals = {}
        raw_totals = value.get("totals")
        if isinstance(raw_totals, dict) and raw_totals:
            raw_by = raw_totals.get("by_model")
            by_model = {}
            if isinstance(raw_by, dict):
                for model, amount in raw_by.items():
                    label = str(model)[:200]
                    if label and label != "None":
                        by_model[label] = _token_value(amount)
            totals = {"tokens": _token_value(raw_totals.get("tokens")),
                      "by_model": by_model}
        return Session(value["id"], str(value.get("repo", "")), str(value.get("model", "")),
                       str(value.get("base", "")), str(value.get("created", "")),
                       str(value.get("updated", "")), clean, totals)

    def append(self, session, kind, text, **meta):
        if not isinstance(text, str):
            raise SessionError("invalid_entry")
        entry = {"kind": str(kind)[:32], "text": text[:MAX_TEXT], "at": _now()}
        entry.update({key: str(value)[:200] for key, value in meta.items()
                      if value is not None
                      and not (key == "model" and value == "")})
        if not session.totals:
            session.totals = _totals_from_entries(session.entries, session.model)
        amount = _token_value(entry.get("tokens"))
        raw_model = entry.get("model")
        model = str(raw_model or (session.model if amount else ""))[:200]
        if amount or raw_model:
            totals = session.totals
            totals["tokens"] = _token_value(totals.get("tokens")) + amount
            by_model = totals.setdefault("by_model", {})
            if model:
                by_model[model] = _token_value(by_model.get(model)) + amount
        session.entries.append(entry)
        session.entries = session.entries[-MAX_ENTRIES:]
        session.updated = entry["at"]
        self.save(session)
        return entry

    def record_run(self, *, repo, model, base, task, status, text="",
                   tokens=0, turns=0, calls=0, verification=None, review=None,
                   diff="", files=(), usage=()):
        """Record a non-interactive run summary without patch contents."""
        from hashlib import sha256

        session = self.create(repo, model, base)
        self.append(session, "user", task if isinstance(task, str) else str(task))
        self.append(session, "assistant", text or status, status=status,
                   tokens=tokens, turns=turns, calls=calls, model=model)
        if verification is not None:
            self.append(session, "verify",
                        "ok" if verification.get("ok") else "failed",
                        error=verification.get("error") or "",
                        attempts=verification.get("attempts", ""))
        if review is not None:
            findings = review.get("findings") or []
            self.append(session, "review", f"{len(findings)} finding(s)",
                        status=review.get("status", ""),
                        model=review.get("model", ""),
                        tokens=review.get("tokens") or 0)
        for label, amount in usage_totals(usage).items():
            self.append(session, "subagent", f"{amount} tokens",
                        model=label, tokens=amount)
        if diff:
            self.append(session, "patch", f"{len(diff)} bytes",
                        sha256=sha256(diff.encode("utf-8")).hexdigest(),
                        files=", ".join(list(files)[:12]))
        return session

    def stats(self, repo=None):
        summaries = self.list(repo, limit=None)
        by_model = {}
        for item in summaries:
            for model, tokens in item.get("by_model", {}).items():
                by_model[model] = by_model.get(model, 0) + tokens
        return {"sessions": len(summaries),
                "tokens": sum(item["tokens"] for item in summaries),
                "models": sorted({item["model"] for item in summaries}
                                 | set(by_model)),
                "repos": sorted({item["repo"] for item in summaries}),
                "by_model": dict(sorted(by_model.items()))}

    def prune(self, keep=20, repo=None):
        """Delete sessions beyond `keep`; `repo=None` means every repository."""
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
            raise ValueError("keep must be a non-negative integer")
        summaries, skipped, stats = self._scan(repo=repo, limit=None)
        deleted = []
        index, _ = self._load_index()
        wanted = Path(repo).resolve() if repo is not None else None
        for item in summaries[keep:]:
            session_id = item["id"]
            baseline = stats.get(session_id)
            path = self._path(session_id)
            try:
                info = path.stat()
            except OSError:
                skipped += 1
                continue
            key = (info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_size)
            if baseline is None or baseline != key:
                skipped += 1
                continue
            if wanted is not None:
                try:
                    current = self.load(session_id)
                except (SessionError, OSError, UnicodeError, RecursionError):
                    skipped += 1
                    continue
                if current.id != session_id or Path(current.repo) != wanted:
                    skipped += 1
                    continue
            try:
                self._unlink(session_id)
            except SessionError:
                skipped += 1
                continue
            deleted.append(session_id)
            index.pop(session_id, None)
        if deleted:
            self._save_index(index)
        kept = 0
        for item in summaries:
            try:
                if self._path(item["id"]).is_file():
                    kept += 1
            except (OSError, SessionError):
                continue
        return {"deleted": deleted, "kept": kept, "skipped": skipped}

    def _unlink(self, session_id):
        path = self._path(session_id)
        if path.is_symlink() or not path.is_file():
            raise SessionError("session_not_found")
        try:
            path.unlink()
        except OSError:
            raise SessionError("session_delete_failed") from None

    def delete(self, session_id):
        self._unlink(session_id)
        index, _ = self._load_index()
        if session_id in index:
            index.pop(session_id)
            self._save_index(index)

    def search(self, query, repo=None, limit=20, model=None):
        """Search recent transcripts and return bounded matching snippets."""
        if (not isinstance(query, str) or not query.strip()
                or len(query) > 200 or "\0" in query):
            raise SessionError("invalid_query")
        needle = query.lower()
        results = []
        candidates, _, _ = self._scan(repo=repo, model=model, limit=None)
        for summary in candidates:
            try:
                session = self.load(summary["id"])
            except (SessionError, OSError, UnicodeError, RecursionError):
                continue
            matches = []
            for entry in session.entries:
                text = entry.get("text", "")
                index = text.lower().find(needle)
                if index < 0:
                    continue
                start = max(0, index - 60)
                end = min(len(text), index + len(query) + 100)
                snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                matches.append({"kind": entry.get("kind", ""),
                                "at": entry.get("at", ""),
                                "snippet": snippet[:160]})
                if len(matches) == 3:
                    break
            if not matches:
                continue
            summary = dict(summary)
            summary["matches"] = matches
            results.append(summary)
            if len(results) >= limit:
                break
        return results

    def _entries(self):
        try:
            candidates = list(self.directory.glob("*.json"))
        except OSError:
            return [], 0
        found = []
        skipped = 0
        for path in candidates:
            if path.name == INDEX_FILE or not ID.fullmatch(path.stem):
                continue
            try:
                if path.is_symlink():
                    skipped += 1
                    continue
                info = path.stat()
            except OSError:
                skipped += 1
                continue
            found.append((info.st_mtime_ns, info.st_ctime_ns,
                          info.st_ino, info.st_size, path))
        found.sort(key=lambda item: (item[0], item[1], item[3]), reverse=True)
        return found, skipped

    def _paths(self, limit=None):
        entries, _ = self._entries()
        paths = [entry[4] for entry in entries]
        return paths if limit is None else paths[:limit]

    def _load_index(self):
        path = self.directory / INDEX_FILE
        try:
            if path.is_symlink() or not path.is_file():
                return {}, False
            info = path.stat()
            if info.st_size > MAX_INDEX or info.st_mode & 0o077:
                return {}, False
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return {}, False
        if (not isinstance(value, dict) or value.get("version") != INDEX_VERSION
                or not isinstance(value.get("entries"), dict)):
            return {}, False
        return value["entries"], True

    def _save_index(self, entries):
        payload = json.dumps({"version": INDEX_VERSION, "entries": entries},
                             ensure_ascii=True).encode()
        if len(payload) > MAX_INDEX:
            return
        try:
            _atomic_write(self.directory / INDEX_FILE, payload, mode=0o600,
                          prefix=".index-")
        except (OSError, ValueError):
            pass

    def _scan(self, repo=None, model=None, query=None, limit=20):
        index, valid_index = self._load_index()
        wanted = Path(repo).resolve() if repo is not None else None
        updated = {}
        result = []
        stats = {}
        entries, skipped = self._entries()
        for mtime_ns, ctime_ns, inode, size, path in entries:
            session_id = path.stem
            cached = index.get(session_id)
            session = None
            if (isinstance(cached, dict)
                    and cached.get("mtime_ns") == mtime_ns
                    and cached.get("ctime_ns") == ctime_ns
                    and cached.get("ino") == inode
                    and cached.get("size") == size
                    and cached.get("digest") == _summary_digest(cached.get("summary"))
                    and _valid_summary(cached.get("summary"), session_id)
                    and not query):
                summary = cached["summary"]
            else:
                try:
                    session = self.load(session_id)
                except (SessionError, OSError, UnicodeError, RecursionError):
                    skipped += 1
                    continue
                summary = session.summary()
            updated[session_id] = {"mtime_ns": mtime_ns, "ctime_ns": ctime_ns,
                                   "ino": inode, "size": size,
                                   "summary": summary,
                                   "digest": _summary_digest(summary)}
            stats[session_id] = (mtime_ns, ctime_ns, inode, size)
            if wanted is not None:
                raw_repo = summary.get("repo")
                if not isinstance(raw_repo, str) or Path(raw_repo) != wanted:
                    continue
            if model:
                models = {str(summary.get("model", ""))}
                models.update(str(name) for name in (summary.get("by_model") or {}))
                if model not in models:
                    continue
            if query:
                if session is None:
                    try:
                        session = self.load(session_id)
                    except SessionError:
                        continue
                needle = query.lower()
                haystack = [str(summary.get("task", ""))] + [
                    str(entry.get("text", "")) for entry in session.entries]
                if not any(needle in text.lower() for text in haystack):
                    continue
            if limit is None or len(result) < limit:
                result.append(summary)
        if not valid_index or updated != index:
            self._save_index(updated)
        return result, skipped, stats

    def count(self, repo=None):
        return len(self._scan(repo=repo, limit=None)[0])

    def list(self, repo=None, limit=20, query=None, model=None):
        if query is not None and (not isinstance(query, str) or not query.strip()
                                  or len(query) > 200 or "\0" in query):
            raise SessionError("invalid_query")
        return self._scan(repo=repo, model=model, query=query, limit=limit)[0]
