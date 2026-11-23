import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nalar.agent import Runner
from nalar.core import Call, Reply, run
from nalar.tools import ToolBelt
from nalar.workspace import ToolError, Workspace


def read_belt(delay=.2, fail=()):
    belt = ToolBelt()
    state = {"running": 0, "peak": 0, "order": []}

    def handler(args):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            time.sleep(delay)
            state["order"].append(args["n"])
            if args["n"] in fail:
                raise ToolError("read_failed")
            return args["n"]
        finally:
            state["running"] -= 1

    belt.add({"type": "function", "function": {
        "name": "read_lines", "description": "read",
        "parameters": {"type": "object", "properties": {"n": {"type": "string"}},
                       "additionalProperties": False}}}, handler)
    belt.add({"type": "function", "function": {
        "name": "create", "description": "write",
        "parameters": {"type": "object", "properties": {"n": {"type": "string"}},
                       "additionalProperties": False}}},
             lambda args: state["order"].append("write:" + args["n"]) or "ok")
    state["belt"] = belt
    return state


def batch_chat(calls):
    def chat(messages, tools):
        if len(messages) == 2:
            return Reply(calls=calls)
        return Reply(text="done")
    return chat


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.enterContext(Workspace(self.root))

    def test_create_does_not_overwrite(self):
        args = {"name": "report.txt", "text": "original"}
        self.workspace.call("create", args)
        with self.assertRaises(ToolError):
            self.workspace.call("create", {**args, "text": "different"})
        self.assertEqual((self.root / "report.txt").read_text(), "original")
        self.assertEqual(self.workspace.call("list", {}), ["report.txt"])

    def test_read_rejects_unsafe_entries(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
        outside.write_text("private")
        (self.root / "symlink").symlink_to(outside)
        os.link(outside, self.root / "hardlink")
        os.mkfifo(self.root / "fifo")
        (self.root / "directory").mkdir()
        for name in (str(outside), "../secret", ".env", "symlink", "hardlink", "fifo", "directory"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.workspace.call("read", {"name": name})
        self.assertEqual(self.workspace.call("list", {}), [])

    def test_list_skips_entries_that_disappear(self):
        class Entry:
            def __init__(self, name, fail=False):
                self.name = name
                self.fail = fail

            def stat(self, follow_symlinks=False):
                if self.fail:
                    raise OSError("gone")
                return os.stat_result((0o100644, 0, 0, 1, 0, 0, 10, 0, 0, 0))

        class Entries:
            def __enter__(self):
                return iter([Entry("good.txt"), Entry("gone.txt", fail=True)])

            def __exit__(self, *_):
                return False

        with patch("nalar.workspace.os.scandir", return_value=Entries()):
            self.assertEqual(self.workspace.call("list", {}), ["good.txt"])

    def test_tool_argument_shape_is_enforced(self):
        for args in ({"name": "a"}, {"name": "a", "text": 42}, {"name": "a", "text": "", "x": 0}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.workspace.call("create", args)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_recovery_produces_artifact_without_content_in_trace(self):
        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("missing", {}),))
            if len(messages) == 4:
                self.assertEqual(json.loads(messages[-1]["content"]), {"ok": False, "error": "unknown_tool"})
                return Reply(calls=(Call("create", {"name": "result.txt", "text": "private"}),))
            return Reply(text="done", tokens=20)

        result = run(chat, self.workspace, "private prompt", mode="recover")
        self.assertEqual((result.status, result.text, result.calls, result.tokens), ("ok", "done", 2, 20))
        self.assertEqual((self.root / "result.txt").read_text(), "private")
        self.assertNotIn("private", repr(result.trace))

    def test_baseline_aborts_after_tool_failure(self):
        result = run(lambda *_: Reply(calls=(Call("missing", {}),)), self.workspace, "task", mode="baseline")
        self.assertEqual((result.status, result.turns, result.calls), ("tool_error", 1, 1))

    def test_overbudget_batch_executes_none(self):
        calls = tuple(Call("create", {"name": name, "text": "x"}) for name in ("a", "b"))
        result = run(lambda *_: Reply(calls=calls), self.workspace, "task", max_calls=1)
        self.assertEqual((result.status, result.calls), ("call_limit", 0))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_turn_budget_prevents_unverifiable_side_effect(self):
        reply = Reply(calls=(Call("create", {"name": "a", "text": "x"}),))
        result = run(lambda *_: reply, self.workspace, "task", max_turns=1)
        self.assertEqual((result.status, result.turns, result.calls), ("turn_limit", 1, 0))
        self.assertFalse((self.root / "a").exists())

    def test_empty_replies_are_retried_then_reported(self):
        state = read_belt(delay=0)
        attempts = []
        events = []

        def chat(messages, tools):
            attempts.append(1)
            return Reply()

        result = Runner(chat, state["belt"], retries=2, sleep=lambda _: None).run(
            "task", on_event=events.append)
        self.assertEqual((result.status, len(attempts)), ("empty_response", 3))
        retries = [event for event in events if event["type"] == "retry"]
        self.assertEqual([event["code"] for event in retries], ["empty_response"] * 2)

    def test_an_empty_reply_then_success_completes(self):
        state = read_belt(delay=0)
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            return Reply() if len(attempts) == 1 else Reply(text="done")

        result = Runner(chat, state["belt"], retries=1, sleep=lambda _: None).run("task")
        self.assertEqual((result.status, len(attempts)), ("ok", 2))

    def test_discarded_empty_replies_charge_the_budget_by_estimate(self):
        state = read_belt(delay=0)
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            return Reply()

        result = Runner(chat, state["belt"], retries=2, sleep=lambda _: None,
                        token_budget=1).run("task")
        self.assertEqual((result.status, len(attempts)), ("token_limit", 1))
        self.assertTrue(result.tokens_estimated)

    def test_invalid_arguments_carry_a_schema_hint(self):
        belt = ToolBelt()

        def handler(args):
            raise ToolError("invalid_arguments")

        belt.add({"type": "function", "function": {
            "name": "read_lines", "description": "read",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "start": {"type": "integer"},
                "end": {"type": "integer"}}, "required": ["path", "start", "end"]}}},
            handler)
        with self.assertRaises(ToolError) as caught:
            belt.call("read_lines", {})
        self.assertEqual(str(caught.exception), "invalid_arguments")
        self.assertIn("required: path, start, end", caught.exception.detail)

    def test_common_tool_errors_carry_recovery_hints(self):
        belt = ToolBelt()

        def handler(args):
            raise ToolError("no_match")

        belt.add({"type": "function", "function": {
            "name": "replace", "description": "replace",
            "parameters": {"type": "object", "properties": {}}}}, handler)
        with self.assertRaises(ToolError) as caught:
            belt.call("replace", {})
        self.assertEqual(str(caught.exception), "no_match")
        self.assertIn("exact text", caught.exception.detail)

        def invalid_file(args):
            raise ToolError("invalid_file")

        belt.add({"type": "function", "function": {
            "name": "read", "description": "read",
            "parameters": {"type": "object", "properties": {}}}}, invalid_file)
        with self.assertRaises(ToolError) as caught:
            belt.call("read", {})
        self.assertIn("regular file", caught.exception.detail)

    def test_invalid_argument_hints_reach_the_model(self):
        belt = ToolBelt()

        def handler(args):
            raise ToolError("invalid_arguments")

        belt.add({"type": "function", "function": {
            "name": "read_lines", "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"]}}}, handler)
        seen = []

        def chat(messages, tools):
            seen.append(messages)
            if len(messages) == 2:
                return Reply(calls=(Call("read_lines", {}),))
            return Reply(text="done")

        Runner(chat, belt, max_turns=3).run("task")
        tool_messages = [message for message in seen[1]
                         if message.get("role") == "tool"]
        payload = json.loads(tool_messages[-1]["content"])
        self.assertEqual(payload["error"], "invalid_arguments")
        self.assertIn("path", payload["detail"])

    def _tool_belt(self, handler, name="replace"):
        belt = ToolBelt()
        belt.add({"type": "function", "function": {
            "name": name, "description": name,
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"path": {"type": "string"},
                                          "before": {"type": "string"},
                                          "after": {"type": "string"}}}}}, handler)
        return belt

    def test_identical_failing_calls_are_not_dispatched_twice(self):
        calls = []

        def handler(args):
            calls.append(args)
            raise ToolError("no_match")

        belt = self._tool_belt(handler)
        seen = []
        replies = iter([
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "y"}),)),
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "y"}),)),
            Reply(text="done"),
        ])

        def chat(messages, tools):
            seen.append([dict(message) for message in messages])
            return next(replies)

        result = Runner(chat, belt, max_turns=4).run("task")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(calls), 1)
        tool_results = [json.loads(message["content"]) for message in seen[-1]
                        if message.get("role") == "tool"]
        self.assertEqual(tool_results[-1]["error"], "repeated_call")
        self.assertIn("change it", tool_results[-1]["detail"])

    def test_different_arguments_are_not_blocked(self):
        calls = []

        def handler(args):
            calls.append(args)
            raise ToolError("no_match")

        belt = self._tool_belt(handler)
        replies = iter([
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "y"}),)),
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "z"}),)),
            Reply(text="done"),
        ])
        Runner(lambda messages, tools: next(replies), belt, max_turns=4).run("task")
        self.assertEqual(len(calls), 2)

    def test_a_successful_state_change_allows_the_retry(self):
        calls = []

        def flaky(args):
            calls.append(args)
            raise ToolError("no_match")

        belt = self._tool_belt(flaky)
        belt.add({"type": "function", "function": {
            "name": "mutate", "description": "mutate",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {}}}}, lambda args: "ok")
        replies = iter([
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "y"}),)),
            Reply(calls=(Call("mutate", {}),)),
            Reply(calls=(Call("replace", {"path": "a", "before": "x", "after": "y"}),)),
            Reply(text="done"),
        ])
        Runner(lambda messages, tools: next(replies), belt, max_turns=5).run("task")
        self.assertEqual(len(calls), 2)

    def test_compaction_falls_back_to_old_replies_and_keeps_the_current_task(self):
        loop = Runner(lambda messages, tools: Reply(text="x"),
                      self._tool_belt(lambda args: "ok"), context_budget=1_200)
        messages = [
            {"role": "system", "content": "s" * 40},
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "a" * 2_000},
            {"role": "assistant", "content": "b" * 2_000},
            {"role": "user", "content": "current task"},
            {"role": "assistant", "content": "c" * 20},
            {"role": "assistant", "content": "d" * 20},
        ]
        count = loop._compact(messages)
        self.assertGreaterEqual(count, 1)
        self.assertTrue(messages[2]["content"].startswith("[Earlier assistant"))
        self.assertEqual(messages[4]["content"], "current task")
        self.assertEqual(messages[0]["content"], "s" * 40)
        self.assertEqual(loop._compact(messages), 0)

    def test_reasoning_is_not_replayed_within_a_run(self):
        belt = self._tool_belt(lambda args: "ok")
        seen = []
        replies = iter([
            Reply(calls=(Call("replace", {"path": "a", "before": "x",
                                         "after": "y"}),),
                  reasoning="private thought"),
            Reply(text="done"),
        ])

        def chat(messages, tools):
            seen.append([dict(message) for message in messages])
            return next(replies)

        Runner(chat, belt, max_turns=3).run("task")
        self.assertTrue(all("reasoning_content" not in message
                            for message in seen[1]))

    def test_parallel_read_failures_are_not_reissued(self):
        state = read_belt(delay=0, fail=("bad",))
        replies = iter([
            Reply(calls=(Call("read_lines", {"n": "bad"}),
                         Call("read_lines", {"n": "bad"}))),
            Reply(calls=(Call("read_lines", {"n": "bad"}),
                         Call("read_lines", {"n": "bad"}))),
            Reply(text="done"),
        ])
        result = Runner(lambda messages, tools: next(replies), state["belt"],
                        max_turns=4).run("task")
        self.assertEqual(result.status, "ok")
        self.assertEqual(state["order"], ["bad", "bad"])

    def test_discarded_empty_replies_are_charged_once(self):
        state = read_belt(delay=0)
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            return Reply(tokens=5)

        from nalar.agent import Budget

        budget = Budget(0)
        result = Runner(chat, state["belt"], retries=1, sleep=lambda _: None,
                        budget=budget).run("task")
        self.assertEqual((result.status, len(attempts)), ("empty_response", 2))
        self.assertEqual(budget.spent, 10)

    def test_estimate_covers_the_whole_request(self):
        from nalar.agent import estimate_tokens

        short = estimate_tokens([{"role": "user", "content": "x"}], Reply())
        long = estimate_tokens([{"role": "system", "content": "s" * 400},
                                {"role": "user", "content": "x"}], Reply())
        with_tools = estimate_tokens([{"role": "user", "content": "x"}], Reply(),
                                     [{"type": "function", "function": {"name": "x"}}])
        self.assertGreater(long, short)
        self.assertGreater(with_tools, short)

    def test_compaction_records_which_tool_output_was_dropped(self):
        state = read_belt(delay=0)
        loop = Runner(lambda *_: None, state["belt"], context_budget=10)
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_lines", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1",
             "content": '{"ok": true, "value": "' + "x" * 200 + '"}'},
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "user", "content": "four"},
        ]
        self.assertEqual(loop._compact(messages), 1)
        self.assertIn('"tool": "read_lines"', messages[1]["content"])
        self.assertIn('"truncated": true', messages[1]["content"])
        self.assertEqual(loop._compact(messages), 0)

    def test_request_events_surround_each_provider_call(self):
        state = read_belt(delay=0)
        calls = (Call("read_lines", {"n": "a"}),)
        events = []
        result = Runner(batch_chat(calls), state["belt"], max_turns=3).run(
            "task", on_event=events.append)
        self.assertEqual(result.status, "ok")
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "request")
        self.assertEqual(kinds.count("request"), 2)
        self.assertEqual(kinds, ["request", "reply", "tool", "request", "reply", "final"])

    def test_read_only_batch_runs_concurrently_and_keeps_call_order(self):
        state = read_belt(delay=.15)
        calls = tuple(Call("read_lines", {"n": f"r{index}"}) for index in range(4))
        started = time.monotonic()
        result = Runner(batch_chat(calls), state["belt"], max_turns=3).run("task")
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, "ok")
        self.assertLess(elapsed, .45)
        self.assertGreaterEqual(state["peak"], 2)

    def test_failed_read_does_not_cancel_its_siblings(self):
        state = read_belt(delay=.01, fail=("bad",))
        calls = (Call("read_lines", {"n": "bad"}), Call("read_lines", {"n": "good"}))
        seen = []
        result = Runner(batch_chat(calls), state["belt"], max_turns=3).run(
            "task", on_event=lambda event: seen.append(event) if event["type"] == "tool" else None)
        self.assertEqual(result.status, "ok")
        statuses = {event["status"] for event in seen if event["type"] == "tool"}
        self.assertEqual(statuses, {"read_failed", "ok"})
        self.assertEqual(sorted(state["order"]), ["bad", "good"])

    def test_write_batches_stay_sequential_and_skip_after_failure(self):
        state = read_belt(delay=0)
        calls = (Call("create", {"n": "a"}), Call("create", {"n": "b"}))
        result = Runner(batch_chat(calls), state["belt"], max_turns=3).run("task")
        self.assertEqual(result.status, "ok")
        self.assertEqual(state["order"], ["write:a", "write:b"])

    def test_approval_gated_reads_are_not_run_concurrently(self):
        state = read_belt(delay=.01)
        state["belt"].apply_policy(ask=("read_lines",))
        approved = []
        calls = tuple(Call("read_lines", {"n": f"r{index}"}) for index in range(3))
        result = Runner(batch_chat(calls), state["belt"], max_turns=3,
                        approve=lambda name, args: approved.append(name) or True).run("task")
        self.assertEqual(result.status, "ok")
        self.assertEqual(approved, ["read_lines"] * 3)

    def test_successful_batch_executes_both_calls(self):
        calls = tuple(Call("create", {"name": name, "text": "x"}) for name in ("a", "b"))
        result = run(lambda messages, _: Reply(calls=calls) if len(messages) == 2 else Reply(text="done"),
                     self.workspace, "task", mode="recover")
        self.assertEqual((result.status, result.calls), ("ok", 2))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a", "b"])
