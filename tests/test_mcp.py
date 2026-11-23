import json
import sys
import time
import unittest
from pathlib import Path

from nalar.mcp import MCPError, MCPProcess

FIXTURE = Path(__file__).with_name("fixtures") / "mcp_server.py"
RESOURCES = Path(__file__).with_name("fixtures") / "mcp_resources.py"
STRUCTURED = Path(__file__).with_name("fixtures") / "mcp_structured.py"
SPAM = Path(__file__).with_name("fixtures") / "mcp_spam.py"
DEEP = Path(__file__).with_name("fixtures") / "mcp_deep.py"


class MCPTests(unittest.TestCase):
    def start(self):
        client = MCPProcess("fixture", (sys.executable, str(FIXTURE)), {})
        self.addCleanup(client.close)
        client.initialize()
        return client

    def test_lists_and_calls_tools_with_namespaced_names(self):
        client = self.start()
        tools = client.tools()
        self.assertEqual([tool["function"]["name"] for tool in tools],
                         ["fixture__echo", "fixture__explode", "fixture__env"])
        self.assertEqual(tools[0]["function"]["parameters"]["required"], ["text"])
        self.assertEqual(client.call("fixture__echo", {"text": "halo"}), "halo")

    def test_server_requests_are_answered_and_structured_content_is_used(self):
        client = MCPProcess("structured", (sys.executable, str(STRUCTURED)),
                            {}, timeout=5)
        self.addCleanup(client.close)
        client.initialize()
        self.assertEqual([tool["function"]["name"] for tool in client.tools()],
                         ["structured__echo"])
        result = client.call("structured__echo", {"text": "halo"})
        self.assertEqual(json.loads(result), {"text": "halo", "length": 4})

    def test_server_request_spam_times_out_without_blocking(self):
        client = MCPProcess("spam", (sys.executable, str(SPAM)), {}, timeout=1)
        self.addCleanup(client.close)
        client.initialize()
        started = time.monotonic()
        with self.assertRaises(MCPError):
            client.tools()
        self.assertLess(time.monotonic() - started, 5)

    def test_client_roots_are_advertised_and_returned(self):
        client = MCPProcess("structured", (sys.executable, str(STRUCTURED)),
                            {}, timeout=5,
                            roots=[("file:///repo", "repo")])
        self.addCleanup(client.close)
        client.initialize()
        result = client.call("structured__echo", {"text": "x"})
        self.assertEqual(json.loads(result), {
            "text": "x", "length": 1,
            "roots": [{"uri": "file:///repo", "name": "repo"}]})

    def test_deep_json_from_a_stdio_server_is_a_protocol_error(self):
        client = MCPProcess("deep", (sys.executable, str(DEEP)), {}, timeout=2)
        self.addCleanup(client.close)
        with self.assertRaises(MCPError):
            client.initialize()

    def test_tool_error_is_reported_as_tool_error(self):
        client = self.start()
        with self.assertRaises(MCPError) as caught:
            client.call("fixture__explode", {})
        self.assertIn("fixture failure", str(caught.exception))

    def test_unknown_names_and_transport_failures_are_rejected(self):
        client = self.start()
        with self.assertRaises(MCPError):
            client.call("fixture__missing", {})
        with self.assertRaises(MCPError):
            client.call("other__echo", {"text": "x"})
        client.close()
        with self.assertRaises(MCPError):
            client.tools()

    def test_repeated_or_empty_pages_stop_the_listing(self):
        from nalar.mcp import MCPProcess

        class Loop(MCPProcess):
            def __init__(self):
                self.calls = 0

            def _request(self, method, params, timeout=None):
                self.calls += 1
                return {"resources": [{"uri": f"file:///{self.calls}.txt"}],
                        "nextCursor": "same"}

        client = Loop()
        self.assertEqual(len(client.resources()), 2)
        self.assertEqual(client.calls, 2)

        class Empty(MCPProcess):
            def __init__(self):
                self.calls = 0

            def _request(self, method, params, timeout=None):
                self.calls += 1
                return {"resources": [], "nextCursor": "again"}

        empty = Empty()
        with self.assertRaises(MCPError):
            empty.resources()
        self.assertEqual(empty.calls, 1)

    def test_a_later_page_failure_keeps_earlier_items(self):
        from nalar.mcp import MCPProcess

        class Partial(MCPProcess):
            def __init__(self):
                self.calls = 0

            def _request(self, method, params, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return {"resources": [{"uri": "file:///only.txt"}],
                            "nextCursor": "more"}
                raise MCPError("server_error_-32603")

        client = Partial()
        self.assertEqual([item["uri"] for item in client.resources()],
                         ["file:///only.txt"])
        self.assertEqual(client.calls, 2)
        self.assertTrue(client.listing_truncated)

    def test_caps_and_page_limits_mark_listing_truncated(self):
        from nalar.mcp import MCPProcess

        class Many(MCPProcess):
            def __init__(self):
                self.name = "many"

            def _request(self, method, params, timeout=None):
                return {"tools": [{"name": f"t{index}",
                                   "inputSchema": {"type": "object"}}
                                  for index in range(200)]}

        many = Many()
        self.assertEqual(len(many.tools()), 128)
        self.assertTrue(many.listing_truncated)

        class Pages(MCPProcess):
            def __init__(self):
                self.name = "pages"
                self.calls = 0

            def _request(self, method, params, timeout=None):
                self.calls += 1
                return {"resources": [{"uri": f"file:///{self.calls}"}],
                        "nextCursor": f"c{self.calls}"}

        pages = Pages()
        self.assertEqual(len(pages.resources()), 10)
        self.assertTrue(pages.listing_truncated)

    def test_caps_warn_only_when_usable_items_are_dropped(self):
        from nalar.mcp import MCPProcess

        class Client(MCPProcess):
            def __init__(self, items):
                self.name = "cap"
                self.items = items

            def _request(self, method, params, timeout=None):
                return {"tools": self.items}

        valid = [{"name": f"t{index}", "inputSchema": {"type": "object"}}
                 for index in range(128)]
        exact = Client(valid + [{"name": 7}])
        self.assertEqual(len(exact.tools()), 128)
        self.assertFalse(exact.listing_truncated)

        capped = Client(valid + [{"name": "extra", "inputSchema": {"type": "object"}}])
        self.assertEqual(len(capped.tools()), 128)
        self.assertTrue(capped.listing_truncated)

    def test_method_not_found_means_no_tools(self):
        client = MCPProcess("resources", (sys.executable, str(RESOURCES), "reject-tools"), {})
        self.addCleanup(client.close)
        client.initialize()
        with self.assertRaises(MCPError) as caught:
            client.tools()
        self.assertEqual(str(caught.exception), "no_tools")
        self.assertEqual(client.resources()[0]["uri"], "file:///alpha.txt")

    def test_resources_and_prompts_follow_next_cursor(self):
        client = MCPProcess("resources", (sys.executable, str(RESOURCES)), {})
        self.addCleanup(client.close)
        client.initialize()
        resources = client.resources()
        self.assertEqual([item["uri"] for item in resources],
                         ["file:///alpha.txt", "file:///beta.txt"])
        self.assertEqual(client.read_resource("file:///beta.txt"), "beta text")
        prompts = client.prompts()
        self.assertEqual([item["name"] for item in prompts], ["brief", "review"])
        self.assertEqual(client.get_prompt("review", {"topic": "diff"}), "review:diff")

    def test_resource_templates_are_listed(self):
        client = MCPProcess("resources", (sys.executable, str(RESOURCES)), {})
        self.addCleanup(client.close)
        client.initialize()
        templates = client.resource_templates()
        self.assertEqual([item["uriTemplate"] for item in templates],
                         ["file:///{path}", "mem://{+path}"])
        self.assertEqual(client.read_resource("file:///dynamic.txt"),
                         "dynamic file:///dynamic.txt")

    def test_prompts_are_listed_and_rendered(self):
        client = self.start()
        prompts = client.prompts()
        self.assertEqual([item["name"] for item in prompts], ["review"])
        self.assertEqual(prompts[0]["arguments"][0]["name"], "focus")
        self.assertEqual(client.get_prompt("review", {"focus": "security"}),
                         "Review focus: security")
        with self.assertRaises(MCPError):
            client.get_prompt("missing", {})
        with self.assertRaises(MCPError):
            client.get_prompt("review", {"focus": 5})

    def test_environment_visible_to_the_server_is_limited(self):
        client = MCPProcess("fixture", (sys.executable, str(FIXTURE)), {"FIXTURE_ONLY": "1"})
        self.addCleanup(client.close)
        client.initialize()
        visible = client.call("fixture__env", {})
        self.assertIn("FIXTURE_ONLY", visible)
        self.assertNotIn("NALAR_API_KEY", visible)
