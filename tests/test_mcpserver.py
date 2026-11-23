import http.client
import io
import json
import os
import subprocess
import threading
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.coding import Repository
from nalar.mcpserver import (MAX_HTTP_BODY, MAX_LINE, READ_TOOLS, handle,
                             make_http_server, serve, serve_http, tool_specs)


def message(method, params=None, identifier=1):
    payload = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "app.py").write_text(
            "def load(path):\n    return path\n\nclass Parser:\n    pass\n",
            encoding="utf-8")
        (self.root / "notes.md").write_text("# Notes\nsecret marker\n",
                                            encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "init"],
                       check=True)
        self.repo = Repository(self.root)

    def start_http(self, token=None):
        server = make_http_server(self.root, 0, token=token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)
        return server.server_port

    def post(self, port, payload, *, headers=None, path="/mcp",
             method="POST", raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = raw if raw is not None else json.dumps(payload).encode()
            values = {"Content-Type": "application/json"}
            values.update(headers or {})
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read().decode())
        finally:
            connection.close()

    def test_http_transport_serves_snapshot_tools_and_notifications(self):
        port = self.start_http()
        status, headers, body = self.post(
            port, message("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body)["result"]["protocolVersion"],
                         "2025-06-18")
        status, _, body = self.post(port, message("tools/list", identifier=2))
        self.assertEqual(status, 200)
        names = [item["name"] for item in json.loads(body)["result"]["tools"]]
        self.assertIn("read_lines", names)
        status, _, body = self.post(port, message(
            "tools/call", {"name": "read_lines",
                           "arguments": {"path": "app.py", "start": 1, "end": 2}},
            identifier=3))
        self.assertEqual(status, 200, body)
        self.assertIn("def load", body)
        status, _, body = self.post(
            port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, ""))
        status, _, body = self.post(port, message("resources/list", identifier=4))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["resources"], [])

    def test_http_bearer_token_is_required_when_configured(self):
        port = self.start_http("secret-token")
        status, headers, _ = self.post(port, message("ping"))
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
        status, _, _ = self.post(port, message("ping"),
                                 headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, _, _ = self.post(port, message("ping"), method="GET")
        self.assertEqual(status, 401)
        status, _, body = self.post(
            port, message("ping"),
            headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(status, 200)
        self.assertIn('"result"', body)
        status, _, _ = self.post(
            port, message("ping"), method="GET",
            headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(status, 405)

    def test_http_token_env_is_required_and_not_printed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NALAR_TEST_MCP_TOKEN", None)
            with self.assertRaises(ValueError):
                serve_http(self.root, 0, token_env="NALAR_TEST_MCP_TOKEN")
        with self.assertRaises(ValueError):
            make_http_server(self.root, 0, token="")
        with self.assertRaises(ValueError):
            make_http_server(self.root, 0, token="tökén")

    def test_http_transport_rejects_bad_bodies_hosts_and_methods(self):
        port = self.start_http()
        status, headers, _ = self.post(port, message("ping"), method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        status, _, _ = self.post(port, message("ping"), path="/other")
        self.assertEqual(status, 404)
        status, _, _ = self.post(port, message("ping"),
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, body = self.post(port, None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)
        status, _, _ = self.post(port, message("ping"),
                                 headers={"Host": "evil.example"})
        self.assertEqual(status, 421)

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("POST", "/mcp")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(MAX_HTTP_BODY + 1))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            connection.close()

    def test_tools_are_read_only_and_schema_shaped(self):
        specs = tool_specs()
        self.assertEqual([spec["name"] for spec in specs], list(READ_TOOLS))
        self.assertIn("repo_map", {spec["name"] for spec in specs})
        for spec in specs:
            self.assertIsInstance(spec["description"], str)
            self.assertEqual(spec["inputSchema"]["type"], "object")
            self.assertFalse(spec["inputSchema"]["additionalProperties"])
            self.assertEqual(spec["annotations"],
                             {"readOnlyHint": True, "idempotentHint": True,
                              "openWorldHint": False})
        specs = {spec["name"]: spec for spec in specs}
        self.assertEqual(set(specs["read_lines"]["inputSchema"]["required"]),
                         {"path", "start", "end"})
        self.assertEqual(specs["read_lines"]["inputSchema"]["properties"]["start"],
                         {"type": "integer"})
        self.assertEqual(set(specs["git_show"]["inputSchema"]["required"]),
                         {"sha", "path"})
        self.assertEqual(specs["repo_map"]["inputSchema"]["required"], [])
        from nalar.extensions import READ_TOOLS as BELT_READ
        self.assertEqual(set(READ_TOOLS), set(BELT_READ))
        names = set(specs)
        for write in ("replace", "create_file", "delete_file", "rename_file",
                      "chmod_file", "apply_patch"):
            self.assertNotIn(write, names)

    def test_initialize_declares_tools_and_read_only_instructions(self):
        response = handle(self.repo, message("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}))
        result = response["result"]
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "nalar")
        self.assertIn("read-only", result["instructions"])

    def test_tools_list_and_call_return_json_content(self):
        listed = handle(self.repo, message("tools/list"))["result"]["tools"]
        self.assertEqual({spec["name"] for spec in listed}, set(READ_TOOLS))
        response = handle(self.repo, message("tools/call", {
            "name": "search", "arguments": {"query": "secret marker", "prefix": ""}}))
        result = response["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["matches"][0]["path"], "notes.md")
        self.assertEqual(result["structuredContent"], payload)

    def test_resources_and_prompts_are_cached_per_snapshot(self):
        from nalar import mcpserver

        with patch.object(mcpserver, "load_instructions",
                          wraps=mcpserver.load_instructions) as loader, \
                patch.object(mcpserver, "discover_committed",
                             wraps=mcpserver.discover_committed) as skills, \
                patch.object(mcpserver, "discover_commands",
                             wraps=mcpserver.discover_commands) as commands:
            handle(self.repo, message("resources/list"))
            handle(self.repo, message("resources/list"))
            handle(self.repo, message("prompts/list"))
            handle(self.repo, message("prompts/list"))
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(skills.call_count, 1)
        self.assertEqual(commands.call_count, 1)

    def test_project_instructions_are_exposed_as_resources(self):
        (self.root / "NALAR.md").write_text("# Conventions\nUse tabs.\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Agents\nReview first.\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "AGENTS.md").write_text("# Nested\nUse pytest.\n",
                                                     encoding="utf-8")
        (self.root / "UNTRACKED.md").write_text("local only\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "NALAR.md", "AGENTS.md",
                        "src/AGENTS.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "guidance"],
                       check=True)
        repo = Repository(self.root)
        listed = handle(repo, message("resources/list"))["result"]["resources"]
        uris = {item["uri"] for item in listed}
        self.assertEqual(uris, {"nalar://instructions/NALAR.md",
                                "nalar://instructions/AGENTS.md",
                                "nalar://instructions/src/AGENTS.md"})
        self.assertTrue(all(item["mimeType"] == "text/markdown" for item in listed))
        self.assertIn("data, not permission", listed[0]["description"])
        read = handle(repo, message("resources/read", {
            "uri": "nalar://instructions/NALAR.md"}))["result"]["contents"][0]
        self.assertEqual(read["uri"], "nalar://instructions/NALAR.md")
        self.assertIn("Use tabs.", read["text"])
        nested = handle(repo, message("resources/read", {
            "uri": "nalar://instructions/src/AGENTS.md"}))["result"]["contents"][0]
        self.assertIn("Use pytest.", nested["text"])
        (self.root / "NALAR.md").write_text("# Local\nDo not use.\n", encoding="utf-8")
        read = handle(repo, message("resources/read", {
            "uri": "nalar://instructions/NALAR.md"}))["result"]["contents"][0]
        self.assertIn("Use tabs.", read["text"])
        self.assertNotIn("Do not use", read["text"])
        templates = handle(repo, message("resources/templates/list"))
        self.assertEqual(templates["result"]["resourceTemplates"], [])
        for params in ({}, {"uri": 3}):
            response = handle(repo, message("resources/read", params))
            self.assertEqual(response["error"]["code"], -32602, params)
        for params in ({"uri": "nalar://instructions/UNTRACKED.md"},
                       {"uri": "nalar://instructions/../SECRET"}):
            response = handle(repo, message("resources/read", params))
            self.assertEqual(response["error"]["code"], -32002, params)
        capabilities = handle(repo, message("initialize", {
            "protocolVersion": "2025-06-18"}))["result"]["capabilities"]
        self.assertIn("resources", capabilities)

    def test_committed_skills_are_exposed_as_resources(self):
        skills = self.root / ".nalar" / "skills"
        (skills / "review").mkdir(parents=True)
        (skills / "review" / "SKILL.md").write_text(
            "---\nname: review\ndescription: Review changes carefully\n---\n"
            "Read the diff first.\n", encoding="utf-8")
        (skills / "broken").mkdir()
        (skills / "broken" / "SKILL.md").write_text(
            "---\nname: other\ndescription: mismatch\n---\nbody\n",
            encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".nalar"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "skills"],
                       check=True)
        (skills / "local").mkdir()
        (skills / "local" / "SKILL.md").write_text(
            "---\nname: local\ndescription: untracked\n---\nbody\n",
            encoding="utf-8")
        repo = Repository(self.root)
        listed = handle(repo, message("resources/list"))["result"]["resources"]
        by_uri = {item["uri"]: item for item in listed}
        self.assertIn("nalar://skills/review", by_uri)
        self.assertNotIn("nalar://skills/local", by_uri)
        self.assertNotIn("nalar://skills/broken", by_uri)
        self.assertEqual(by_uri["nalar://skills/review"]["description"],
                         "Review changes carefully")
        read = handle(repo, message("resources/read", {
            "uri": "nalar://skills/review"}))["result"]["contents"][0]
        self.assertIn("Read the diff first.", read["text"])
        (skills / "review" / "SKILL.md").write_text(
            "---\nname: review\ndescription: edited\n---\nlocal edit\n",
            encoding="utf-8")
        read = handle(repo, message("resources/read", {
            "uri": "nalar://skills/review"}))["result"]["contents"][0]
        self.assertIn("Read the diff first.", read["text"])
        self.assertNotIn("local edit", read["text"])

    def test_committed_commands_are_exposed_as_prompts(self):
        self.assertEqual(handle(self.repo, message("prompts/list"))["result"]["prompts"],
                         [])
        commands = self.root / ".nalar" / "commands"
        commands.mkdir(parents=True)
        (commands / "bump.md").write_text(
            "---\ndescription: Bump the answer\n---\nSet answer to $ARGUMENTS.\n",
            encoding="utf-8")
        (commands / "plain.md").write_text(
            "---\ndescription: No placeholder\n---\nExplain the parser.\n",
            encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".nalar"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=F", "-c",
                        "user.email=f@example.test", "commit", "-qm", "commands"],
                       check=True)
        (commands / "local.md").write_text(
            "---\ndescription: untracked\n---\nlocal\n", encoding="utf-8")
        repo = Repository(self.root)
        listed = handle(repo, message("prompts/list"))["result"]["prompts"]
        names = {item["name"] for item in listed}
        self.assertEqual(names, {"bump", "plain"})
        by_name = {item["name"]: item for item in listed}
        self.assertEqual(by_name["bump"]["description"], "Bump the answer")
        self.assertEqual(by_name["bump"]["arguments"],
                         [{"name": "arguments",
                           "description": "Text substituted for $ARGUMENTS.",
                           "required": False}])
        rendered = handle(repo, message("prompts/get", {
            "name": "bump", "arguments": {"arguments": "42"}}))["result"]
        self.assertEqual(rendered["description"], "Bump the answer")
        self.assertEqual(rendered["messages"][0]["role"], "user")
        self.assertEqual(rendered["messages"][0]["content"]["text"],
                         "Set answer to 42.")
        plain = handle(repo, message("prompts/get", {
            "name": "plain", "arguments": {"arguments": "extra"}}))["result"]
        self.assertIn("Arguments: extra", plain["messages"][0]["content"]["text"])
        (commands / "bump.md").write_text(
            "---\ndescription: edited\n---\nlocal edit\n", encoding="utf-8")
        again = handle(repo, message("prompts/get", {
            "name": "bump", "arguments": {"arguments": "42"}}))["result"]
        self.assertEqual(again["messages"][0]["content"]["text"], "Set answer to 42.")
        for params in ({"name": "missing"},
                       {"name": "bump", "arguments": {"x": "1"}},
                       {"name": "bump", "arguments": {"arguments": 1}},
                       {"arguments": {}}, []):
            response = handle(repo, message("prompts/get", params))
            self.assertEqual(response["error"]["code"], -32602, params)
        capabilities = handle(repo, message("initialize", {
            "protocolVersion": "2025-06-18"}))["result"]["capabilities"]
        self.assertIn("prompts", capabilities)

    def test_untracked_guidance_is_not_a_resource(self):
        (self.root / "NALAR.md").write_text("# local only\n", encoding="utf-8")
        listed = handle(self.repo, message("resources/list"))["result"]["resources"]
        self.assertNotIn("nalar://instructions/NALAR.md",
                         {item["uri"] for item in listed})

    def test_write_tools_paths_and_arguments_fail_closed(self):
        for name in ("replace", "rename_file", "chmod_file", "apply_patch"):
            response = handle(self.repo, message("tools/call", {
                "name": name, "arguments": {}}))
            self.assertTrue(response["result"]["isError"])
            self.assertIn("unknown or non-read-only",
                          response["result"]["content"][0]["text"])
        response = handle(self.repo, message("tools/call", {
            "name": "read_lines", "arguments": {"path": "../outside",
                                                "start": 1, "end": 2}}))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("invalid_path", response["result"]["content"][0]["text"])
        response = handle(self.repo, message("tools/call", {
            "name": "read_lines", "arguments": {"path": "app.py"}}))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("invalid_arguments", response["result"]["content"][0]["text"])
        response = handle(self.repo, message("tools/call", {"arguments": {}}))
        self.assertEqual(response["error"]["code"], -32602)

    def test_protocol_errors_and_notifications(self):
        self.assertIsNone(handle(self.repo, {"jsonrpc": "2.0", "method": "ping"}))
        self.assertIsNone(handle(self.repo, {"jsonrpc": "1.0", "method": "notifications/x"}))
        self.assertEqual(handle(self.repo, message("missing/method"))["error"]["code"],
                         -32601)
        self.assertEqual(handle(self.repo, {"jsonrpc": "2.0", "id": 1})["error"]["code"],
                         -32600)
        for identifier in ({"a": 1}, [1, 2], True):
            response = handle(self.repo, {"jsonrpc": "2.0", "id": identifier,
                                          "method": "ping"})
            self.assertEqual(response["error"]["code"], -32600)
            self.assertIsNone(response["id"])

    def test_initialize_negotiates_supported_versions_and_validates_params(self):
        old = handle(self.repo, message("initialize", {"protocolVersion": "2024-11-05"}))
        self.assertEqual(old["result"]["protocolVersion"], "2024-11-05")
        future = handle(self.repo, message("initialize", {"protocolVersion": "2099-01-01"}))
        self.assertEqual(future["result"]["protocolVersion"], "2025-06-18")
        for params in ({}, {"protocolVersion": 3}, []):
            response = handle(self.repo, message("initialize", params))
            self.assertEqual(response["error"]["code"], -32602)

    def test_oversized_line_is_drained_and_cannot_smuggle_a_request(self):
        smuggled = json.dumps(message("tools/call", {
            "name": "read_lines",
            "arguments": {"path": "app.py", "start": 1, "end": 1}},
            identifier=77)).encode()
        payload = (b"X" * (MAX_LINE + 2 * 65_536) + smuggled + b"\n"
                   + json.dumps(message("ping", identifier=9)).encode() + b"\n")
        stdout = _Buffer()
        self.assertEqual(serve(self.root, stdin=io.BytesIO(payload), stdout=stdout), 0)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_surrogate_request_id_is_escaped(self):
        payload = (json.dumps(message("ping", identifier="\ud800")) + "\n").encode()
        stdout = _Buffer()
        self.assertEqual(serve(self.root, stdin=io.BytesIO(payload), stdout=stdout), 0)
        self.assertIn(b"\\ud800", stdout.data)
        response = json.loads(stdout.data)
        self.assertEqual(response["id"], "\ud800")

    def test_blank_transport_lines_are_ignored(self):
        payload = (b"\n\r\n \n"
                   + json.dumps(message("ping", identifier=7)).encode() + b"\n")
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(payload), stdout=stdout)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 7, "result": {}}])

    def test_batches_follow_the_negotiated_protocol(self):
        lines = [
            json.dumps(message("initialize", {"protocolVersion": "2024-11-05"}, 1)),
            json.dumps([message("ping", identifier=2),
                        message("ping", identifier=3)]),
            json.dumps(message("initialize", {"protocolVersion": "2025-06-18"}, 4)),
            json.dumps([message("ping", identifier=5)]),
            json.dumps(message("ping", identifier=6)),
        ]
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(("\n".join(lines) + "\n").encode()),
              stdout=stdout)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual([item["id"] for item in responses[1]], [2, 3])
        self.assertEqual(responses[2]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[3]["error"]["code"], -32600)
        self.assertEqual(responses[4]["id"], 6)

    def test_a_batch_can_initialize_an_older_client(self):
        lines = [
            json.dumps([message("initialize", {"protocolVersion": "2024-11-05"}, 1),
                        message("ping", identifier=2),
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}]),
            json.dumps(message("ping", identifier=3)),
            json.dumps([message("ping", identifier=4),
                        message("ping", identifier=5)]),
            json.dumps([]),
        ]
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(("\n".join(lines) + "\n").encode()),
              stdout=stdout)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual([item["id"] for item in responses[0]], [1, 2])
        self.assertEqual(responses[0][0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(responses[1]["id"], 3)
        self.assertEqual([item["id"] for item in responses[2]], [4, 5])
        self.assertEqual(responses[3]["error"]["code"], -32600)

    def test_a_broken_stdout_shuts_down_cleanly(self):
        payload = json.dumps(message("ping", identifier=1)).encode() + b"\n"
        self.assertEqual(serve(self.root, stdin=io.BytesIO(payload),
                               stdout=_Broken()), 0)

    def test_line_limit_counts_content_not_the_newline(self):
        prefix = b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"'
        suffix = b'"}'
        exact = prefix + b"x" * (MAX_LINE - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(exact), MAX_LINE)
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(exact + b"\n"), stdout=stdout)
        self.assertEqual(json.loads(stdout.data)["id"], 1)
        over = prefix + b"x" * (MAX_LINE - len(prefix) - len(suffix) + 1) + suffix
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(over + b"\n"), stdout=stdout)
        self.assertEqual(json.loads(stdout.data)["error"]["code"], -32600)

    def test_a_partial_final_line_is_parsed_at_eof(self):
        stdout = _Buffer()
        serve(self.root, stdin=io.BytesIO(json.dumps(message("ping", identifier=4)).encode()),
              stdout=stdout)
        self.assertEqual(json.loads(stdout.data)["id"], 4)

    def test_serve_reads_lines_and_reports_parse_errors(self):
        requests = [
            json.dumps(message("initialize", {"protocolVersion": "2025-06-18"})),
            "not json",
            json.dumps(message("tools/call", {
                "name": "list_files", "arguments": {"prefix": "", "offset": 0}},
                identifier="abc")),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(message("ping", identifier=9)),
        ]
        stdin = _Lines(requests)
        stdout = _Buffer()
        self.assertEqual(serve(self.root, stdin=stdin, stdout=stdout), 0)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(responses[2]["id"], "abc")
        self.assertIn("app.py", responses[2]["result"]["content"][0]["text"])
        self.assertEqual(responses[3], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_oversized_and_invalid_utf8_lines_are_rejected(self):
        stdin = _Lines([b"{" + b"x" * (MAX_LINE + 1) + b"}\n", b"\xff\n"])
        stdout = _Buffer()
        serve(self.root, stdin=stdin, stdout=stdout)
        responses = [json.loads(line) for line in stdout.data.splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["error"]["code"], -32700)

    def test_cli_prints_an_mcp_client_configuration(self):
        result = subprocess.run(
            [sys.executable, "-m", "nalar", "mcp-serve", "--print-config",
             "--repo", str(self.root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(result.stdout)["mcpServers"]["nalar"]
        self.assertEqual(entry["command"], "nalar")
        self.assertEqual(entry["args"],
                         ["mcp-serve", "--repo", str(self.root.resolve())])

    def test_print_config_rejects_a_non_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "nalar", "mcp-serve", "--print-config",
                 "--repo", tmp],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Cannot read Git snapshot", result.stderr)

    def test_cli_stdio_server_round_trip_and_no_writes(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "nalar", "mcp-serve", "--repo", str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(Path(__file__).resolve().parents[1]))
        try:
            for request in (message("initialize", {"protocolVersion": "2025-06-18"}),
                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            message("tools/call", {
                                "name": "read_lines",
                                "arguments": {"path": "app.py", "start": 1, "end": 2}},
                                identifier=3)):
                process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            first = json.loads(process.stdout.readline())
            second = json.loads(process.stdout.readline())
            self.assertEqual(first["result"]["serverInfo"]["name"], "nalar")
            self.assertEqual(second["id"], 3)
            self.assertIn("def load", second["result"]["content"][0]["text"])
        finally:
            process.stdin.close()
            process.wait(timeout=20)
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.root / "app.py").read_text(),
                         "def load(path):\n    return path\n\nclass Parser:\n    pass\n")


    def test_http_input_hardening_and_error_connections_close(self):
        port = self.start_http()
        deep = b"[" * 20_000 + b"]" * 20_000
        status, _, _ = self.post(port, None, raw=deep)
        self.assertEqual(status, 400)
        status, _, body = self.post(
            port, None, raw=b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)
        status, headers, _ = self.post(port, message("ping"), path="/other")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Connection"), "close")

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("POST", "/mcp")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "9" * 5000)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 411)
            response.read()
        finally:
            connection.close()

    def test_default_port_host_header_is_accepted(self):
        from types import SimpleNamespace

        from nalar.mcpserver import _HTTPHandler

        handler = _HTTPHandler.__new__(_HTTPHandler)
        handler.headers = {"Host": "127.0.0.1"}
        handler.server = SimpleNamespace(server_port=80)
        self.assertTrue(handler._host_ok())
        handler.headers = {"Host": "localhost"}
        self.assertTrue(handler._host_ok())
        handler.headers = {"Host": "127.0.0.1:8766"}
        self.assertFalse(handler._host_ok())
        handler.server.server_port = 8766
        self.assertTrue(handler._host_ok())
        handler.headers = {"Host": "127.0.0.1"}
        self.assertFalse(handler._host_ok())

    def test_stdio_deep_json_is_a_parse_error_not_a_crash(self):
        deep = b"[" * 20_000 + b"]" * 20_000
        stdin = io.BytesIO(deep + b"\n" +
                           b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        stdout = io.BytesIO()
        self.assertEqual(serve(self.root, stdin, stdout), 0)
        lines = stdout.getvalue().decode().splitlines()
        self.assertTrue(any("-32700" in line for line in lines))
        self.assertTrue(any('"id":1' in line and '"result"' in line
                            for line in lines))

class _Broken:
    def write(self, data):
        raise BrokenPipeError

    def flush(self):
        pass

    def fileno(self):
        raise AttributeError("no fd")


class _Lines:
    def __init__(self, items):
        self.items = []
        for item in items:
            data = item.encode() if isinstance(item, str) else item
            if not data.endswith(b"\n"):
                data += b"\n"
            self.items.append(data)

    def readline(self, limit=-1):
        return self.items.pop(0) if self.items else b""


class _Buffer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def flush(self):
        pass
