import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.store import SessionError, SessionStore, _summary_digest


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = SessionStore(self.root / "sessions")

    def test_roundtrip_is_private_and_lists_newest_first(self):
        first = self.store.create("/repo", "model-a", "base1")
        self.store.append(first, "user", "Fix the parser")
        second = self.store.create("/repo", "model-b", "base2")
        self.assertEqual(self.store.load(first.id).entries[0]["text"], "Fix the parser")
        listed = self.store.list("/repo")
        self.assertEqual([item["id"] for item in listed], [second.id, first.id])
        path = self.store.directory / f"{first.id}.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)

    def test_rejects_bad_ids_and_symlinked_files(self):
        for value in ("../escape", "ABCDEF0123456789", "short", 7):
            with self.subTest(value=value), self.assertRaises(SessionError):
                self.store.load(value)
        outside = self.root / "outside.json"
        outside.write_text("{}")
        link = self.store.directory / ("a" * 16 + ".json")
        link.symlink_to(outside)
        with self.assertRaises(SessionError):
            self.store.load("a" * 16)

    def test_fork_copies_a_transcript_independently(self):
        source = self.store.create("/repo", "model", "base")
        self.store.append(source, "user", "original task")
        forked = self.store.fork(source.id)
        self.assertNotEqual(forked.id, source.id)
        self.assertEqual((forked.repo, forked.model, forked.base),
                         (source.repo, source.model, source.base))
        self.assertEqual([entry["text"] for entry in forked.entries], ["original task"])
        self.store.append(forked, "user", "fork task")
        self.assertEqual(len(self.store.load(source.id).entries), 1)
        self.assertEqual(len(self.store.load(forked.id).entries), 2)
        self.assertEqual(len(self.store.list("/repo")), 2)
        with self.assertRaises(SessionError):
            self.store.fork("missing")

    def test_corrupt_entries_are_skipped(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"] = ["oops", {"kind": "user", "text": "valid"},
                              {"kind": 3, "text": "bad kind"}, {"kind": "user", "text": 9}]
        path.write_text(json.dumps(payload))
        loaded = self.store.load(session.id)
        self.assertEqual([entry["text"] for entry in loaded.entries], ["valid"])
        self.assertEqual(len(self.store.list("/repo")), 1)

    def test_corrupt_token_values_do_not_crash_summaries(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"].append({"kind": "assistant", "text": "x", "tokens": "²"})
        path.write_text(json.dumps(payload))
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 0)
        self.assertEqual(self.store.stats("/repo")["tokens"], 0)

    def test_overflowing_token_values_do_not_crash_summaries(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"].append({"kind": "assistant", "text": "x", "tokens": 1e400})
        path.write_text(json.dumps(payload))
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 0)
        self.assertEqual(self.store.stats("/repo")["tokens"], 0)

    def test_symlinked_session_directory_is_refused(self):
        target = self.root / "private"
        target.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(SessionError):
            SessionStore(link)

    def test_caps_entries_and_text(self):
        session = self.store.create("/repo", "m", "b")
        for index in range(230):
            self.store.append(session, "user", "x" * 30_000)
        loaded = self.store.load(session.id)
        self.assertEqual(len(loaded.entries), 200)
        self.assertEqual(len(loaded.entries[-1]["text"]), 4_000)

    def test_query_filters_by_transcript_text(self):
        first = self.store.create("/repo", "m", "b")
        self.store.append(first, "user", "Fix the parser")
        second = self.store.create("/repo", "m", "b")
        self.store.append(second, "user", "Add logging")
        self.assertEqual([item["id"] for item in self.store.list(query="parser")], [first.id])
        self.assertEqual(self.store.list(query="missing"), [])

    def test_query_searches_beyond_the_newest_limit(self):
        oldest = self.store.create("/repo", "m", "b")
        self.store.append(oldest, "user", "needle in the oldest")
        for index in range(25):
            newer = self.store.create("/repo", "m", "b")
            self.store.append(newer, "user", f"filler {index}")
        self.assertEqual([item["id"] for item in self.store.list(query="needle", limit=20)],
                         [oldest.id])

    def test_stats_aggregate_tokens_models_and_repos(self):
        first = self.store.create("/repo-a", "model-a", "b")
        self.store.append(first, "assistant", "x", tokens=5)
        second = self.store.create("/repo-b", "model-b", "b")
        self.store.append(second, "assistant", "y", tokens=7)
        stats = self.store.stats()
        self.assertEqual((stats["sessions"], stats["tokens"]), (2, 12))
        self.assertEqual(stats["models"], ["model-a", "model-b"])
        self.assertEqual(stats["by_model"], {"model-a": 5, "model-b": 7})
        self.assertEqual(self.store.stats("/repo-a")["sessions"], 1)

    def test_prune_keeps_the_newest_sessions(self):
        sessions = []
        for index in range(4):
            session = self.store.create("/repo", "m", "b")
            self.store.append(session, "user", f"task {index}")
            sessions.append(session)
        result = self.store.prune(keep=2)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(len(result["deleted"]), 2)
        remaining = self.store.list()
        self.assertEqual([item["id"] for item in remaining], [sessions[-1].id, sessions[-2].id])

    def test_delete_removes_only_the_requested_session(self):
        first = self.store.create("/repo", "m", "b")
        second = self.store.create("/repo", "m", "b")
        self.store.delete(first.id)
        self.assertEqual([item["id"] for item in self.store.list()], [second.id])
        with self.assertRaises(SessionError):
            self.store.delete(first.id)

    def test_rejects_non_private_directory(self):
        folder = self.root / "public"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        with self.assertRaises(SessionError):
            SessionStore(folder)

    def test_search_returns_bounded_case_insensitive_snippets(self):
        first = self.store.create("/repo", "m", "b")
        self.store.append(first, "user", "Fix the PARSER " + "x" * 300)
        self.store.append(first, "assistant", "Changed parser.py")
        other = self.store.create("/elsewhere", "m", "b")
        self.store.append(other, "user", "unrelated task")
        found = self.store.search("parser", "/repo")
        self.assertEqual([item["id"] for item in found], [first.id])
        self.assertEqual([match["kind"] for match in found[0]["matches"]],
                         ["user", "assistant"])
        self.assertIn("Fix the PARSER", found[0]["matches"][0]["snippet"])
        self.assertLessEqual(len(found[0]["matches"][0]["snippet"]), 160)
        self.assertEqual(self.store.search("PaRsEr", "/repo")[0]["id"], first.id)
        self.assertEqual(self.store.search("missing", "/repo"), [])

    def test_search_rejects_empty_oversize_and_non_text_queries(self):
        for value in ("", "   ", "x" * 201, None, 7, "a\0b"):
            with self.subTest(value=value), self.assertRaises(SessionError):
                self.store.search(value)

    def test_search_honors_the_limit_and_skips_corrupt_transcripts(self):
        for index in range(4):
            session = self.store.create("/repo", f"m{index}", "b")
            self.store.append(session, "user", f"topic {index}")
        corrupt = self.store.directory / ("b" * 16 + ".json")
        corrupt.write_text("{not json", encoding="utf-8")
        self.assertEqual(len(self.store.search("topic", "/repo", limit=2)), 2)
        self.assertEqual(len(self.store.search("topic", "/repo", limit=50)), 4)

    def test_a_dangling_symlink_does_not_hide_valid_sessions(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "needle")
        link = self.store.directory / ("b" * 16 + ".json")
        link.symlink_to(self.root / "missing.json")
        self.assertEqual([item["id"] for item in self.store.list("/repo")],
                         [session.id])
        self.assertEqual(self.store.search("needle", "/repo")[0]["id"],
                         session.id)
        self.assertEqual(self.store.stats("/repo")["sessions"], 1)

    def test_search_snippets_and_matches_are_bounded_and_repo_scoped(self):
        session = self.store.create("/repo", "m", "b")
        for index in range(5):
            self.store.append(session, "user",
                              f"needle {index} " + "x" * 400)
        other = self.store.create("/other", "m", "b")
        self.store.append(other, "user", "needle elsewhere")
        found = self.store.search("needle", "/repo")
        self.assertEqual([item["id"] for item in found], [session.id])
        self.assertEqual(len(found[0]["matches"]), 3)
        self.assertTrue(all(len(match["snippet"]) <= 160
                            for match in found[0]["matches"]))

    def test_stats_attribute_tokens_to_each_entry_model(self):
        session = self.store.create("/repo", "primary", "b")
        self.store.append(session, "assistant", "work", tokens=5,
                          model="primary")
        self.store.append(session, "review", "1 finding", tokens=7,
                          model="reviewer")
        stats = self.store.stats("/repo")
        self.assertEqual(stats["by_model"], {"primary": 5, "reviewer": 7})
        self.assertEqual(stats["models"], ["primary", "reviewer"])
        self.assertEqual(stats["tokens"], 12)

    def test_a_file_path_is_reported_as_a_session_error(self):
        target = self.root / "not-a-directory"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(SessionError):
            SessionStore(target)

    def test_default_state_parent_is_made_private(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = home / ".local" / "share" / "nalar"
        state.mkdir(parents=True)
        os.chmod(state, 0o755)
        with patch.dict(os.environ, {"HOME": str(home)}):
            os.environ.pop("NALAR_SESSIONS", None)
            store = SessionStore()
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertTrue(store.directory.is_dir())

    def test_list_and_search_filter_by_entry_model(self):
        session = self.store.create("/repo", "primary", "b")
        self.store.append(session, "assistant", "principal work", tokens=3,
                          model="primary")
        self.store.append(session, "review", "review work", tokens=2,
                          model="reviewer")
        self.assertEqual([item["id"] for item in self.store.list(
            "/repo", model="reviewer")], [session.id])
        self.assertEqual([item["id"] for item in self.store.search(
            "work", "/repo", model="reviewer")], [session.id])
        self.assertEqual(self.store.list("/repo", model="other"), [])
        self.assertEqual(self.store.search("work", "/repo", model="other"), [])

    def test_environment_path_is_expanded(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.dict(os.environ, {"HOME": str(home),
                                     "NALAR_SESSIONS": "~/sessions"}):
            store = SessionStore()
        self.assertEqual(store.directory, home / "sessions")

    def test_corrupt_entry_models_do_not_crash_summaries(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"] = [
            {"kind": "assistant", "text": "x", "tokens": 5,
             "model": {"bad": 1}},
            {"kind": "assistant", "text": "y", "tokens": 2, "model": 7},
        ]
        path.write_text(json.dumps(payload))
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 7)
        self.assertEqual(self.store.stats("/repo")["tokens"], 7)
        self.assertIsInstance(self.store.search("x", "/repo"), list)

    def test_count_covers_repo_sessions_beyond_the_list_limit(self):
        for index in range(3):
            session = self.store.create("/repo", "m", "b")
            self.store.append(session, "user", f"repo {index}")
        for index in range(5):
            session = self.store.create("/other", "m", "b")
            self.store.append(session, "user", f"other {index}")
        self.assertEqual(self.store.count("/repo"), 3)

    def set_mtime(self, session, stamp):
        path = self.store.directory / f"{session.id}.json"
        os.utime(path, (stamp, stamp))

    def test_list_stats_and_count_see_repo_sessions_beyond_the_file_cap(self):
        repo_sessions = []
        for index in range(3):
            session = self.store.create("/repo", "m", "b")
            self.store.append(session, "user", f"repo {index}")
            repo_sessions.append(session)
        for index in range(1_001):
            session = self.store.create("/other", "m", "b")
            self.store.append(session, "user", f"other {index}")
        for index, session in enumerate(repo_sessions):
            self.set_mtime(session, 1_000_000 + index)
        self.assertEqual(len(self.store.list("/repo", limit=20)), 3)
        self.assertEqual(self.store.stats("/repo")["sessions"], 3)
        self.assertEqual(self.store.count("/repo"), 3)

    def test_prune_sees_repo_sessions_older_than_foreign_ones(self):
        repo_sessions = []
        for index in range(3):
            session = self.store.create("/repo", "m", "b")
            self.store.append(session, "user", f"repo {index}")
            repo_sessions.append(session)
        for index in range(1_001):
            session = self.store.create("/other", "m", "b")
            self.store.append(session, "user", f"other {index}")
        for index, session in enumerate(repo_sessions):
            self.set_mtime(session, 1_000_000 + index)
        result = self.store.prune(keep=2, repo="/repo")
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["deleted"], [repo_sessions[0].id])
        remaining = {item["id"] for item in self.store.list("/repo")}
        self.assertEqual(remaining, {repo_sessions[1].id, repo_sessions[2].id})

    def test_count_sees_sessions_beyond_the_ten_thousand_cap(self):
        payload = {"id": "0" * 16, "repo": "/repo", "model": "m",
                   "base": "b", "created": "now", "updated": "now",
                   "entries": []}
        target = self.store.directory / f"{payload['id']}.json"
        target.write_text(json.dumps(payload))
        os.utime(target, (1_000_000, 1_000_000))
        for index in range(10_000):
            payload["id"] = f"{index + 1:016x}"
            payload["repo"] = "/other"
            path = self.store.directory / f"{payload['id']}.json"
            path.write_text(json.dumps(payload))
        self.assertEqual(self.store.count("/repo"), 1)
        self.assertEqual(self.store.count("/other"), 10_000)

    def test_stats_requests_every_session(self):
        seen = {}

        def fake_list(repo=None, limit=20, **kwargs):
            seen["limit"] = limit
            return []

        with patch.object(self.store, "list", side_effect=fake_list):
            self.assertEqual(self.store.stats()["sessions"], 0)
        self.assertIsNone(seen["limit"])

    def test_prune_streams_without_listing_every_session(self):
        with patch.object(self.store, "list",
                          side_effect=AssertionError("prune must stream")):
            self.assertEqual(self.store.prune(keep=3),
                             {"deleted": [], "kept": 0, "skipped": 0})

    def test_prune_rejects_a_negative_keep(self):
        for value in (-1, -100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.prune(keep=value)

    def test_summary_keeps_totals_beyond_the_entry_window(self):
        session = self.store.create("/repo", "m", "b")
        for index in range(205):
            self.store.append(session, "assistant", f"step {index}",
                              tokens=1, model="m")
        loaded = self.store.load(session.id)
        self.assertEqual(len(loaded.entries), 200)
        self.assertEqual(loaded.summary()["tokens"], 205)
        self.assertEqual(loaded.summary()["by_model"], {"m": 205})
        forked = self.store.load(self.store.fork(session.id).id)
        self.assertEqual(forked.summary()["tokens"], 205)

    def test_old_session_without_totals_falls_back_to_entries(self):
        payload = {
            "id": "a" * 16, "repo": "/repo", "model": "m", "base": "b",
            "created": "now", "updated": "now",
            "entries": [
                {"kind": "assistant", "text": "a", "tokens": 3, "model": "m"},
                {"kind": "subagent", "text": "b", "tokens": 4,
                 "model": "vendor/tiny"},
            ],
        }
        (self.store.directory / f"{payload['id']}.json").write_text(
            json.dumps(payload))
        summary = self.store.load(payload["id"]).summary()
        self.assertEqual(summary["tokens"], 7)
        self.assertEqual(summary["by_model"],
                         {"m": 3, "vendor/tiny": 4})

    def test_index_avoids_reparsing_unchanged_sessions(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "task")
        calls = {"count": 0}
        original = self.store.load

        def counting(session_id):
            calls["count"] += 1
            return original(session_id)

        with patch.object(self.store, "load", side_effect=counting):
            self.store.list("/repo")
            first = calls["count"]
            self.store.list("/repo")
            second = calls["count"] - first
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_index_tracks_appends_and_deletes(self):
        first = self.store.create("/repo", "m", "b")
        second = self.store.create("/repo", "m", "b")
        self.assertEqual(len(self.store.list("/repo")), 2)
        self.store.append(first, "assistant", "done", tokens=5, model="m")
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 5)
        self.store.delete(second.id)
        index = json.loads((self.store.directory / "index.json").read_text())
        self.assertEqual(set(index["entries"]), {first.id})

    def test_corrupt_or_symlinked_index_is_ignored(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        index = self.store.directory / "index.json"
        index.write_text("{")
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        self.assertEqual(json.loads(index.read_text())["version"], 1)
        outside = self.root / "outside-index.json"
        outside.write_text("keep")
        index.unlink()
        index.symlink_to(outside)
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        self.assertEqual(outside.read_text(), "keep")

    def test_index_is_private(self):
        self.store.create("/repo", "m", "b")
        self.store.list("/repo")
        path = self.store.directory / "index.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_prune_reports_skipped_corrupt_files(self):
        session = self.store.create("/repo", "m", "b")
        corrupt = self.store.directory / ("b" * 16 + ".json")
        corrupt.write_text("{")
        result = self.store.prune(keep=0, repo="/repo")
        self.assertEqual(result["deleted"], [session.id])
        self.assertEqual(result["skipped"], 1)

    def test_load_treats_a_disappearing_file_as_missing(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        original = Path.is_file

        def racing(candidate):
            result = original(candidate)
            if candidate == path:
                path.unlink()
            return result

        with patch.object(Path, "is_file", racing):
            with self.assertRaises(SessionError):
                self.store.load(session.id)

    def test_index_detects_same_size_same_mtime_edits(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=5, model="m")
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 5)
        path = self.store.directory / f"{session.id}.json"
        before = path.stat()
        payload = json.loads(path.read_text())
        payload["entries"][0]["tokens"] = "7"
        payload["totals"]["tokens"] = 7
        payload["totals"]["by_model"]["m"] = 7
        path.write_text(json.dumps(payload))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 7)

    def test_wrong_version_or_unwritable_index_is_rebuilt(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        index = self.store.directory / "index.json"
        index.write_text(json.dumps({"version": 999, "entries": {}}))
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        self.assertEqual(json.loads(index.read_text())["version"], 1)

        index.write_text(json.dumps({"version": 999, "entries": {}}))
        with patch("nalar.store._atomic_write", side_effect=OSError("read only")):
            self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)

    def test_symlinked_valid_index_is_not_trusted(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        index = self.store.directory / "index.json"
        outside = self.root / "outside-index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["tokens"] = 999
        outside.write_text(json.dumps(data))
        index.unlink()
        index.symlink_to(outside)
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        self.assertFalse(index.is_symlink())

    def test_prune_counts_only_sessions_it_actually_removed(self):
        first = self.store.create("/repo", "m", "b")
        second = self.store.create("/repo", "m", "b")
        with patch.object(self.store, "_unlink",
                          side_effect=SessionError("session_delete_failed")):
            result = self.store.prune(keep=0, repo="/repo")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["kept"], 2)
        self.assertEqual(len(self.store.list("/repo")), 2)
        self.store.delete(first.id)
        self.store.delete(second.id)

    def test_prune_skips_symlinked_sessions(self):
        session = self.store.create("/repo", "m", "b")
        outside = self.root / "outside-session.json"
        outside.write_text("{}")
        link = self.store.directory / ("c" * 16 + ".json")
        link.symlink_to(outside)
        result = self.store.prune(keep=0, repo="/repo")
        self.assertEqual(result["deleted"], [session.id])
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(link.is_symlink())

    def test_schema_corrupt_index_is_rebuilt(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"] = {"repo": "/repo"}
        data["entries"][session.id]["digest"] = _summary_digest(
            data["entries"][session.id]["summary"])
        index.write_text(json.dumps(data))
        self.assertEqual(self.store.list("/repo")[0]["tokens"], 3)
        rebuilt = json.loads(index.read_text())["entries"][session.id]["summary"]
        self.assertEqual(rebuilt["tokens"], 3)

    def test_index_mode_is_repaired_by_a_scan(self):
        self.store.create("/repo", "m", "b")
        self.store.list("/repo")
        index = self.store.directory / "index.json"
        os.chmod(index, 0o644)
        self.store.list("/repo")
        self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o600)

    def test_invalid_utf8_transcript_is_skipped_everywhere(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        path.write_bytes(b'{"id":"' + session.id.encode() + b'","entries":[\xff]}')
        with self.assertRaises(SessionError):
            self.store.load(session.id)
        self.assertEqual(self.store.list("/repo"), [])
        self.assertEqual(self.store.stats("/repo")["sessions"], 0)
        self.assertEqual(self.store.search("x", "/repo"), [])
        result = self.store.prune(keep=0, repo="/repo")
        self.assertEqual((result["deleted"], result["skipped"]), ([], 1))

    def test_index_identity_mismatch_is_rebuilt(self):
        first = self.store.create("/repo", "m", "b")
        self.store.append(first, "user", "first")
        second = self.store.create("/repo", "m", "b")
        self.store.append(second, "user", "second")
        self.store.list("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][first.id]["summary"]["id"] = second.id
        data["entries"][first.id]["digest"] = _summary_digest(
            data["entries"][first.id]["summary"])
        index.write_text(json.dumps(data))
        listed = {item["id"]: item for item in self.store.list("/repo")}
        self.assertEqual(set(listed), {first.id, second.id})
        self.assertEqual(listed[first.id]["id"], first.id)

    def test_index_type_corruption_is_rebuilt(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "the task")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        self.store.stats("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["task"] = 123
        data["entries"][session.id]["summary"]["by_model"] = {"m": [1]}
        data["entries"][session.id]["digest"] = _summary_digest(
            data["entries"][session.id]["summary"])
        index.write_text(json.dumps(data))
        self.assertEqual(self.store.list("/repo")[0]["task"], "the task")
        self.assertEqual(self.store.stats("/repo")["by_model"], {"m": 3})

    def test_prune_ignores_a_corrupt_index_identity(self):
        first = self.store.create("/repo", "m", "b")
        self.store.append(first, "user", "first")
        second = self.store.create("/repo", "m", "b")
        self.store.append(second, "user", "second")
        self.store.list("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][first.id]["summary"]["id"] = second.id
        data["entries"][first.id]["digest"] = _summary_digest(
            data["entries"][first.id]["summary"])
        index.write_text(json.dumps(data))
        result = self.store.prune(keep=1, repo="/repo")
        self.assertEqual(result["deleted"], [first.id])
        self.assertTrue((self.store.directory / f"{second.id}.json").is_file())

    def test_empty_store_repairs_an_invalid_index(self):
        index = self.store.directory / "index.json"
        index.write_text("{")
        self.store.list()
        self.assertEqual(json.loads(index.read_text())["version"], 1)
        os.chmod(index, 0o644)
        self.store.list()
        self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o600)

    def test_prune_kept_counts_only_surviving_files(self):
        self.store.create("/repo", "m", "b")
        self.store.create("/repo", "m", "b")

        def vanish(session_id):
            self.store._path(session_id).unlink()
            raise SessionError("session_delete_failed")

        with patch.object(self.store, "_unlink", side_effect=vanish):
            result = self.store.prune(keep=0, repo="/repo")
        self.assertEqual((result["deleted"], result["kept"]), ([], 0))

    def test_deep_entry_metadata_is_sanitized_on_load(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        deep = 0
        for _ in range(3000):
            deep = [deep]
        payload["entries"] = [{"kind": "user", "text": "hi", "deep": deep}]
        path.write_text(json.dumps(payload))
        loaded = self.store.load(session.id)
        self.assertEqual(loaded.entries[0]["text"], "hi")
        self.assertNotIn("deep", loaded.entries[0])
        json.dumps(loaded.entries)
        forked = self.store.fork(session.id)
        self.assertEqual(forked.entries[0]["text"], "hi")

    def test_index_rejects_extra_summary_keys(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "the task")
        self.store.list("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["extra"] = {"deep": [1]}
        data["entries"][session.id]["digest"] = _summary_digest(
            data["entries"][session.id]["summary"])
        index.write_text(json.dumps(data))
        self.assertNotIn("extra", self.store.list("/repo")[0])

    def test_stale_digest_is_rejected_even_with_matching_stat(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "done", tokens=3, model="m")
        self.assertEqual(self.store.stats("/repo")["tokens"], 3)
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["tokens"] = 999
        index.write_text(json.dumps(data))
        self.assertEqual(self.store.stats("/repo")["tokens"], 3)

    def test_prune_skips_a_candidate_changed_after_the_scan(self):
        first = self.store.create("/repo", "m", "b")
        self.store.append(first, "user", "first")
        second = self.store.create("/repo", "m", "b")
        self.store.append(second, "user", "second")
        original = self.store._scan

        def scan_then_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.store.directory / f"{first.id}.json"
            payload = json.loads(path.read_text())
            payload["entries"][0]["text"] = "changed"
            path.write_text(json.dumps(payload))
            return result

        with patch.object(self.store, "_scan", side_effect=scan_then_edit), \
                patch("nalar.store._atomic_write", side_effect=OSError("full")):
            result = self.store.prune(keep=1, repo="/repo")
        self.assertEqual(result["deleted"], [])
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertTrue((self.store.directory / f"{first.id}.json").is_file())

    def test_prune_does_not_trust_a_forged_repo(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "task")
        self.store.list("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["repo"] = "/elsewhere"
        data["entries"][session.id]["digest"] = _summary_digest(
            data["entries"][session.id]["summary"])
        index.write_text(json.dumps(data))
        result = self.store.prune(keep=0, repo="/elsewhere")
        self.assertEqual(result["deleted"], [])
        self.assertTrue((self.store.directory / f"{session.id}.json").is_file())

    def test_zero_token_model_labels_remain_filterable(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "task")
        self.store.append(session, "review", "1 finding", status="ok",
                          model="vendor/reviewer", tokens=0)
        listed = self.store.list("/repo", model="vendor/reviewer")
        self.assertEqual([item["id"] for item in listed], [session.id])
        self.assertEqual(listed[0]["by_model"]["vendor/reviewer"], 0)
        self.assertIn("vendor/reviewer", self.store.stats("/repo")["models"])

    def test_null_model_metadata_is_not_stringified(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["entries"] = [{"kind": "assistant", "text": "x",
                               "model": None, "tokens": 0}]
        path.write_text(json.dumps(payload))
        loaded = self.store.load(session.id)
        self.assertNotIn("model", loaded.entries[0])

    def test_append_drops_null_model_and_clamps_tokens(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "user", "task")
        self.store.append(session, "assistant", "zero", model=None, tokens=0)
        loaded = self.store.load(session.id)
        self.assertNotIn("model", loaded.entries[-1])
        self.assertNotIn("None", loaded.summary()["by_model"])
        self.store.append(session, "assistant", "huge", model="m",
                          tokens=10**13)
        self.assertEqual(self.store.load(session.id).summary()["tokens"], 10**12)
        self.store.append(session, "assistant", "bool", model="m", tokens=True)
        self.assertEqual(self.store.load(session.id).summary()["tokens"], 10**12)

    def test_legacy_none_label_is_ignored(self):
        session = self.store.create("/repo", "m", "b")
        path = self.store.directory / f"{session.id}.json"
        payload = json.loads(path.read_text())
        payload["totals"] = {"tokens": 5, "by_model": {"None": 5}}
        path.write_text(json.dumps(payload))
        summary = self.store.load(session.id).summary()
        self.assertEqual(summary["tokens"], 5)
        self.assertNotIn("None", summary["by_model"])

    def test_index_rejects_legacy_none_label(self):
        session = self.store.create("/repo", "m", "b")
        self.store.append(session, "assistant", "x", tokens=5, model="m")
        self.store.stats("/repo")
        index = self.store.directory / "index.json"
        data = json.loads(index.read_text())
        data["entries"][session.id]["summary"]["by_model"] = {"None": 5}
        data["entries"][session.id]["digest"] = _summary_digest(
            data["entries"][session.id]["summary"])
        index.write_text(json.dumps(data))
        self.assertEqual(self.store.stats("/repo")["by_model"], {"m": 5})
