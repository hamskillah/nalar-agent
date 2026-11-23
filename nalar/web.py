"""Single-user browser interface for the Nalar harness."""

import html
import json
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from threading import Lock
from urllib.parse import parse_qs
from uuid import uuid4

from .core import MODEL_ID, run
from .workspace import MAX_BYTES, NAME, ToolError, Workspace

ASSETS = Path(__file__).with_name("assets")
RUN_PATH = re.compile(r"/runs/([a-f0-9]{32})(?:/files/([A-Za-z0-9][A-Za-z0-9_.-]{0,79}))?")
MODEL = MODEL_ID
MAX_REQUEST = 128 * 1024
STATUS = {
    "ok": "Respons selesai", "tool_error": "Tool gagal", "provider_error": "Provider gagal",
    "empty_response": "Respons kosong", "call_limit": "Batas tool tercapai",
    "turn_limit": "Batas putaran tercapai", "token_limit": "Batas token tercapai",
    "cancelled": "Dibatalkan",
}


def _stored(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _status(value):
    if value is None:
        return "Status tidak tersedia"
    return STATUS.get(value, "Selesai") if isinstance(value, str) else "Tidak terbaca"


def _number(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def esc(value):
    return html.escape(str(value))


def page(content):
    return Template((ASSETS / "index.html").read_text(encoding="utf-8")).substitute(content=content)


def make_server(state, *, port=8765, chat_factory=None):
    """Serve on IPv4 loopback only; `state` must be a private local directory."""
    state = Path(state).absolute()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_mode & 0o077:
        raise ValueError("Direktori data harus privat (izin 0700) dan bukan symlink.")
    workspaces = state / "workspaces"
    workspaces.mkdir(mode=0o700, exist_ok=True)
    if workspaces.is_symlink() or workspaces.stat().st_mode & 0o077:
        raise ValueError("Direktori workspace harus privat dan bukan symlink.")
    db = state / "runs.sqlite"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, prompt TEXT NOT NULL, model TEXT NOT NULL,
            mode TEXT NOT NULL, result TEXT NOT NULL
        )""")
    csrf = secrets.token_urlsafe(32)
    active = Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_):
            pass

        @property
        def authority(self):
            return f"127.0.0.1:{self.server.server_port}"

        def send(self, status, body=b"", content_type="text/plain; charset=utf-8", **headers):
            if isinstance(body, str):
                body = body.encode("utf-8", "replace")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; script-src 'self'; "
                             "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, message):
            body = (f'<section class="panel problem"><p class="eyebrow">Permintaan tidak selesai</p>'
                    f'<h1>{esc(message)}</h1><a class="button" href="/">Kembali ke tugas</a></section>')
            self.send(status, page(body), "text/html; charset=utf-8")

        def check_host(self):
            if self.headers.get_all("Host") != [self.authority]:
                self.send(403, "Host tidak diizinkan.")
                return False
            return True

        def do_GET(self):
            if not self.check_host():
                return
            assets = {"/app.css": ("app.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
            if self.path in assets:
                name, content_type = assets[self.path]
                self.send(200, (ASSETS / name).read_bytes(), content_type)
                return
            if self.path == "/":
                with closing(sqlite3.connect(db)) as conn:
                    rows = conn.execute("SELECT id, prompt, model, result FROM runs "
                                        "ORDER BY rowid DESC LIMIT 30").fetchall()
                try:
                    body = page(home(csrf, chat_factory is not None, rows))
                except Exception:
                    return self.error(500, "Riwayat tugas tidak dapat dibaca.")
                self.send(200, body, "text/html; charset=utf-8")
                return
            match = RUN_PATH.fullmatch(self.path)
            if not match:
                return self.error(404, "Halaman tidak ditemukan.")
            run_id, filename = match.groups()
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute("SELECT prompt, model, mode, result FROM runs WHERE id = ?",
                                   (run_id,)).fetchone()
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            if filename:
                try:
                    with Workspace(workspaces / run_id) as workspace:
                        text = workspace.call("read", {"name": filename})
                except (OSError, ToolError):
                    return self.error(404, "File tidak tersedia.")
                return self.send(200, text, Content_Disposition=f'attachment; filename="{filename}"')
            try:
                with Workspace(workspaces / run_id) as workspace:
                    files = workspace.call("list", {})
            except (OSError, ToolError):
                files = None
            try:
                rendered = detail(run_id, row, files)
                if rendered is None:
                    return self.error(500, "Riwayat tugas tidak dapat dibaca.")
                body = page(rendered)
            except Exception:
                return self.error(500, "Riwayat tugas tidak dapat dibaca.")
            self.send(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            if not self.check_host():
                return
            if self.path != "/runs":
                return self.error(404, "Halaman tidak ditemukan.")
            if self.headers.get("Origin") != f"http://{self.authority}":
                return self.error(403, "Asal permintaan tidak diizinkan.")
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.error(415, "Format form tidak didukung.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError
                fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True,
                                  strict_parsing=True, errors="strict", max_num_fields=6)
                if any(len(values) != 1 for values in fields.values()):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self.error(400, "Form tidak valid atau terlalu besar.")
            if not secrets.compare_digest(fields.get("csrf", [""])[0].encode(), csrf.encode()):
                return self.error(403, "Muat ulang halaman sebelum menjalankan tugas.")
            prompt, model, mode, name, text = (
                fields.get(key, [""])[0] for key in ("prompt", "model", "mode", "input_name", "input_text")
            )
            if (not prompt.strip() or len(prompt) > 6000 or not MODEL.fullmatch(model)
                    or mode not in {"recover", "baseline"}
                    or (name or text) and not NAME.fullmatch(name)
                    or len(text.encode()) > MAX_BYTES):
                return self.error(400, "Periksa tugas, model, dan file masukan.")
            if chat_factory is None:
                return self.error(503, "Atur NALAR_API_KEY di server.")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            try:
                run_id = uuid4().hex
                folder = workspaces / run_id
                chat = chat_factory(model)
                folder.mkdir(mode=0o700)
                with Workspace(folder) as workspace:
                    if name:
                        workspace.call("create", {"name": name, "text": text})
                    result = run(chat, workspace, prompt, mode=mode)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                                 (run_id, prompt, model, mode, json.dumps(asdict(result))))
            except (OSError, ValueError, sqlite3.Error, ToolError):
                return self.error(500, "Tugas tidak dapat disimpan atau dijalankan.")
            except Exception:
                return self.error(500, "Tugas tidak dapat disimpan atau dijalankan.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def home(csrf, ready, rows):
    connection = ("Key tersedia · the default provider" if ready else "Key belum diatur")
    notice = ("" if ready else
              '<p class="notice">Atur <code>NALAR_API_KEY</code> di terminal dan mulai ulang server.</p>')
    items = []
    for run_id, prompt, model, result in rows:
        parsed = _stored(result)
        status = _status(parsed.get("status")) if parsed is not None else "Tidak terbaca"
        items.append(f'<li><a href="/runs/{esc(run_id)}"><strong>{esc(prompt[:90])}</strong>'
                     f'<span>{esc(model)} · {esc(status)}</span></a></li>')
    history = "".join(items)
    return f"""
    <div class="intro"><div><p class="eyebrow">01 / RUANG KERJA</p>
    <h1>Tugas baru<span>.</span></h1><p>Beri instruksi dan lihat langkah yang diambil agent.</p></div>
    <span class="pill">{connection}</span></div>
    <div class="board">
      <section class="panel">
        <div class="panel-top"><h2>Instruksi</h2><span class="chip">AGENT RUN</span></div>
        {notice}
        <form action="/runs" method="post" aria-describedby="privacy">
          <input type="hidden" name="csrf" value="{csrf}">
          <label for="prompt">Tugas untuk Nalar</label>
          <textarea id="prompt" name="prompt" rows="4" maxlength="6000" required
            placeholder="Contoh: Baca catatan, lalu buat rangkuman di ringkasan.txt."></textarea>
          <div class="fields">
            <div><label for="model">Model the default provider</label>
            <input id="model" name="model" value="beam-free" required maxlength="96"
              pattern="[A-Za-z0-9@][A-Za-z0-9_.:/@\\-]{{0,95}}" spellcheck="false" autocapitalize="none"></div>
            <div><label for="mode">Mode eksekusi</label>
            <select id="mode" name="mode"><option value="recover">Recovery</option>
              <option value="baseline">Baseline</option></select></div>
          </div>
          <details><summary>Tambahkan file masukan <span>Opsional</span></summary>
            <label for="input-name">Nama file</label>
            <input id="input-name" name="input_name" maxlength="80"
              pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]{{0,79}}" placeholder="catatan.txt">
            <label for="input-text">Isi file</label>
            <textarea id="input-text" name="input_text" rows="3" maxlength="16384"></textarea>
          </details>
          <p id="privacy" class="muted">Instruksi dan hasil tool yang dibaca model dikirim ke the default provider.
          File dan riwayat tugas tersimpan lokal.</p>
          <button type="submit">Jalankan tugas <span aria-hidden="true">↗</span></button>
          <p id="pending" role="status"></p>
        </form>
      </section>
      <aside class="panel"><div class="panel-top"><h2>Riwayat</h2><span class="chip">{len(rows):02d}</span></div>
      {f'<ul class="history">{history}</ul>' if history else
        '<div class="empty"><span aria-hidden="true">↳</span><strong>Belum ada tugas</strong>'
        '<p>Riwayat muncul setelah Nalar menjalankan tugas pertamanya.</p></div>'}
      <div class="limits"><h3>Batas akses</h3><p>File teks saja. Tidak ada akses shell atau web.</p>
      <div><code>list</code> <code>read</code> <code>create</code> <code>search</code>
      <code>replace</code> <code>delete</code> <code>rename</code></div></div></aside>
    </div>"""


def detail(run_id, row, files):
    prompt, model, mode, raw = row
    result = _stored(raw)
    if result is None:
        return None
    raw_trace = result.get("trace")
    trace = "".join(
        f'<li><code>{esc(step.get("tool", "provider"))}</code>'
        f'<span>Putaran {esc(step.get("turn", "?"))} · {esc(step.get("status", "?"))}</span></li>'
        for step in (raw_trace if isinstance(raw_trace, list) else [])
        if isinstance(step, dict))
    links = "".join(f'<li><a href="/runs/{esc(run_id)}/files/{esc(name)}" download>'
                    f'{esc(name)}</a></li>' for name in files or ())
    tokens = result.get("tokens")
    tokens = tokens if not isinstance(tokens, bool) and isinstance(tokens, int) else "—"
    text = result.get("text")
    text = text if isinstance(text, str) and text else "Tidak ada respons akhir."
    turns = _number(result.get("turns"))
    calls = _number(result.get("calls"))
    elapsed = _number(result.get("elapsed_ms"))
    return f"""
    <a class="back" href="/">← Tugas baru</a>
    <div class="intro"><div><p class="eyebrow">02 / EKSEKUSI</p><h1>Hasil tugas<span>.</span></h1>
      <p>{esc(model)} · {esc(mode)}</p></div><span class="pill">{esc(_status(result.get("status")))}</span></div>
    <div class="board"><section class="panel">
      <div class="panel-top"><h2>Respons agent</h2><span class="chip">OUTPUT</span></div>
      <p class="eyebrow">INSTRUKSI</p><p class="prose">{esc(prompt)}</p>
      <p class="eyebrow">RESPONS</p><p class="prose">{esc(text)}</p>
      <dl class="metrics"><div><dt>Putaran</dt><dd>{esc(turns)}</dd></div>
       <div><dt>Tool call</dt><dd>{esc(calls)}</dd></div>
       <div><dt>Token</dt><dd>{esc(tokens)}</dd></div>
       <div><dt>Durasi</dt><dd>{elapsed / 1000:.1f} dtk</dd></div></dl>
      <p class="muted">Respons selesai tidak menjamin isi file benar. Periksa hasil sebelum dipakai.</p>
    </section><aside class="panel"><div class="panel-top"><h2>Jejak tool</h2><span class="chip">TRACE</span></div>
    {f'<ol class="history trace">{trace}</ol>' if trace else '<p class="muted">Tidak ada panggilan tool.</p>'}
    <div class="limits"><h3>File workspace</h3>
    {f'<ul class="files">{links}</ul>' if links else
      ('<p>File tidak dapat dimuat.</p>' if files is None else '<p>Tidak ada file.</p>')}</div></aside>
    </div>"""
