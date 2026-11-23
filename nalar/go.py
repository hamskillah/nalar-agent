"""OpenAI-compatible Chat Completions adapter."""

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from .core import MODEL_ID, Call, ProviderError, Reply

PATH = "/inference/go/v1/chat/completions"
DEFAULT_BASE = "http://127.0.0.1:11434/v1/chat/completions"
MAX_RESPONSE = 512 * 1024
MAX_STREAM = 4 * 1024 * 1024


JSON_HEADERS = {"Content-Type": "application/json", "User-Agent": "nalar-agent/0.5"}


def validate_base_url(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 500:
        raise ValueError("provider base_url must be an http(s) URL")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("provider base_url has an invalid host") from None
    try:
        parsed.port
    except ValueError:
        raise ValueError("provider base_url has an invalid port") from None
    if (parsed.scheme not in {"https", "http"} or not hostname
            or parsed.fragment or parsed.username or parsed.password
            or re.search(r"[\x00-\x1f\x7f]", value) or not value.isascii()):
        raise ValueError("provider base_url must be an http(s) URL")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("plain http is only allowed for loopback providers")
    return parsed


def models_url(base_url):
    parsed = validate_base_url(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    path = path + "/models"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def list_models(base_url, api_key, extra_headers=None, timeout=20):
    if not api_key or not re.fullmatch(r"[!-~]{1,4096}", api_key):
        raise ValueError("set the provider API key in the configured environment variable")
    url = urlparse(models_url(base_url))
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "nalar-agent/0.5"}
    for name, value in (extra_headers or {}).items():
        if isinstance(name, str) and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", name):
            headers[name] = value
    connection = (HTTPSConnection(url.hostname, url.port, timeout=timeout)
                  if url.scheme == "https" else HTTPConnection(url.hostname, url.port, timeout=timeout))
    try:
        connection.request("GET", url.path or "/", None, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderError(f"http_{response.status}")
        raw = response.read(MAX_RESPONSE + 1)
    except TimeoutError:
        raise ProviderError("request_timeout") from None
    except (OSError, HTTPException):
        raise ProviderError("transport_unavailable") from None
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE:
        raise ProviderError("response_too_large")
    try:
        doc = json.loads(raw)
        items = doc["data"]
        if not isinstance(items, list):
            raise ValueError
        models = sorted({item["id"] for item in items
                         if isinstance(item, dict) and isinstance(item.get("id"), str)
                         and 0 < len(item["id"]) <= 200})
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ProviderError("invalid_response") from None
    if not models:
        raise ProviderError("no_models")
    return models[:500]


class FallbackChat:
    """Try alternate models when the provider reports the model as missing."""

    def __init__(self, models, factory):
        self._models = tuple(models)
        self._factory = factory
        self._index = 0
        self._last_missing = None

    def _try(self, operation):
        """Use the first working model; advance only on a missing-model error."""
        last = None
        while self._index < len(self._models):
            try:
                result = operation(self._factory(self._models[self._index]))
            except ProviderError as exc:
                if str(exc) != "http_404":
                    raise
                last = exc
                self._last_missing = exc
                self._index += 1
                continue
            return result
        raise self._last_missing or last or ProviderError("no_models")

    @staticmethod
    def _accepts(client, name):
        import inspect

        try:
            return name in inspect.signature(client).parameters
        except (TypeError, ValueError):
            return False

    def __call__(self, messages, tools, cancel=None):
        def operation(client):
            if cancel is not None and self._accepts(client, "cancel"):
                return client(messages, tools, cancel=cancel)
            return client(messages, tools)

        return self._try(operation)

    def stream(self, messages, tools, on_text=None, cancel=None):
        def operation(client):
            kwargs = {"on_text": on_text}
            if cancel is not None and self._accepts(client, "cancel"):
                kwargs["cancel"] = cancel
            return client.stream(messages, tools, **kwargs)

        return self._try(operation)


class ChatCompletions:
    """OpenAI-compatible chat completions; the default provider is the default endpoint."""

    def __init__(self, model, api_key, *, base_url=DEFAULT_BASE, extra_headers=None,
                 session=True, session_header="x-nalar-session",
                 max_tokens=1536, temperature=0.0, timeout=60.0):
        if not MODEL_ID.fullmatch(model):
            raise ValueError("use the provider's model ID (letters, digits, . _ : / @ -)")
        if not api_key or not re.fullmatch(r"[!-~]{1,4096}", api_key):
            raise ValueError("set the provider API key in the configured environment variable")
        self.model = model
        self._key = api_key
        self._session = None
        self._session_enabled = bool(session)
        if (not isinstance(session_header, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", session_header)
                or session_header.lower() in {"host", "content-length", "transfer-encoding"}):
            raise ValueError("invalid session header name")
        self._session_header = session_header
        if type(max_tokens) is not int or not 1 <= max_tokens <= 32_768:
            raise ValueError("max_tokens must be between 1 and 32768")
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not 0 <= temperature <= 2):
            raise ValueError("temperature must be between 0 and 2")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 1 <= timeout <= 600):
            raise ValueError("timeout must be between 1 and 600 seconds")
        self._max_tokens = max_tokens
        self._temperature = float(temperature)
        self._timeout = float(timeout)
        self._url = validate_base_url(base_url)
        self._extra = {}
        for name, value in (extra_headers or {}).items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name):
                raise ValueError("invalid provider header name")
            if (not isinstance(value, str) or not 0 <= len(value) <= 512
                    or re.search(r"[\x00-\x1f\x7f]", value)):
                raise ValueError("invalid provider header value")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise ValueError("invalid provider header value") from None
            if name.lower() in {"host", "content-length", "transfer-encoding"}:
                raise ValueError("provider may not override transport headers")
            self._extra[name] = value

    def _connection(self):
        port = self._url.port
        if self._url.scheme == "https":
            return HTTPSConnection(self._url.hostname, port, timeout=self._timeout)
        return HTTPConnection(self._url.hostname, port, timeout=self._timeout)

    def _path(self):
        path = self._url.path or "/"
        return f"{path}?{self._url.query}" if self._url.query else path

    def __call__(self, messages, tools, cancel=None):
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled")
        body = json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
            "stream": False, "temperature": self._temperature, "max_tokens": self._max_tokens,
        }).encode()
        headers = self._headers(messages)
        connection = self._connection()
        try:
            connection.request("POST", self._path(), body, headers)
            response = connection.getresponse()
            if response.status != 200:
                response.read(8192)
                raise ProviderError(f"http_{response.status}", _retry_after(response))
            raw = response.read(MAX_RESPONSE + 1)
        except TimeoutError:
            raise ProviderError("request_timeout") from None
        except (OSError, HTTPException):
            raise ProviderError("transport_unavailable") from None
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE:
            raise ProviderError("response_too_large")
        return _decode(raw)

    def _headers(self, messages=None):
        headers = {"Authorization": f"Bearer {self._key}", **JSON_HEADERS, **self._extra}
        if self._session_enabled:
            if self._session is None or (messages is not None and len(messages) == 2):
                self._session = uuid4().hex
            headers[self._session_header] = self._session
        return headers

    def stream(self, messages, tools, on_text=None, cancel=None):
        """Consume server-sent events; text deltas arrive through on_text."""
        body = json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
            "stream": True, "stream_options": {"include_usage": True},
            "temperature": self._temperature, "max_tokens": self._max_tokens,
        }).encode()
        connection = self._connection()
        text, reasoning, tokens = [], [], None
        calls = {}
        finish = None
        try:
            connection.request("POST", self._path(), body, self._headers(messages))
            response = connection.getresponse()
            if response.status != 200:
                response.read(8192)
                raise ProviderError(f"http_{response.status}", _retry_after(response))
            total = 0
            for raw in response:
                if cancel is not None and cancel.is_set():
                    raise ProviderError("cancelled")
                total += len(raw)
                if total > MAX_STREAM:
                    raise ProviderError("response_too_large")
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                usage = chunk.get("usage")
                if (isinstance(usage, dict)
                        and type(usage.get("total_tokens")) is int
                        and usage["total_tokens"] >= 0):
                    tokens = usage["total_tokens"]
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    continue
                reason = choices[0].get("finish_reason")
                if isinstance(reason, str):
                    finish = reason
                delta = choices[0].get("delta")
                if not isinstance(delta, dict):
                    continue
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    text.append(piece)
                    if on_text is not None:
                        on_text(piece)
                thought = delta.get("reasoning_content")
                if isinstance(thought, str) and thought:
                    reasoning.append(thought)
                for item in delta.get("tool_calls") or ():
                    if not isinstance(item, dict) or type(item.get("index")) is not int:
                        continue
                    slot = calls.setdefault(item["index"], {"id": None, "name": "", "arguments": ""})
                    if isinstance(item.get("id"), str):
                        slot["id"] = item["id"]
                    function = item.get("function")
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str) and function["name"]:
                            # Some compatible servers repeat the full name in
                            # every delta; keep the first and concatenate only
                            # argument fragments.
                            if not slot["name"]:
                                slot["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            slot["arguments"] += function["arguments"]
        except TimeoutError:
            raise ProviderError("request_timeout") from None
        except (OSError, HTTPException):
            raise ProviderError("transport_unavailable") from None
        finally:
            connection.close()
        if finish in {"length", "content_filter"}:
            raise ProviderError("incomplete_response")
        assembled = []
        for index in sorted(calls):
            slot = calls[index]
            try:
                arguments = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except (ValueError, RecursionError):
                arguments = None
            assembled.append(Call(slot["name"], arguments if isinstance(arguments, dict) else None,
                                  slot["id"], slot["arguments"]))
        return Reply("".join(text), tuple(assembled), tokens, "".join(reasoning) or None)


def _retry_after(response):
    if response.status not in (429, 503):
        return None
    header = response.getheader("Retry-After")
    if not isinstance(header, str) or not header.strip():
        return None
    header = header.strip()
    if header.isdigit():
        seconds = int(header)
    else:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    return min(seconds, 60)


def _decode(raw):
    try:
        doc = json.loads(raw)
        choice = doc["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ProviderError("incomplete_response")
        message = choice["message"]
        text = message.get("content")
        reasoning = message.get("reasoning_content")
        raw_calls = message.get("tool_calls") or []
        if (text is not None and not isinstance(text, str)
                or reasoning is not None and not isinstance(reasoning, str)
                or not isinstance(raw_calls, list)):
            raise ValueError
        calls = []
        ids = set()
        for call in raw_calls:
            call_id = call["id"]
            function = call["function"]
            name, raw_args = function["name"], function["arguments"]
            if (call["type"] != "function" or not isinstance(call_id, str) or not call_id
                    or call_id in ids or not isinstance(name, str) or not isinstance(raw_args, str)):
                raise ValueError
            ids.add(call_id)
            try:
                args = json.loads(raw_args)
            except (ValueError, RecursionError):
                args = None
            calls.append(Call(name, args if isinstance(args, dict) else None, call_id, raw_args))
        usage = doc.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            tokens = None
        return Reply(text or "", tuple(calls), tokens, reasoning)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError):
        raise ProviderError("invalid_response") from None
