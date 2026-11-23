import http.client
import json
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from nalar.config import ConfigError, load_config
from nalar.core import ProviderError, Reply
from nalar.go import DEFAULT_BASE, FallbackChat, ChatCompletions


class ProviderWireTests(unittest.TestCase):
    def serve(self, response):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                 dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_):
                pass

        server = self.enterContext(HTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.enterContext(patch("nalar.go.HTTPSConnection",
                                side_effect=lambda *a, **kw: http.client.HTTPConnection(
                                    "127.0.0.1", server.server_port, **kw)))
        return requests

    def test_custom_base_url_and_headers_are_used(self):
        requests = self.serve({"choices": [{"message": {"content": "ok"}}]})
        client = ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions",
                            extra_headers={"X-Organisation": "team"}, session=False,
                            max_tokens=128, temperature=0.2)
        reply = client([{"role": "user", "content": "hi"}], [])
        self.assertEqual(reply.text, "ok")
        path, payload, headers = requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["model"], "m")
        self.assertEqual((payload["max_tokens"], payload["temperature"]), (128, 0.2))
        self.assertEqual(headers["X-Organisation"], "team")
        self.assertNotIn("x-nalar-session", headers)

    def test_retry_after_is_reported_on_429(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(429)
                self.send_header("Retry-After", "7")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = self.enterContext(HTTPServer(("127.0.0.1", 0), Handler))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with patch("nalar.go.HTTPSConnection",
                   side_effect=lambda *a, **kw: http.client.HTTPConnection(
                       "127.0.0.1", server.server_port, **kw)):
            from nalar.core import ProviderError
            with self.assertRaises(ProviderError) as caught:
                ChatCompletions("m", "k", base_url="https://gateway.example/v1/chat/completions")([{"role": "user", "content": "x"}], [])
        self.assertEqual(str(caught.exception), "http_429")
        self.assertEqual(caught.exception.retry_after, 7)

    def test_model_ids_may_carry_a_vendor_prefix(self):
        for model in ("openai/gpt-5", "@cf/meta/llama", "vendor/model:free"):
            with self.subTest(model=model):
                self.assertEqual(ChatCompletions(model, "fixture-key", base_url="https://gateway.example/v1/chat/completions").model, model)
        with self.assertRaises(ValueError):
            ChatCompletions("bad model!", "fixture-key")

    def test_fallback_chat_tries_alternate_models_only_for_404(self):

        calls = []

        class Client:
            def __init__(self, name):
                self.name = name

            def __call__(self, messages, tools, cancel=None):
                calls.append(self.name)
                if self.name == "missing":
                    raise ProviderError("http_404")
                if self.name == "broken":
                    raise ProviderError("http_500")
                return Reply(text="ok")

            def stream(self, messages, tools, on_text=None, cancel=None):
                calls.append(self.name)
                if self.name == "missing":
                    raise ProviderError("http_404")
                on_text("ok")
                return Reply(text="ok")

        chat = FallbackChat(["missing", "good"], Client)
        self.assertEqual(chat([], []).text, "ok")
        self.assertEqual(calls, ["missing", "good"])
        with self.assertRaises(ProviderError) as caught:
            FallbackChat(["broken", "good"], Client)([], [])
        self.assertEqual(str(caught.exception), "http_500")
        calls.clear()
        pieces = []
        self.assertEqual(chat.stream([], [], on_text=pieces.append).text, "ok")
        self.assertEqual(pieces, ["ok"])

    def test_fallback_chat_sticks_to_the_working_model(self):

        calls = []
        seen = {"second": 0}

        class Client:
            def __init__(self, name):
                self.name = name

            def __call__(self, messages, tools, cancel=None):
                calls.append(self.name)
                if self.name == "missing":
                    raise ProviderError("http_404")
                if self.name == "second":
                    seen["second"] += 1
                    if seen["second"] > 2:
                        raise ProviderError("http_404")
                return Reply(text="ok")

        chat = FallbackChat(["missing", "second", "third"], Client)
        self.assertEqual(chat([], []).text, "ok")
        self.assertEqual(calls, ["missing", "second"])

        calls.clear()
        self.assertEqual(chat([], []).text, "ok")
        self.assertEqual(calls, ["second"])

        calls.clear()
        self.assertEqual(chat([], []).text, "ok")
        self.assertEqual(calls, ["second", "third"])

        calls.clear()
        self.assertEqual(chat([], []).text, "ok")
        self.assertEqual(calls, ["third"])

    def test_exhausted_fallback_reports_the_missing_model(self):

        def client(name):
            def call(messages, tools, cancel=None):
                raise ProviderError("http_404")

            return call

        chat = FallbackChat(["missing"], client)
        for _ in range(2):
            with self.assertRaises(ProviderError) as caught:
                chat([], [])
            self.assertEqual(str(caught.exception), "http_404")

    def test_client_wraps_fallback_models(self):
        from nalar.__main__ import _client
        from nalar.config import Provider
        from nalar.go import FallbackChat

        with patch("nalar.__main__.ChatCompletions") as mocked:
            client = _client(Provider(), "primary", ("backup",))
        self.assertIsInstance(client, FallbackChat)
        self.assertFalse(mocked.called)

    def test_plain_http_is_limited_to_loopback(self):
        for url in ("http://example.com/v1/chat/completions", "ftp://example.com/x", "not-a-url"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChatCompletions("m", "k", base_url=url)
        ChatCompletions("m", "k", base_url="http://127.0.0.1:8080/v1/chat/completions")


class ProviderConfigTests(unittest.TestCase):
    def test_fallback_models_may_carry_a_vendor_prefix(self):
        self.write({"fallback_models": ["vendor/model", "vendor/other:free"]})
        self.assertEqual(self.load().fallback_models, ("vendor/model", "vendor/other:free"))


    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, value):
        (self.root / "nalar.json").write_text(json.dumps(value), encoding="utf-8")

    def load(self):
        return load_config(self.root, self.root / "absent.json", trust_project=True)

    def test_provider_defaults(self):
        provider = self.load().provider
        self.assertEqual((provider.base_url, provider.api_key_env, provider.session_header),
                         (DEFAULT_BASE, "NALAR_API_KEY", True))

    def test_provider_overrides_are_validated(self):
        self.write({"provider": {"base_url": "https://gateway.example/v1/chat/completions",
                                 "api_key_env": "GATEWAY_KEY",
                                 "headers": {"X-Org": "{env:ORG_NAME}"},
                                 "session_header": False}})
        import os
        with patch.dict(os.environ, {"ORG_NAME": "team"}):
            provider = self.load().provider
        self.assertEqual((provider.base_url, provider.api_key_env, provider.session_header),
                         ("https://gateway.example/v1/chat/completions", "GATEWAY_KEY", False))
        self.assertEqual(provider.headers, (("X-Org", "team"),))

    def test_project_provider_requires_explicit_trust(self):
        self.write({"provider": {"base_url": "https://attacker.example/v1/chat/completions"}})
        with self.assertRaises(ConfigError):
            load_config(self.root, self.root / "absent.json")

    def test_provider_tuning_is_parsed_and_validated(self):
        self.write({"provider": {"max_tokens": 256, "temperature": 0.4, "timeout": 12}})
        provider = self.load().provider
        self.assertEqual((provider.max_tokens, provider.temperature, provider.timeout),
                         (256, 0.4, 12.0))
        for value in ({"max_tokens": 0}, {"max_tokens": 40_000}, {"temperature": 3},
                      {"timeout": 0}, {"timeout": "fast"}, {"max_tokens": True}):
            with self.subTest(value=value):
                self.write({"provider": value})
                with self.assertRaises(ConfigError):
                    self.load()

    def test_fallback_models_config_is_validated(self):
        self.write({"fallback_models": ["a", "b", "a"]})
        provider = self.load().fallback_models
        self.assertEqual(provider, ("a", "b"))
        for value in ({"fallback_models": "a"}, {"fallback_models": ["bad id"]},
                      {"fallback_models": ["a"] * 6}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    self.load()
        self.write({"fallback_models": ["a"]})
        with self.assertRaises(ConfigError):
            load_config(self.root, self.root / "absent.json")

    def test_provider_rejects_unsafe_values(self):
        cases = [
            {"provider": {"base_url": "http://example.com/x"}},
            {"provider": {"api_key_env": "not a variable"}},
            {"provider": {"headers": {"Bad Header": "x"}}},
            {"provider": {"headers": {"X": "y"}, "surprise": 1}},
            {"provider": "https://example.com"},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ConfigError):
                    self.load()

    def test_direct_headers_and_base_url_are_latin1_safe(self):
        for headers in ({"X-Note": "a\u20acb"}, {"X-Note": "\ud800"},
                        {"X-Note": "bad\nvalue"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                ChatCompletions("m", "k", extra_headers=headers)
        for url in ("https://user:pw@example.test/v1/chat/completions",
                    "https://example.test/v1/chat/completions\nX: 1",
                    "http://127.0.0.1:99999/v1/chat/completions"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChatCompletions("m", "k", base_url=url)

    def test_non_stream_non_dict_usage_is_ignored(self):
        from nalar.go import _decode

        reply = _decode('{"choices":[{"message":{"content":"hi"}}],"usage":[]}')
        self.assertEqual((reply.text, reply.tokens), ("hi", None))

    def test_session_header_can_be_named_for_a_gateway(self):
        client = ChatCompletions("m", "k",
                                 base_url="https://gateway.example/v1/chat/completions",
                                 session_header="x-provider-session")
        headers = client._headers([{"role": "user", "content": "x"}])
        self.assertIn("x-provider-session", headers)
        self.assertNotIn("x-nalar-session", headers)
