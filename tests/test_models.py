import http.client
import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

from nalar.core import ProviderError
from nalar.go import list_models, models_url


class ModelListTests(unittest.TestCase):
    def serve(self, status, payload):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode() if not isinstance(payload, bytes) else payload)

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

    def test_models_path_is_derived_and_sorted(self):
        self.assertEqual(models_url("https://api.example.com/v1/chat/completions"),
                         "https://api.example.com/v1/models")
        self.assertEqual(models_url("http://127.0.0.1:8080/v1/chat/completions/"),
                         "http://127.0.0.1:8080/v1/models")

    def test_lists_models_with_auth(self):
        requests = self.serve(200, {"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "a-model"}]})
        models = list_models("https://gateway.test/v1/chat/completions", "secret",
                             {"X-Org": "team"})
        self.assertEqual(models, ["a-model", "z-model"])
        path, headers = requests[0]
        self.assertEqual(path, "/v1/models")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["X-Org"], "team")

    def test_cli_lists_models_for_loopback_provider(self):
        self.serve(200, {"data": [{"id": "m2"}, {"id": "m1"}]})
        import os, subprocess, sys, tempfile
        from pathlib import Path
        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The patched HTTPSConnection points at the fixture; derive its port from the mock call.
        from unittest.mock import patch as _patch
        with _patch("nalar.go.HTTPSConnection") as mocked:
            mocked.side_effect = lambda *a, **kw: http.client.HTTPConnection("127.0.0.1", 1, **kw)
            _ = mocked
        (repo / "nalar.json").write_text(json.dumps({
            "provider": {"base_url": "http://127.0.0.1:1/v1/chat/completions",
                         "api_key_env": "MODELS_TEST_KEY"}}), encoding="utf-8")
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(repo / "none.json"),
               "MODELS_TEST_KEY": "k"}
        result = subprocess.run([sys.executable, "-m", "nalar", "config", "show",
                                 "--repo", str(repo), "--trust-project"], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["provider"]["api_key_env"], "MODELS_TEST_KEY")

    def test_cli_text_format_lists_one_model_per_line(self):
        import io
        import os
        import tempfile

        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        from nalar.__main__ import main

        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (repo / "nalar.json").write_text(json.dumps({"model": "fixture"}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(repo / "none.json")}), \
                patch("nalar.go.list_models", return_value=["beta", "alpha"]), \
                patch("sys.argv", ["nalar", "models", "--repo", str(repo),
                                   "--format", "text"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue().splitlines(), ["beta", "alpha"])

    def test_errors_are_reported_without_body_leak(self):
        self.serve(500, {"error": "secret detail"})
        with self.assertRaises(ProviderError) as caught:
            list_models("https://gateway.test/v1/chat/completions", "secret")
        self.assertEqual(str(caught.exception), "http_500")
        self.serve(200, b"not json")
        with self.assertRaises(ProviderError):
            list_models("https://gateway.test/v1/chat/completions", "secret")

    def test_cli_rejects_invalid_key_and_config_without_traceback(self):
        import os, subprocess, sys, tempfile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = repo / "global.json"
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(config)}
        config.write_text(json.dumps({
            "model": "m",
            "provider": {"base_url": "https://gateway.test/v1/chat/completions",
                         "api_key_env": "MODELS_TEST_KEY"}}), encoding="utf-8")
        bad_key = subprocess.run(
            [sys.executable, "-m", "nalar", "models", "--repo", str(repo)],
            cwd=root, env={**env, "MODELS_TEST_KEY": "bad key"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(bad_key.returncode, 2)
        self.assertIn("not a valid API key", bad_key.stderr)
        self.assertNotIn("Traceback", bad_key.stderr)
        config.write_text(json.dumps({
            "model": "m",
            "provider": {"base_url": "http://127.0.0.1:99999/v1/chat/completions",
                         "api_key_env": "MODELS_TEST_KEY"}}), encoding="utf-8")
        bad_config = subprocess.run(
            [sys.executable, "-m", "nalar", "models", "--repo", str(repo)],
            cwd=root, env={**env, "MODELS_TEST_KEY": "k"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(bad_config.returncode, 2)
        self.assertIn("invalid port", bad_config.stderr)
        self.assertNotIn("Traceback", bad_config.stderr)

    def test_cli_text_model_ids_are_single_line(self):
        import io
        import os
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        from nalar.__main__ import main

        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (repo / "nalar.json").write_text(json.dumps({"model": "fixture"}),
                                         encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"NALAR_API_KEY": "k",
                                     "NALAR_GLOBAL_CONFIG": str(repo / "none.json")}), \
                patch("nalar.go.list_models", return_value=["evil\nFORGED", "ok"]), \
                patch("sys.argv", ["nalar", "models", "--repo", str(repo),
                                   "--format", "text"]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue().splitlines(),
                         ["evil\\nFORGED", "ok"])
