import unittest

from nalar.agent import Runner
from nalar.core import Call, ProviderError, Reply
from nalar.tools import ToolBelt


def belt_with(executed):
    belt = ToolBelt()
    belt.add({"type": "function", "function": {
        "name": "noop", "description": "noop",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False}}},
             lambda args: executed.append(1) or "ok")
    return belt


def chat_with_tokens(amount, executed):
    state = {"calls": 0}

    def chat(messages, tools):
        state["calls"] += 1
        if state["calls"] == 1:
            return Reply(calls=(Call("noop", {}),), tokens=amount)
        return Reply(text="done", tokens=amount)

    return chat


class TokenBudgetTests(unittest.TestCase):
    def test_exceeding_the_budget_stops_the_run(self):
        executed = []
        result = Runner(chat_with_tokens(4, executed), belt_with(executed),
                        token_budget=5).run("task")
        self.assertEqual((result.status, result.tokens), ("token_limit", 8))

    def test_budget_zero_means_unlimited(self):
        executed = []
        result = Runner(chat_with_tokens(1_000_000, executed), belt_with(executed),
                        token_budget=0).run("task")
        self.assertEqual(result.status, "ok")


class EstimateAndSubagentTests(unittest.TestCase):
    def test_missing_usage_is_estimated_so_the_budget_still_applies(self):
        result = Runner(lambda *_: Reply(text="x" * 400), ToolBelt(),
                        token_budget=10).run("task")
        self.assertEqual(result.status, "token_limit")
        self.assertTrue(result.tokens_estimated)

    def test_subagent_spend_counts_against_the_parent_budget(self):
        import tempfile
        from pathlib import Path as P
        from nalar.config import Config
        from nalar.coding import Repository
        from nalar.extensions import build_extensions
        from nalar.agent import Budget

        root = P(self.enterContext(tempfile.TemporaryDirectory()))
        import subprocess
        subprocess.run(["git", "-C", str(root), "init", "-q", "--template="], check=True)
        (root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c", "user.email=f@e.test",
                        "commit", "-qm", "fixture"], check=True)
        budget = Budget(100_000)
        # The sub-agent reports 40k tokens for one review.
        sub_chat = lambda messages, tools: Reply(text="sub", tokens=40_000)
        config = Config(subagents=True, token_budget=100_000)
        extensions = build_extensions(Repository(root), config, start_mcp=False,
                                      chat_factory=lambda: sub_chat, budget=budget)
        self.addCleanup(extensions.close)

        parent = lambda messages, tools: (Reply(calls=(Call("delegate", {"task": "look"}),))
                                          if len(messages) == 2 else Reply(text="done"))
        result = Runner(parent, extensions.belt, token_budget=100_000,
                        budget=budget).run("task")
        self.assertEqual(result.status, "ok")
        self.assertGreaterEqual(budget.spent, 40_000)
        self.assertGreaterEqual(result.tokens, 40_000)

    def test_subagent_spend_can_exhaust_the_parent_budget(self):
        import subprocess
        import tempfile
        from pathlib import Path as P

        from nalar.agent import Budget
        from nalar.coding import Repository
        from nalar.config import Config
        from nalar.extensions import build_extensions

        root = P(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(root), "init", "-q", "--template="], check=True)
        (root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c",
                        "user.email=f@e.test", "commit", "-qm", "fixture"], check=True)
        budget = Budget(2_000)
        sub_chat = lambda messages, tools: Reply(text="sub", tokens=40_000)
        config = Config(subagents=True, token_budget=2_000)
        extensions = build_extensions(Repository(root), config, start_mcp=False,
                                      chat_factory=lambda: sub_chat, budget=budget)
        self.addCleanup(extensions.close)
        parent = lambda messages, tools: (Reply(calls=(Call("delegate", {"task": "look"}),))
                                          if len(messages) == 2 else Reply(text="done"))
        result = Runner(parent, extensions.belt, token_budget=2_000, budget=budget).run("task")
        self.assertEqual(result.status, "token_limit")
        self.assertGreaterEqual(budget.spent, 40_000)

    def test_subagent_respects_denied_read_tools(self):
        import tempfile, subprocess
        from pathlib import Path as P
        from nalar.config import Config
        from nalar.coding import Repository
        from nalar.extensions import build_extensions

        root = P(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(root), "init", "-q", "--template="], check=True)
        (root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=F", "-c", "user.email=f@e.test",
                        "commit", "-qm", "fixture"], check=True)
        seen = {}

        def chat(messages, tools):
            seen["tools"] = {tool["function"]["name"] for tool in tools}
            return Reply(text="sub")

        config = Config(subagents=True, tools_deny=("read_lines",))
        extensions = build_extensions(Repository(root), config, start_mcp=False,
                                      chat_factory=lambda: chat)
        self.addCleanup(extensions.close)
        extensions.belt.call("delegate", {"task": "look"})
        self.assertNotIn("read_lines", seen["tools"])

    def test_nonfatal_tool_error_does_not_skip_the_batch(self):
        from nalar.workspace import ToolError

        executed = []
        belt = ToolBelt()
        belt.add({"type": "function", "function": {
            "name": "flaky", "description": "d",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}},
                 lambda args: (_ for _ in ()).throw(ToolError("flaky_failed", nonfatal=True)))
        belt.add({"type": "function", "function": {
            "name": "read", "description": "d",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}},
                 lambda args: executed.append(1) or "ok")

        def chat(messages, tools):
            if len(messages) == 2:
                return Reply(calls=(Call("flaky", {}), Call("read", {})))
            return Reply(text="done")

        result = Runner(chat, belt).run("task")
        self.assertEqual(result.status, "ok")
        self.assertEqual(executed, [1])
        self.assertEqual([step["status"] for step in result.trace], ["flaky_failed", "ok"])


class RateLimitRetryTests(unittest.TestCase):
    def test_http_429_is_retried(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("http_429")
            return Reply(text="ok")

        result = Runner(chat, ToolBelt(), retries=2, sleep=lambda _: None).run("task")
        self.assertEqual((result.status, len(attempts)), ("ok", 2))

    def test_retry_after_header_delay_is_used(self):
        attempts = []
        delays = []

        def chat(messages, tools):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("http_429", retry_after=3)
            return Reply(text="ok")

        result = Runner(chat, ToolBelt(), retries=2, sleep=delays.append).run("task")
        self.assertEqual((result.status, delays), ("ok", [3]))

    def test_retry_after_is_honoured_for_503(self):
        delays = []

        def chat(messages, tools):
            if not delays:
                raise ProviderError("http_503", retry_after=2)
            return Reply(text="ok")

        result = Runner(chat, ToolBelt(), retries=1, sleep=delays.append).run("task")
        self.assertEqual((result.status, delays), ("ok", [2]))

    def test_http_400_is_not_retried(self):
        attempts = []

        def chat(messages, tools):
            attempts.append(1)
            raise ProviderError("http_400")

        result = Runner(chat, ToolBelt(), retries=2, sleep=lambda _: None).run("task")
        self.assertEqual((result.status, len(attempts)), ("provider_error", 1))
