import http.client
import re
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

from nalar.core import Call, Reply
from nalar.web import MAX_REQUEST, make_server


class WebRenderTests(unittest.TestCase):
    def test_corrupt_history_rows_do_not_break_rendering(self):
        from nalar.web import detail, home

        listing = home("csrf", True, [("a" * 32, "task", "model", "{bad")])
        self.assertIn("Tidak terbaca", listing)
        self.assertIsNone(detail("a" * 32, ("task", "model", "recover", "{bad"), []))
        rendered = detail("a" * 32, (
            "task", "model", "recover",
            '{"status": "cancelled", "text": "", "turns": 0, "calls": 0, '
            '"tokens": null, "elapsed_ms": 5, "trace": []}'), [])
        self.assertIn("Dibatalkan", rendered)
        for raw in ('{"status":"ok","trace":null}', '{"status":"ok","trace":5}',
                    '{"status":"ok"}', '{"status":"ok","turns":"two"}',
                    '{"status":["ok"]}', '{"turns": 1}'):
            with self.subTest(raw=raw):
                self.assertIsInstance(home("csrf", True,
                                           [("a" * 32, "task", "model", raw)]), str)
                page = detail("a" * 32, ("task", "model", "recover", raw), [])
                self.assertIsInstance(page, str)
                self.assertIn("Tidak ada respons", page)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def start(self, factory):
        server = self.enterContext(make_server(self.state, port=0, chat_factory=factory))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_port

    def request(self, method, path, *, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request(method, path, body=data, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def submit(self, fields, origin=None):
        _, _, page = self.request("GET", "/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        return self.request("POST", "/runs", data=urlencode({"csrf": csrf, **fields}), headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin or f"http://127.0.0.1:{self.port}",
        })

    def test_run_persists_trace_and_serves_attachment(self):
        def factory(model):
            self.assertEqual(model, "beam-free")
            replies = iter([
                Reply(calls=(Call("read", {"name": "in.txt"}),)),
                Reply(calls=(Call("create", {"name": "out.txt", "text": "hasil"}),)),
                Reply(text="<script>untrusted</script>"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({
            "prompt": "Buat rangkuman", "model": "beam-free", "mode": "recover",
            "input_name": "in.txt", "input_text": "masukan",
        })
        self.assertEqual(status, 303)
        path = headers["Location"]
        status, _, page = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;untrusted", page)
        self.assertIn("out.txt", page)
        self.assertIn("read", page)
        status, headers, body = self.request("GET", path + "/files/out.txt")
        self.assertEqual((status, body), (200, "hasil"))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.start(None)
        self.assertIn("Buat rangkuman", self.request("GET", "/")[2])
        self.assertEqual(self.request("GET", path)[0], 200)

    def test_workspace_edit_tools_run_through_web(self):
        def factory(model):
            replies = iter([
                Reply(calls=(Call("replace", {"name": "in.txt", "before": "old",
                                             "after": "new"}),)),
                Reply(text="edited"),
            ])
            return lambda *_: next(replies)

        self.start(factory)
        status, headers, _ = self.submit({
            "prompt": "Edit the input", "model": "beam-free", "mode": "recover",
            "input_name": "in.txt", "input_text": "old\n",
        })
        self.assertEqual(status, 303)
        location = headers["Location"]
        status, _, page = self.request("GET", location)
        self.assertEqual(status, 200)
        self.assertIn("in.txt", page)
        status, _, body = self.request("GET", location + "/files/in.txt")
        self.assertEqual((status, body), (200, "new\n"))

    def test_rejects_bad_forms_and_unknown_attachments(self):
        self.start(None)
        _, _, page = self.request("GET", "/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        origin = f"http://127.0.0.1:{self.port}"
        form = {"prompt": "task", "model": "beam-free", "mode": "recover"}
        missing = self.request("POST", "/runs", data=urlencode(form), headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
        self.assertEqual(missing[0], 403)
        wrong_type = self.request("POST", "/runs", data=urlencode({**form, "csrf": csrf}),
                                  headers={"Content-Type": "text/plain", "Origin": origin})
        self.assertEqual(wrong_type[0], 415)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.putrequest("POST", "/runs")
            conn.putheader("Origin", origin)
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(MAX_REQUEST + 1))
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()
        self.assertEqual(self.request("GET", "/runs/" + "a" * 32)[0], 404)
        self.assertEqual(self.request("GET", "/runs/" + "a" * 32 + "/files/../x")[0], 404)

    def test_rejects_unknown_host_and_cross_origin(self):
        self.start(lambda _: self.fail("must not contact model"))
        self.assertEqual(self.request("GET", "/", headers={"Host": "untrusted.test"})[0], 403)
        fields = {"prompt": "task", "model": "beam-free", "mode": "recover"}
        self.assertEqual(self.submit(fields, origin="https://untrusted.test")[0], 403)
        self.assertEqual(self.submit({**fields, "input_name": "../outside"})[0], 400)

    def test_unexpected_run_errors_return_500_and_release_the_lock(self):
        self.start(lambda model: (lambda *_: Reply(text="ok")))
        with patch("nalar.web.run", side_effect=RuntimeError("boom")):
            status, _, _ = self.submit({"prompt": "task", "model": "beam-free",
                                        "mode": "recover"})
            self.assertEqual(status, 500)
        status, _, _ = self.submit({"prompt": "task", "model": "beam-free",
                                    "mode": "recover"})
        self.assertEqual(status, 303)

    def test_a_lone_surrogate_does_not_drop_the_detail_page(self):
        self.start(lambda model: (lambda *_: Reply(text="bad \ud800 text")))
        status, headers, _ = self.submit({"prompt": "task", "model": "beam-free",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        status, _, page = self.request("GET", headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn("bad", page)

    def test_a_detail_rendering_failure_returns_the_error_page(self):
        self.start(lambda model: (lambda *_: Reply(text="ok")))
        status, headers, _ = self.submit({"prompt": "task", "model": "beam-free",
                                          "mode": "recover"})
        self.assertEqual(status, 303)
        with patch("nalar.web.detail", side_effect=RuntimeError("boom")):
            status, _, page = self.request("GET", headers["Location"])
        self.assertEqual(status, 500)
        self.assertIn("tidak dapat dibaca", page)

    def test_without_key_never_runs(self):
        self.start(None)
        self.assertIn("NALAR_API_KEY", self.request("GET", "/")[2])
        self.assertEqual(self.submit({"prompt": "task", "model": "beam-free", "mode": "baseline"})[0], 503)
