import sys
import unittest

from nalar.agent import Runner
from nalar.core import Call, ProviderError, Reply
from nalar.mcp import MCPProcess
from nalar.tools import ToolBelt
from nalar.workspace import ToolError

from pathlib import Path

FIXTURE = Path(__file__).with_name("fixtures") / "mcp_server.py"


def spec(name, handler, description="test tool"):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}, handler


class RuntimeTests(unittest.TestCase):
    def test_belt_dispatches_and_rejects_duplicate_tools(self):
        belt = ToolBelt()
        belt.add(*spec("one", lambda args: {"value": 1}))
        self.assertEqual(belt.call("one", {}), {"value": 1})
        with self.assertRaises(ValueError):
            belt.add(*spec("one", lambda args: None))
        with self.assertRaises(ToolError):
            belt.call("missing", {})

    def test_transient_provider_errors_are_retried(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            if len(attempts) < 3:
                raise ProviderError("transport_unavailable")
            return Reply(text="done", tokens=4)

        belt = ToolBelt()
        result = Runner(chat, belt, retries=2, sleep=lambda _: None).run("task")
        self.assertEqual((result.status, result.text, len(attempts)), ("ok", "done", 3))
        self.assertEqual([step["status"] for step in result.trace],
                         ["transport_unavailable", "transport_unavailable"])

    def test_permanent_provider_errors_are_not_retried(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            raise ProviderError("http_400")

        result = Runner(chat, ToolBelt(), retries=2, sleep=lambda _: None).run("task")
        self.assertEqual((result.status, len(attempts)), ("provider_error", 1))

    def test_compaction_truncates_old_tool_output_but_keeps_recent(self):
        rounds = []

        def chat(messages, tools):
            rounds.append(len(messages))
            if len(rounds) <= 3:
                return Reply(calls=(Call("read", {"path": f"file{len(rounds)}.txt"}),))
            return Reply(text="finished", tokens=2)

        belt = ToolBelt()
        belt.add(*spec("read", lambda args: {"text": "x" * 400}))
        result = Runner(chat, belt, context_budget=1200).run("task")
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.compacted, 0)

    def test_events_report_tools_and_final_text(self):
        events = []

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("read", {"path": "a"}),))
            return Reply(text="done")

        belt = ToolBelt()
        belt.add(*spec("read", lambda args: {"text": "ok"}))
        Runner(chat, belt).run("task", on_event=events.append)
        self.assertIn({"type": "tool", "name": "read", "status": "ok"}, events)
        final = [event for event in events if event["type"] == "final"]
        self.assertEqual((final[0]["text"], final[0]["status"]), ("done", "ok"))


class MCPSkillBeltTests(unittest.TestCase):
    def test_mcp_tools_join_the_belt_with_namespaced_names(self):
        client = MCPProcess("fixture", (sys.executable, str(FIXTURE)), {})
        self.addCleanup(client.close)
        client.initialize()
        belt = ToolBelt()
        for tool in client.tools():
            name = tool["function"]["name"]
            belt.add(tool, lambda args, name=name: client.call(name, args))
        self.assertEqual(belt.call("fixture__echo", {"text": "halo"}), "halo")
        with self.assertRaises(ToolError):
            belt.call("fixture__explode", {})
        self.assertEqual(client.read_resource("fixture://readme"), "fixture resource")
        with self.assertRaises(ToolError):
            belt.call("fixture__explode", {})
        with self.assertRaises(Exception):
            client.read_resource("fixture://missing")
