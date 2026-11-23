"""Bounded MCP clients: stdio processes and streamable HTTP.

Both transports share the same validation: 15 s timeout, 32 KB text results,
128 tools, bounded SSE/JSON payloads. HTTP endpoints must use TLS, except for
loopback addresses, so configured headers never travel in clear text.
"""

import json
import os
import select
import signal
import socket
import subprocess
import time
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urlparse

PROTOCOL = "2025-06-18"
MAX_LINE = 1_000_000
MAX_BODY = 4 * 1024 * 1024
MAX_RESULT = 32_000
MAX_TOOLS = 128
MAX_RESOURCES = 100
MAX_PROMPTS = 50
MAX_PAGES = 10
MAX_INSTRUCTIONS = 4_000
LISTING_BUDGET = 20.0
REQUEST_TIMEOUT = 15.0
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")


class MCPError(Exception):
    """An MCP server failure, safe to show to the model or user."""


def _clean(value, limit):
    return value if isinstance(value, str) and len(value) <= limit else None


def _safe(name):
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)
    return cleaned[:64] or "tool"


def validate_url(url):
    if not isinstance(url, str) or not 12 <= len(url) <= 1000:
        raise MCPError("invalid_url")
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise MCPError("invalid_url") from None
    if (parsed.scheme not in {"https", "http"} or not hostname
            or parsed.username or parsed.password or parsed.fragment
            or not url.isascii()
            or any(ord(char) < 0x20 or 0x7f <= ord(char) <= 0x9f or char == " "
                   for char in url)):
        raise MCPError("invalid_url")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise MCPError("invalid_url")
    return parsed


class _Client:
    def __init__(self, name, roots=()):
        self.name = name
        self.instructions = None
        self._closed = False
        self._next_id = 0
        self._listing_truncated = False
        self._roots = []
        for item in list(roots or ())[:8]:
            if isinstance(item, dict):
                uri = item.get("uri")
                label = item.get("name", "")
            else:
                try:
                    uri, label = item
                except (TypeError, ValueError):
                    continue
            if isinstance(uri, str) and 0 < len(uri) <= 2048:
                self._roots.append({"uri": uri, "name": str(label)[:200]})

    def _request(self, method, params, timeout=REQUEST_TIMEOUT):
        raise NotImplementedError

    def _notify(self, method, params=None):
        raise NotImplementedError

    def initialize(self):
        capabilities = {}
        if self._roots:
            capabilities["roots"] = {"listChanged": False}
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": capabilities,
            "clientInfo": {"name": "nalar", "version": "0.6"},
        })
        if not isinstance(result, dict):
            raise MCPError("server_protocol_error")
        raw = result.get("instructions")
        if isinstance(raw, str) and raw.strip():
            import re

            cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", raw)
            self.instructions = cleaned.strip()[:MAX_INSTRUCTIONS]
        self._notify("notifications/initialized")
        return result

    def _paginate(self, method, key):
        """Yield listing items with cursor, page and deadline guards."""
        cursor = None
        seen = set()
        yielded = 0
        self._listing_truncated = False
        deadline = time.monotonic() + LISTING_BUDGET
        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._listing_truncated = bool(cursor)
                break
            params = {"cursor": cursor} if cursor else {}
            try:
                result = self._request(method, params,
                                       timeout=max(1.0, min(REQUEST_TIMEOUT, remaining)))
            except MCPError:
                if yielded:
                    self._listing_truncated = True
                    return
                raise
            if not isinstance(result, dict) or not isinstance(result.get(key), list):
                if yielded:
                    self._listing_truncated = True
                    return
                raise MCPError("server_protocol_error")
            items = result[key]
            for item in items:
                yielded += 1
                yield item
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
            if cursor in seen:
                self._listing_truncated = True
                break
            seen.add(cursor)
            if not items:
                self._listing_truncated = True
                break
        else:
            self._listing_truncated = True

    @property
    def listing_truncated(self):
        return self._listing_truncated

    def tools(self):
        tools = []
        seen = False
        try:
            for tool in self._paginate("tools/list", "tools"):
                seen = True
                if not isinstance(tool, dict):
                    continue
                name = _clean(tool.get("name"), 64)
                description = tool.get("description", "")
                schema = tool.get("inputSchema", {"type": "object", "properties": {}})
                if not name or not isinstance(schema, dict) or not isinstance(description, str):
                    continue
                if len(tools) >= MAX_TOOLS:
                    self._listing_truncated = True
                    break
                tools.append({"type": "function", "function": {
                    "name": f"{self.name}__{_safe(name)}",
                    "description": description[:1024],
                    "parameters": schema,
                }})
        except MCPError as exc:
            if str(exc) == "server_error_-32601":
                raise MCPError("no_tools") from None
            raise
        if not tools:
            raise MCPError("no_valid_tools" if seen else "no_tools")
        return tools

    def prompts(self):
        found = []
        try:
            for item in self._paginate("prompts/list", "prompts"):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                description = item.get("description", "")
                arguments = item.get("arguments", [])
                if not isinstance(name, str) or not 0 < len(name) <= 128:
                    continue
                if not isinstance(description, str) or not isinstance(arguments, list):
                    continue
                parsed = []
                for argument in arguments[:20]:
                    if (isinstance(argument, dict) and isinstance(argument.get("name"), str)
                            and 0 < len(argument["name"]) <= 64):
                        parsed.append({"name": argument["name"],
                                       "description": str(argument.get("description", ""))[:200],
                                       "required": bool(argument.get("required", False))})
                if len(found) >= MAX_PROMPTS:
                    self._listing_truncated = True
                    break
                found.append({"name": name, "description": description[:500],
                              "arguments": parsed})
        except MCPError as exc:
            if str(exc) == "server_error_-32601":
                raise MCPError("no_prompts") from None
            raise
        if not found:
            raise MCPError("no_prompts")
        return found

    def get_prompt(self, name, arguments=None):
        if not isinstance(name, str) or not 0 < len(name) <= 128:
            raise MCPError("invalid_prompt")
        supplied = arguments or {}
        if (not isinstance(supplied, dict) or len(supplied) > 32
                or any(not isinstance(key, str) or not isinstance(value, str)
                       or len(key) > 64 or len(value) > 2_000 for key, value in supplied.items())):
            raise MCPError("invalid_arguments")
        result = self._request("prompts/get", {"name": name, "arguments": supplied})
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise MCPError("server_protocol_error")
        parts = []
        for message in result["messages"][:50]:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, dict) and content.get("type") == "text" \
                    and isinstance(content.get("text"), str):
                parts.append(content["text"])
        if not parts:
            raise MCPError("no_text_content")
        return "\n\n".join(parts)[:MAX_RESULT]

    def resources(self):
        found = []
        try:
            for item in self._paginate("resources/list", "resources"):
                uri = item.get("uri") if isinstance(item, dict) else None
                if not (isinstance(uri, str) and 0 < len(uri) <= 1024):
                    continue
                if len(found) >= MAX_RESOURCES:
                    self._listing_truncated = True
                    break
                found.append({"uri": uri, "name": str(item.get("name", ""))[:200],
                              "description": str(item.get("description", ""))[:500]})
        except MCPError as exc:
            if str(exc) == "server_error_-32601":
                raise MCPError("no_resources") from None
            raise
        if not found:
            raise MCPError("no_resources")
        return found

    def resource_templates(self):
        found = []
        try:
            for item in self._paginate("resources/templates/list", "resourceTemplates"):
                if not isinstance(item, dict):
                    continue
                template = item.get("uriTemplate")
                if not (isinstance(template, str) and 0 < len(template) <= 2048):
                    continue
                if len(found) >= MAX_RESOURCES:
                    self._listing_truncated = True
                    break
                found.append({"uriTemplate": template,
                              "name": str(item.get("name", ""))[:200],
                              "description": str(item.get("description", ""))[:500]})
        except MCPError as exc:
            if str(exc) == "server_error_-32601":
                raise MCPError("no_templates") from None
            raise
        if not found:
            raise MCPError("no_templates")
        return found

    def read_resource(self, uri):
        if not isinstance(uri, str) or not 0 < len(uri) <= 1024:
            raise MCPError("invalid_uri")
        result = self._request("resources/read", {"uri": uri})
        if not isinstance(result, dict) or not isinstance(result.get("contents"), list):
            raise MCPError("server_protocol_error")
        parts = [item.get("text") for item in result["contents"]
                 if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if not parts:
            raise MCPError("no_text_content")
        return "\n".join(parts)[:MAX_RESULT]

    def call(self, name, arguments):
        prefix = f"{self.name}__"
        if self._closed or not name.startswith(prefix):
            raise MCPError("unknown_tool")
        result = self._request("tools/call", {"name": name[len(prefix):],
                                              "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError("server_protocol_error")
        content = result.get("content", [])
        if not isinstance(content, list):
            raise MCPError("server_protocol_error")
        parts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"]
        text = "\n".join(parts)[:MAX_RESULT]
        if not text:
            structured = result.get("structuredContent")
            if isinstance(structured, (dict, list)):
                text = json.dumps(structured, ensure_ascii=True,
                                  sort_keys=True)[:MAX_RESULT]
        if result.get("isError"):
            raise MCPError(text or "server_tool_error")
        return text

    def close(self):
        self._closed = True


class MCPProcess(_Client):
    def __init__(self, name, command, env, timeout=REQUEST_TIMEOUT, roots=()):
        super().__init__(name, roots)
        self._timeout = timeout
        self.env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
        self.env.update(env)
        self._buffer = b""
        try:
            self.process = subprocess.Popen(
                list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.env, close_fds=True,
                bufsize=0, start_new_session=True)
        except (OSError, ValueError):
            raise MCPError("server_failed_to_start") from None
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
        except (OSError, ValueError):
            pass

    def _send(self, message, deadline=None):
        payload = (json.dumps(message, ensure_ascii=True) + "\n").encode()
        limit = time.monotonic() + self._timeout
        if deadline is None or deadline > limit:
            deadline = limit
        view = memoryview(payload)
        try:
            fd = self.process.stdin.fileno()
        except (OSError, ValueError):
            raise MCPError("server_disconnected") from None
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError("server_timeout")
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                try:
                    _, ready, _ = select.select([], [fd], [], remaining)
                except (OSError, ValueError):
                    raise MCPError("server_disconnected") from None
                if not ready:
                    raise MCPError("server_timeout")
                continue
            except (OSError, ValueError):
                raise MCPError("server_disconnected") from None
            view = view[written:]

    def _readline(self, deadline):
        # An internal buffer is required: buffered readers can hold a complete
        # line while select() reports the pipe as idle, which would deadlock.
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                return line + b"\n"
            if len(self._buffer) > MAX_LINE:
                raise MCPError("server_response_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError("server_timeout")
            try:
                ready, _, _ = select.select([self.process.stdout], [], [], remaining)
                if not ready:
                    raise MCPError("server_timeout")
                chunk = os.read(self.process.stdout.fileno(), 65_536)
            except (OSError, ValueError):
                raise MCPError("server_disconnected") from None
            if not chunk:
                raise MCPError("server_disconnected")
            self._buffer += chunk

    def _answer(self, message, deadline=None):
        """Answer a server request; unknown methods get JSON-RPC -32601."""
        identifier = message.get("id")
        if identifier is None:
            return
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            return
        method = message.get("method")
        if method == "ping":
            response = {"jsonrpc": "2.0", "id": identifier, "result": {}}
        elif method == "roots/list":
            response = {"jsonrpc": "2.0", "id": identifier,
                        "result": {"roots": list(self._roots)}}
        else:
            response = {"jsonrpc": "2.0", "id": identifier,
                        "error": {"code": -32601, "message": "Method not found"}}
        self._send(response, deadline)

    def _request(self, method, params, timeout=None):
        if self._closed:
            raise MCPError("server_closed")
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        while True:
            try:
                message = json.loads(self._readline(deadline))
            except ValueError:
                continue
            except RecursionError:
                raise MCPError("server_protocol_error") from None
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("method"), str):
                self._answer(message, deadline)
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise MCPError(f"server_error_{code}" if isinstance(code, int) else "server_error")
            if "result" not in message:
                raise MCPError("server_protocol_error")
            return message["result"]

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        if self._closed:
            return
        super().close()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            group = os.getpgid(self.process.pid)
        except (OSError, ProcessLookupError):
            group = None
        try:
            if group is not None:
                os.killpg(group, signal.SIGTERM)
            else:
                self.process.terminate()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if group is not None:
                    os.killpg(group, signal.SIGKILL)
                else:
                    self.process.kill()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


class MCPHttp(_Client):
    def __init__(self, name, url, headers=None, timeout=REQUEST_TIMEOUT):
        super().__init__(name)
        self._url = validate_url(url)
        self._timeout = timeout
        self._headers = {"Accept": "application/json, text/event-stream",
                         "User-Agent": "nalar-agent/0.6"}
        for header, value in (headers or {}).items():
            if (not isinstance(header, str) or not isinstance(value, str)
                    or not 0 < len(header) <= 64 or len(value) > 512
                    or header.lower() in {"host", "content-length", "transfer-encoding"}
                    or any(ord(char) < 0x20 or ord(char) == 0x7f
                           for char in value)):
                raise MCPError("invalid_header")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise MCPError("invalid_header") from None
            self._headers[header] = value
        self._session = None

    def _connection(self):
        if self._url.scheme == "https":
            return HTTPSConnection(self._url.hostname, self._url.port, timeout=self._timeout)
        return HTTPConnection(self._url.hostname, self._url.port, timeout=self._timeout)

    def _path(self):
        path = self._url.path or "/"
        return f"{path}?{self._url.query}" if self._url.query else path

    @staticmethod
    def _remaining(deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise MCPError("server_timeout")
        return max(left, 0.05)

    def _waiting(self, connection, deadline, response=None):
        remaining = self._remaining(deadline)
        sock = connection.sock
        if response is not None:
            raw = getattr(getattr(getattr(response, "fp", None), "raw", None),
                          "_sock", None)
            if raw is not None:
                sock = raw
        if sock is not None:
            try:
                sock.settimeout(min(float(self._timeout), remaining))
            except OSError:
                pass

    def _headers_for_request(self):
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        headers["MCP-Protocol-Version"] = PROTOCOL
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    def _body(self, connection, response, deadline):
        chunks = []
        total = 0
        reader = getattr(response, "read1", response.read)
        while True:
            self._waiting(connection, deadline, response)
            chunk = reader(65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY:
                raise MCPError("server_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _sse(self, connection, response, request_id, deadline):
        buffer = b""
        data = []
        total = 0
        reader = getattr(response, "read1", response.read)
        while True:
            self._waiting(connection, deadline, response)
            chunk = reader(65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY:
                raise MCPError("server_response_too_large")
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                text = raw.decode("utf-8", "replace").rstrip("\r")
                if not text:
                    if not data:
                        continue
                    payload = "\n".join(data)
                    data.clear()
                    try:
                        message = json.loads(payload)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return self._result(message, request_id)
                    continue
                if text.startswith("data:"):
                    value = text[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    data.append(value)
        raise MCPError("server_protocol_error")

    def _request(self, method, params, timeout=None):
        if self._closed:
            raise MCPError("server_closed")
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        self._next_id += 1
        request_id = self._next_id
        connection = self._connection()
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                                  "method": method, "params": params}).encode()
            connection.request("POST", self._path(), payload, self._headers_for_request())
            self._waiting(connection, deadline)
            response = connection.getresponse()
            session = response.getheader("Mcp-Session-Id")
            if isinstance(session, str) and 0 < len(session) <= 256:
                self._session = session
            if response.status != 200:
                raise MCPError(f"http_{response.status}")
            content_type = (response.getheader("Content-Type") or "").lower()
            if content_type.startswith("text/event-stream"):
                return self._sse(connection, response, request_id, deadline)
            raw = self._body(connection, response, deadline)
            try:
                message = json.loads(raw)
            except (ValueError, RecursionError, UnicodeError):
                raise MCPError("server_protocol_error") from None
            return self._result(message, request_id)
        except MCPError:
            raise
        except (TimeoutError, socket.timeout):
            raise MCPError("server_timeout") from None
        except (UnicodeEncodeError, ValueError):
            raise MCPError("invalid_header") from None
        except (OSError, HTTPException):
            raise MCPError("server_disconnected") from None
        finally:
            connection.close()

    @staticmethod
    def _result(message, request_id):
        if not isinstance(message, dict) or message.get("id") != request_id:
            raise MCPError("server_protocol_error")
        if "error" in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise MCPError(f"server_error_{code}" if isinstance(code, int) else "server_error")
        if "result" not in message:
            raise MCPError("server_protocol_error")
        return message["result"]

    def _notify(self, method, params=None):
        if self._closed:
            raise MCPError("server_closed")
        deadline = time.monotonic() + self._timeout
        connection = self._connection()
        try:
            payload = json.dumps({"jsonrpc": "2.0", "method": method,
                                  "params": params or {}}).encode()
            connection.request("POST", self._path(), payload, self._headers_for_request())
            self._waiting(connection, deadline)
            response = connection.getresponse()
            if response.status not in {200, 202, 204}:
                raise MCPError(f"http_{response.status}")
            length = response.getheader("Content-Length")
            if response.status == 200 and length and length != "0":
                try:
                    self._body(connection, response, deadline)
                except (MCPError, TimeoutError, socket.timeout, OSError):
                    pass
        except MCPError:
            raise
        except (TimeoutError, socket.timeout):
            raise MCPError("server_timeout") from None
        except (OSError, HTTPException):
            raise MCPError("server_disconnected") from None
        finally:
            connection.close()


def make_client(server, roots=()):
    """Build the transport named by a validated MCPServer config entry."""
    timeout = getattr(server, "timeout", REQUEST_TIMEOUT)
    url = getattr(server, "url", None)
    if url:
        return MCPHttp(server.name, url, getattr(server, "headers", {}), timeout=timeout)
    return MCPProcess(server.name, server.command, server.env, timeout=timeout,
                      roots=roots)
