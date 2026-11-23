import subprocess
import tempfile
import unittest
from pathlib import Path

from nalar.agent import AgentLoop
from nalar.coding import TOOLS, Repository
from nalar.core import Call, Reply
from nalar.tools import ToolBelt
from nalar.workspace import ToolError


class PlanToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "app.py").write_text("answer = 41\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F",
                        "-c", "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def test_plan_is_validated_and_stored(self):
        result = self.repo.call("update_plan", {"items": [
            {"step": "Read the code", "status": "completed"},
            {"step": "Change the answer", "status": "in_progress"},
            {"step": "Run tests", "status": "pending"},
        ]})
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(self.repo.plan[1]["status"], "in_progress")

    def test_plan_events_are_independent_snapshots(self):
        belt = ToolBelt()
        spec = next(item for item in TOOLS
                    if item["function"]["name"] == "update_plan")
        belt.add(spec, lambda args: self.repo.call("update_plan", args))
        events = []
        replies = iter([
            Reply(calls=(Call("update_plan", {"items": [
                {"step": "One", "status": "completed"}]}),)),
            Reply(calls=(Call("update_plan", {"items": [
                {"step": "Two", "status": "in_progress"}]}),)),
            Reply(text="Done."),
        ])
        AgentLoop(lambda messages, tools: next(replies), belt).run(
            "work", on_event=events.append)
        plans = [event["items"] for event in events if event["type"] == "plan"]
        self.assertEqual(plans[0], [{"step": "One", "status": "completed"}])
        self.assertEqual(plans[1], [{"step": "Two", "status": "in_progress"}])

    def test_control_and_bidi_characters_are_refused(self):
        for step in ("x\x9by", "x\u2028y", "x\u202ey"):
            with self.subTest(step=step), self.assertRaisesRegex(ToolError,
                                                                 "invalid_plan"):
                self.repo.call("update_plan", {"items": [
                    {"step": step, "status": "pending"}]})

    def test_invalid_plans_are_refused_without_replacing_the_last_one(self):
        self.repo.call("update_plan", {"items": [
            {"step": "Keep me", "status": "pending"}]})
        bad = [
            [],
            [{"step": "x", "status": "done"}],
            [{"step": "x", "status": "pending"}, {"step": "X", "status": "pending"}],
            [{"step": "x\nspoof", "status": "pending"}],
            [{"step": "x", "status": "in_progress"},
             {"step": "y", "status": "in_progress"}],
            [{"step": "x", "status": "pending", "extra": True}],
        ]
        for items in bad:
            with self.subTest(items=items), self.assertRaises(ToolError):
                self.repo.call("update_plan", {"items": items})
        self.assertEqual(self.repo.plan, [{"step": "Keep me", "status": "pending"}])

    def test_plan_limit_and_shape_are_enforced(self):
        items = [{"step": f"step {index}", "status": "pending"} for index in range(21)]
        with self.assertRaises(ToolError):
            self.repo.call("update_plan", {"items": items})
        with self.assertRaisesRegex(ToolError, "invalid_arguments"):
            self.repo.call("update_plan", {"items": "not a list"})

    def test_agent_emits_plan_events_and_returns_the_plan(self):
        belt = ToolBelt()
        spec = next(item for item in TOOLS
                    if item["function"]["name"] == "update_plan")
        belt.add(spec, lambda args: self.repo.call("update_plan", args))
        events = []
        replies = iter([
            Reply(calls=(Call("update_plan", {"items": [
                {"step": "Inspect", "status": "in_progress"}]}),)),
            Reply(text="Done."),
        ])
        result = AgentLoop(lambda messages, tools: next(replies), belt).run(
            "work", on_event=events.append)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.plan, ({"step": "Inspect", "status": "in_progress"},))
        self.assertIn({"type": "plan",
                       "items": [{"step": "Inspect", "status": "in_progress"}]},
                      events)
