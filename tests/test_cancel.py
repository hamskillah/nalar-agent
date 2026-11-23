import threading
import unittest

from nalar.agent import Runner
from nalar.core import Call, Reply
from nalar.tools import ToolBelt


def belt(call_count=None):
    def read(args):
        if call_count is not None:
            call_count.append(1)
        return {"text": "data"}

    value = ToolBelt()
    value.add({"type": "function", "function": {
        "name": "read", "description": "read",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}, read)
    return value


class CancellationTests(unittest.TestCase):
    def test_cancel_before_start_prevents_model_calls(self):
        cancel = threading.Event()
        cancel.set()
        calls = []
        result = Runner(lambda *_: calls.append(1), belt(), cancel=cancel).run("task")
        self.assertEqual((result.status, result.calls, calls), ("cancelled", 0, []))

    def test_cancel_between_tool_calls_stops_the_loop(self):
        cancel = threading.Event()
        executed = []

        def read(args):
            executed.append(args["path"])
            cancel.set()
            return {"text": "first"}

        value = ToolBelt()
        value.add({"type": "function", "function": {
            "name": "read", "description": "read",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}, read)

        def chat(messages, tools):
            return Reply(calls=(Call("read", {"path": "a"}), Call("read", {"path": "b"})))

        result = Runner(chat, value, cancel=cancel).run("task")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(executed, ["a"])
        self.assertIn("cancelled", [step["status"] for step in result.trace])

    def test_cancel_is_checked_before_non_streaming_tool_execution(self):
        cancel = threading.Event()
        executed = []

        def chat(messages, tools, cancel=None):
            cancel.set()
            return Reply(calls=(Call("read", {"path": "a"}),))

        belt = ToolBelt()
        belt.add({"type": "function", "function": {
            "name": "read", "description": "read",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}},
                 lambda args: executed.append(1) or "ok")
        result = Runner(chat, belt).run("task", cancel=cancel)
        self.assertEqual((result.status, executed), ("cancelled", []))

    def test_cancel_after_first_turn_skips_second_model_request(self):
        cancel = threading.Event()
        requests = []

        def chat(messages, tools):
            requests.append(1)
            cancel.set()
            return Reply(calls=(Call("read", {"path": "a"}),))

        def read(args):
            return {"text": "ok"}

        value = ToolBelt()
        value.add({"type": "function", "function": {
            "name": "read", "description": "read",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}, read)
        result = Runner(chat, value, cancel=cancel).run("task")
        self.assertEqual((result.status, len(requests)), ("cancelled", 1))
