"""Single-user browser workspace for reviewing and applying coding patches."""

import json
import os
import re
import secrets
import shlex
import sqlite3
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .coding import Repository, apply_reviewed, solve
from .secrets import scan_diff
from .web import (ASSETS, MAX_REQUEST, MODEL, _number, _status, _stored, esc,
                  page)

RUN_PATH = re.compile(
    r"/runs/([a-f0-9]{32})(/apply|/patch|/review|/revert|/fix)?")
MAX_PROMPT = 6_000


def make_server(state, repo, *, port=8765, chat_factory=None,
                review_factory=None, review_model=None,
                default_model="beam-free", limits=None, verify_commands=(),
                secret_env=(), verify_timeout=300):
    """Serve coding proposals on IPv4 loopback only."""
    state = Path(state).absolute()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_mode & 0o077:
        raise ValueError("Direktori data harus privat (izin 0700) dan bukan symlink.")
    root = Path(repo).resolve()
    probe = Repository(root)
    root = Path(os.fsdecode(
        probe._git("rev-parse", "--show-toplevel").removesuffix(b"\n"))).resolve()
    Repository(root)  # validate the repository and its Git snapshot
    db = state / "coding.sqlite"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS coding_runs (
            id TEXT PRIMARY KEY, prompt TEXT NOT NULL, model TEXT NOT NULL,
            mode TEXT NOT NULL, base TEXT NOT NULL, result TEXT NOT NULL,
            patch TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
            verification TEXT, review TEXT, repo TEXT)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(coding_runs)")}
        if "verification" not in columns:
            conn.execute("ALTER TABLE coding_runs ADD COLUMN verification TEXT")
        if "review" not in columns:
            conn.execute("ALTER TABLE coding_runs ADD COLUMN review TEXT")
        if "repo" not in columns:
            conn.execute("ALTER TABLE coding_runs ADD COLUMN repo TEXT")
    csrf = secrets.token_urlsafe(32)
    active = Lock()
    limits = limits or {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_):
            pass

        @property
        def authority(self):
            return f"127.0.0.1:{self.server.server_port}"

        def send(self, status, body=b"", content_type="text/plain; charset=utf-8",
                 **headers):
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
            body = (f'<section class="panel problem"><p class="eyebrow">Permintaan '
                    f'tidak selesai</p><h1>{esc(message)}</h1>'
                    f'<a class="button" href="/">Kembali ke tugas</a></section>')
            self.send(status, page(body), "text/html; charset=utf-8")

        def check_host(self):
            if self.headers.get_all("Host") != [self.authority]:
                self.send(403, "Host tidak diizinkan.")
                return False
            return True

        def do_GET(self):
            if not self.check_host():
                return
            path = urlsplit(self.path).path
            assets = {"/app.css": ("app.css", "text/css"),
                      "/app.js": ("app.js", "text/javascript")}
            if path in assets:
                name, content_type = assets[path]
                self.send(200, (ASSETS / name).read_bytes(), content_type)
                return
            if path == "/":
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                query = query.replace("\x00", "").strip()[:100]
                try:
                    with closing(sqlite3.connect(db)) as conn:
                        sql = ("SELECT id, prompt, model, result, applied, review, "
                               "verification FROM coding_runs WHERE repo = ?")
                        params = [str(root)]
                        if query:
                            sql += (" AND (instr(lower(prompt), lower(?)) > 0"
                                    " OR instr(lower(model), lower(?)) > 0)")
                            params.extend([query, query])
                        sql += " ORDER BY rowid DESC LIMIT 30"
                        rows = conn.execute(sql, params).fetchall()
                    body = page(home(csrf, chat_factory is not None, rows,
                                     default_model, root, query))
                except sqlite3.Error:
                    return self.error(500, "Riwayat tugas tidak dapat dibaca.")
                except Exception:
                    return self.error(500, "Riwayat tugas tidak dapat dibaca.")
                self.send(200, body, "text/html; charset=utf-8")
                return
            match = RUN_PATH.fullmatch(path)
            if not match:
                return self.error(404, "Halaman tidak ditemukan.")
            if match.group(2) == "/patch":
                try:
                    with closing(sqlite3.connect(db)) as conn:
                        row = conn.execute("SELECT patch FROM coding_runs "
                                           "WHERE id = ? AND repo = ?",
                                           (match.group(1), str(root))).fetchone()
                except sqlite3.Error:
                    return self.error(500, "Patch tidak dapat dibaca.")
                if row is None or not row[0]:
                    return self.error(404, "Patch tidak tersedia.")
                return self.send(
                    200, row[0], "text/x-diff; charset=utf-8",
                    Content_Disposition=f'attachment; filename="nalar-{match.group(1)}.patch"')
            if match.group(2):
                return self.error(404, "Halaman tidak ditemukan.")
            try:
                with closing(sqlite3.connect(db)) as conn:
                    row = conn.execute(
                        "SELECT prompt, model, mode, base, result, patch, applied, "
                        "verification, review FROM coding_runs "
                        "WHERE id = ? AND repo = ?",
                        (match.group(1), str(root))).fetchone()
            except sqlite3.Error:
                return self.error(500, "Riwayat tugas tidak dapat dibaca.")
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            try:
                current = Repository(root).commit.decode()
                verification = _stored(row[7]) if row[7] else None
                base_changed = row[3] != current
                can_fix = (bool(chat_factory) and bool(verify_commands)
                           and verification is not None
                           and not verification.get("ok")
                           and not base_changed)
                body = page(detail(match.group(1), row, csrf,
                                   verify_commands,
                                   can_review=bool(chat_factory),
                                   can_fix=can_fix,
                                   base_changed=base_changed))
            except Exception:
                return self.error(500, "Riwayat tugas tidak dapat dibaca.")
            self.send(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            if not self.check_host():
                return
            path = urlsplit(self.path).path
            if self.headers.get("Origin") != f"http://{self.authority}":
                return self.error(403, "Asal permintaan tidak diizinkan.")
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.error(415, "Format form tidak didukung.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError
                fields = parse_qs(self.rfile.read(length).decode(),
                                  keep_blank_values=True, strict_parsing=True,
                                  errors="strict", max_num_fields=6)
                if any(len(values) != 1 for values in fields.values()):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self.error(400, "Form tidak valid atau terlalu besar.")
            if not secrets.compare_digest(fields.get("csrf", [""])[0].encode(),
                                          csrf.encode()):
                return self.error(403, "Muat ulang halaman sebelum memproses tugas.")
            apply_match = RUN_PATH.fullmatch(path)
            if apply_match and apply_match.group(2) == "/apply":
                return self.apply(apply_match.group(1),
                                  fields.get("verify", [""])[0] == "1",
                                  fields.get("allow_secrets", [""])[0] == "1")
            if apply_match and apply_match.group(2) == "/review":
                return self.review(apply_match.group(1))
            if apply_match and apply_match.group(2) == "/revert":
                return self.revert(apply_match.group(1))
            if apply_match and apply_match.group(2) == "/fix":
                return self.fix(apply_match.group(1))
            if path != "/runs":
                return self.error(404, "Halaman tidak ditemukan.")
            prompt = fields.get("prompt", [""])[0]
            model = fields.get("model", [""])[0]
            mode = fields.get("mode", [""])[0]
            if (not prompt.strip() or len(prompt) > MAX_PROMPT
                    or not MODEL.fullmatch(model)
                    or mode not in {"recover", "baseline"}):
                return self.error(400, "Periksa tugas, model, dan mode.")
            if chat_factory is None and review_factory is None:
                return self.error(503, "Atur NALAR_API_KEY di server.")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            try:
                repository = Repository(root)
                chat = chat_factory(model)
                result, _ = solve(
                    chat, repository, prompt, mode=mode,
                    max_turns=limits.get("max_turns", 12),
                    max_calls=limits.get("max_calls", 24),
                    context_budget=limits.get("context_budget", 120_000),
                    token_budget=limits.get("token_budget", 0),
                    retries=limits.get("retries", 2))
                patch = repository.patch() if result.status == "ok" else ""
                run_id = uuid4().hex
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "INSERT INTO coding_runs "
                        "(id, prompt, model, mode, base, result, patch, applied, "
                        "verification, review, repo) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)",
                        (run_id, prompt, model, mode,
                         repository.commit.decode(),
                         json.dumps(asdict(result)), patch, str(root)))
            except (OSError, ValueError, sqlite3.Error):
                return self.error(500, "Tugas tidak dapat disimpan atau dijalankan.")
            except Exception:
                return self.error(500, "Tugas tidak dapat disimpan atau dijalankan.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

        def review(self, run_id):
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "SELECT model, patch FROM coding_runs WHERE id = ? AND repo = ?",
                    (run_id, str(root))).fetchone()
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            model, patch = row
            if not patch:
                return self.error(409, "Tidak ada patch untuk direview.")
            if chat_factory is None and review_factory is None:
                return self.error(503, "Atur NALAR_API_KEY di server.")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            try:
                from .review import review as run_review

                from .agent import Budget
                from .review import MAX_DIFF

                if len(patch) > MAX_DIFF:
                    return self.error(413, "Patch terlalu besar untuk direview.")
                reviewer_model = review_model or model
                reviewer = (review_factory() if review_factory is not None
                            else chat_factory(model))
                outcome = run_review(
                    reviewer, patch, model=reviewer_model,
                    retries=limits.get("retries", 2),
                    budget=Budget(limits.get("token_budget", 0)))
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("UPDATE coding_runs SET review = ? WHERE id = ?",
                                 (json.dumps(outcome), run_id))
            except Exception:
                return self.error(502, "Patch tidak dapat direview.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

        def revert(self, run_id):
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "SELECT base, patch, applied FROM coding_runs "
                    "WHERE id = ? AND repo = ?", (run_id, str(root))).fetchone()
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            base, patch, applied = row
            if not applied or not patch:
                return self.send(303, Location=f"/runs/{run_id}")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            try:
                repository = Repository(root, base)
                apply_reviewed(repository, patch, reverse=True)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "UPDATE coding_runs SET applied = 0, verification = NULL "
                        "WHERE id = ?", (run_id,))
            except ValueError as exc:
                return self.error(409, str(exc))
            except (OSError, sqlite3.Error):
                return self.error(500, "Patch tidak dapat dibatalkan.")
            except Exception:
                return self.error(500, "Patch tidak dapat dibatalkan.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

        def fix(self, run_id):
            if chat_factory is None:
                return self.error(503, "Atur NALAR_API_KEY di server.")
            if not verify_commands:
                return self.error(409, "Atur verify commands di nalar.json dulu.")
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "SELECT patch, verification FROM coding_runs "
                    "WHERE id = ? AND repo = ?", (run_id, str(root))).fetchone()
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            patch, raw_verification = row
            verification = _stored(raw_verification) if raw_verification else None
            if not patch or verification is None or verification.get("ok"):
                return self.send(303, Location=f"/runs/{run_id}")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            repository = None
            reverted = False
            try:
                with closing(sqlite3.connect(db)) as conn:
                    current = conn.execute(
                        "SELECT base, patch, applied, model, verification, review "
                        "FROM coding_runs WHERE id = ? AND repo = ?",
                        (run_id, str(root))).fetchone()
                if current is None:
                    return self.error(404, "Riwayat tugas tidak ditemukan.")
                base, patch, applied, model, raw_verification, raw_review = current
                verification = _stored(raw_verification) if raw_verification else None
                if not patch or verification is None or verification.get("ok"):
                    return self.send(303, Location=f"/runs/{run_id}")
                try:
                    from types import SimpleNamespace

                    from .apply import patch_paths
                    from .fix import run_fix

                    repository = Repository(root, base)
                    if applied:
                        apply_reviewed(repository, patch, reverse=True,
                                       allowed_files=patch_paths(patch))
                        reverted = True
                        with closing(sqlite3.connect(db)) as conn, conn:
                            conn.execute(
                                "UPDATE coding_runs SET applied = 0, "
                                "verification = NULL, review = NULL WHERE id = ?",
                                (run_id,))
                    config = SimpleNamespace(
                        verify=tuple(verify_commands),
                        provider=SimpleNamespace(
                            api_key_env=(secret_env[0] if secret_env
                                         else "NALAR_API_KEY")),
                        max_turns=limits.get("max_turns", 12),
                        max_calls=limits.get("max_calls", 24),
                        context_budget=limits.get("context_budget", 120_000),
                        token_budget=limits.get("token_budget", 0),
                        retries=limits.get("retries", 2))
                    payload = run_fix(
                        repository, chat_factory(model or default_model),
                        config, attempts=1, timeout=verify_timeout)
                    new_patch = payload.get("patch") or ""
                    if not new_patch:
                        raise ValueError("model tidak mengusulkan patch")
                except Exception as exc:
                    restored = False
                    if reverted and repository is not None:
                        try:
                            apply_reviewed(repository, patch, allow_secrets=True)
                        except Exception:
                            pass
                        else:
                            restored = True
                            try:
                                with closing(sqlite3.connect(db)) as conn, conn:
                                    conn.execute(
                                        "UPDATE coding_runs SET applied = 1, "
                                        "verification = ?, review = ? WHERE id = ?",
                                        (raw_verification, raw_review, run_id))
                            except sqlite3.Error:
                                pass
                    if restored:
                        message = ("Perbaikan gagal; patch semula diterapkan kembali: "
                                   + str(exc))
                    elif reverted:
                        message = ("Perbaikan gagal; patch semula dibatalkan. "
                                   "Pulihkan manual: " + str(exc))
                    else:
                        message = "Perbaikan gagal: " + str(exc)
                    return self.error(409, message[:300])
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "UPDATE coding_runs SET patch = ?, applied = 0, "
                        "verification = ?, review = NULL WHERE id = ?",
                        (new_patch, json.dumps(payload.get("verification")),
                         run_id))
            except (OSError, sqlite3.Error):
                return self.error(500, "Perbaikan tidak dapat disimpan.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

        def apply(self, run_id, verify=False, allow_secrets=False):
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "SELECT base, patch, applied FROM coding_runs "
                    "WHERE id = ? AND repo = ?", (run_id, str(root))).fetchone()
            if row is None:
                return self.error(404, "Riwayat tugas tidak ditemukan.")
            base, patch, applied = row
            if applied or not patch:
                return self.send(303, Location=f"/runs/{run_id}")
            if not allow_secrets and scan_diff(patch)["findings"]:
                return self.error(409, "Patch berisi pola kredensial; konfirmasi "
                                       "setelah memeriksanya.")
            if not active.acquire(blocking=False):
                return self.error(409, "Tunggu tugas sebelumnya selesai.")
            try:
                with closing(sqlite3.connect(db)) as conn:
                    current = conn.execute(
                        "SELECT applied, patch FROM coding_runs WHERE id = ? AND repo = ?",
                        (run_id, str(root))).fetchone()
                if current is None or current[0] or not current[1]:
                    return self.send(303, Location=f"/runs/{run_id}")
                repository = Repository(root, base)
                apply_reviewed(repository, patch, allow_secrets=allow_secrets)
                verification = None
                if verify and verify_commands:
                    from .preflight import PreflightError, verify_patch

                    try:
                        result = verify_patch(
                            repository, patch, verify_commands,
                            timeout=verify_timeout, secret_names=secret_env)
                    except PreflightError as exc:
                        result = {"ok": False, "error": str(exc), "commands": []}
                    verification = json.dumps(result)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "UPDATE coding_runs SET applied = 1, verification = ? "
                        "WHERE id = ?", (verification, run_id))
            except ValueError as exc:
                return self.error(409, str(exc))
            except (OSError, sqlite3.Error):
                return self.error(500, "Patch tidak dapat diterapkan.")
            except Exception:
                return self.error(500, "Patch tidak dapat diterapkan.")
            finally:
                active.release()
            self.send(303, Location=f"/runs/{run_id}")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def home(csrf, ready, rows, default_model, root, query=""):
    connection = ("Key tersedia · the default provider" if ready else "Key belum diatur")
    notice = ("" if ready else
              '<p class="notice">Atur <code>NALAR_API_KEY</code> di terminal '
              'dan mulai ulang server.</p>')
    items = []
    for run_id, prompt, model, result, applied, raw_review, raw_verify in rows:
        parsed = _stored(result)
        status = _status(parsed.get("status")) if parsed is not None else "Tidak terbaca"
        if applied:
            status = "Diterapkan"
        elif raw_review:
            review = _stored(raw_review)
            if review is not None and review.get("status") == "ok":
                status = "Direview"
        elif raw_verify:
            verification = _stored(raw_verify)
            if verification is not None:
                status = ("Verifikasi OK" if verification.get("ok")
                          else "Verifikasi gagal")
        items.append(f'<li><a href="/runs/{esc(run_id)}"><strong>{esc(prompt[:90])}'
                     f'</strong><span>{esc(model)} · {esc(status)}</span></a></li>')
    history = "".join(items)
    base = Repository(root).commit.decode()[:12]
    return f"""
    <div class="intro"><div><p class="eyebrow">01 / REPOSITORY</p>
    <h1>Patch baru<span>.</span></h1><p>Buat proposal, periksa diff, lalu terapkan
    dengan checks yang sama seperti CLI.</p></div>
    <span class="pill">{esc(root.name)} · {esc(base)}</span></div>
    <div class="board">
      <section class="panel">
        <div class="panel-top"><h2>Instruksi</h2><span class="chip">CODING RUN</span></div>
        {notice}
        <form action="/runs" method="post">
          <input type="hidden" name="csrf" value="{csrf}">
          <label for="prompt">Tugas untuk Nalar</label>
          <textarea id="prompt" name="prompt" rows="4" maxlength="{MAX_PROMPT}" required
            placeholder="Contoh: Perbaiki kasus daftar kosong dan tambahkan tes."></textarea>
          <div class="fields">
            <div><label for="model">Model</label>
            <input id="model" name="model" value="{esc(default_model)}" required
              maxlength="96" spellcheck="false" autocapitalize="none"></div>
            <div><label for="mode">Mode</label>
            <select id="mode" name="mode"><option value="recover">Recovery</option>
              <option value="baseline">Baseline</option></select></div>
          </div>
          <p class="muted">Tidak ada shell, commit otomatis, atau apply tanpa konfirmasi.
          Diff ditampilkan sebelum diterapkan.</p>
          <button type="submit">Jalankan tugas <span aria-hidden="true">↗</span></button>
          <p id="pending" role="status"></p>
        </form>
      </section>
      <aside class="panel"><div class="panel-top"><h2>Riwayat</h2>
      <span class="chip">{len(rows):02d}</span></div>
      <form action="/" method="get" class="fields" role="search">
        <div><label for="q">Cari</label>
        <input id="q" name="q" value="{esc(query)}" maxlength="100"
          placeholder="Tugas atau model"></div>
        <div><button type="submit">Cari</button></div>
      </form>
      {f'<p class="muted"><a href="/">Hapus filter</a></p>' if query else ''}
      {f'<ul class="history">{history}</ul>' if history else
        (f'<div class="empty"><strong>Tidak ada hasil</strong>'
         f'<span>{esc(query)}</span></div>' if query else
         '<div class="empty"><strong>Belum ada tugas</strong></div>')}
      <div class="limits"><h3>Batas</h3><p>Snapshot committed saja. Apply memeriksa
      HEAD, worktree bersih, dan base patch.</p></div></aside>
    </div>"""


def detail(run_id, row, csrf, verify_commands=(), can_review=False,
           can_fix=False, base_changed=False):
    prompt, model, mode, base, raw, patch, applied, raw_verification, raw_review = row
    secrets = scan_diff(patch) if patch else {"findings": []}
    secret_html = ""
    allow_box = ""
    if secrets["findings"]:
        items = "".join(
            f'<li><code>{esc(item["file"])}:{esc(item["line"])}</code> '
            f'{esc(item["kind"])}</li>' for item in secrets["findings"][:10])
        secret_html = (
            '<p class="eyebrow">CREDENTIALS</p>'
            '<p class="notice">Patch menambahkan pola yang tampak seperti kredensial. '
            'Nilainya tidak ditampilkan; periksa dulu sebelum menerapkan.</p>'
            f'<ul class="history">{items}</ul>')
        allow_box = (
            '<label><input type="checkbox" name="allow_secrets" value="1"> '
            'Saya sudah memeriksa dan tetap ingin menerapkan</label>')
    result = _stored(raw)
    status = _status(result.get("status") if result else None)
    if applied:
        status = "Diterapkan"
    text = (result.get("text") if isinstance(result, dict) else "") or "Tidak ada respons."
    turns = _number(result.get("turns")) if result else 0
    calls = _number(result.get("calls")) if result else 0
    tokens = result.get("tokens") if result else None
    tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else "—"
    if patch:
        patch_block = (f'<pre class="diff" tabindex="0" role="region" '
                       f'aria-label="Proposed patch">{esc(patch)}</pre>')
    elif result is not None and result.get("status") != "ok":
        patch_block = ('<p class="notice">Run tidak selesai; tidak ada patch yang '
                       'dapat diterapkan.</p>')
    else:
        patch_block = '<p class="muted">Tidak ada patch untuk diterapkan.</p>'
    download = (f'<p><a class="button" href="/runs/{run_id}/patch">Unduh patch</a></p>'
                if patch else "")
    verification = _stored(raw_verification) if raw_verification else None
    verification_html = ""
    if verification is not None:
        outcome = "Verification passed." if verification.get("ok") else "Verification failed."
        output = []
        for item in verification.get("commands", []):
            if not isinstance(item, dict):
                continue
            output.append(
                f'<p><code>{esc(" ".join(item.get("argv", [])))}</code></p>'
                f'<pre class="diff">{esc(item.get("output", ""))}</pre>')
        verification_html = (f'<p class="eyebrow">VERIFICATION</p><p>{outcome}</p>'
                             + "".join(output))
    review = _stored(raw_review) if raw_review else None
    review_html = ""
    review_form = ""
    if review is not None:
        if review.get("status") == "ok":
            findings = review.get("findings") or []
            if findings:
                items = []
                for item in findings:
                    if not isinstance(item, dict):
                        continue
                    items.append(
                        f'<li><strong>[{esc(item.get("severity", "low"))}] '
                        f'{esc(item.get("file", ""))}:{esc(item.get("line", ""))} '
                        f'{esc(item.get("title", ""))}</strong>'
                        f'<p>{esc(item.get("detail", ""))}</p></li>')
                review_html = (f'<p class="eyebrow">REVIEW</p><ul class="history">'
                               f'{"".join(items)}</ul>')
            else:
                review_html = ('<p class="eyebrow">REVIEW</p>'
                               '<p>No concrete defects reported.</p>')
        else:
            detail_text = review.get("text") if isinstance(review.get("text"), str) else ""
            review_html = ('<p class="eyebrow">REVIEW</p>'
                           '<p>The review response could not be parsed.</p>'
                           + (f'<pre class="diff">{esc(detail_text)}</pre>'
                              if detail_text else ""))
    if can_review and patch and (review is None or review.get("status") != "ok"):
        review_form = f"""
        <form action="/runs/{run_id}/review" method="post">
          <input type="hidden" name="csrf" value="{csrf}">
          <button type="submit">Review patch</button>
        </form>"""
    fix_form = ""
    if can_fix and patch:
        action = ("Batalkan dan usulkan perbaikan" if applied
                  else "Usulkan perbaikan")
        fix_form = f"""
        <form action="/runs/{run_id}/fix" method="post">
          <input type="hidden" name="csrf" value="{csrf}">
          <button type="submit">{action}</button>
        </form>"""
    apply_form = ""
    if applied:
        apply_form = '<p class="notice">Patch sudah diterapkan.</p>'
    elif base_changed:
        apply_form = ('<p class="notice">Base commit sudah bergeser; buat tugas '
                      'baru sebelum menerapkan.</p>')
    elif patch:
        verify_box = ""
        if verify_commands:
            commands = "".join(
                f'<li><code>{esc(shlex.join(str(part) for part in argv))}</code></li>'
                for argv in verify_commands)
            verify_box = (
                '<label><input type="checkbox" name="verify" value="1"> '
                'Jalankan verifikasi setelah apply</label>'
                f'<ul class="history">{commands}</ul>')
        apply_form = f"""
        <form action="/runs/{run_id}/apply" method="post">
          <input type="hidden" name="csrf" value="{csrf}">
          {verify_box}
          {allow_box}
          <button type="submit">Terapkan patch</button>
        </form>"""
    return f"""
    <a class="back" href="/">← Tugas baru</a>
    <div class="intro"><div><p class="eyebrow">02 / PROPOSAL</p>
      <h1>Review patch<span>.</span></h1><p>{esc(model)} · {esc(mode)}</p></div>
      <span class="pill">{esc(status)}</span></div>
    <div class="board"><section class="panel">
      <div class="panel-top"><h2>Respons</h2><span class="chip">OUTPUT</span></div>
      <p class="eyebrow">INSTRUKSI</p><p class="prose">{esc(prompt)}</p>
      <p class="eyebrow">RESPONS</p><p class="prose">{esc(text)}</p>
      <p class="eyebrow">DIFF</p>{patch_block}
      {download}
      {secret_html}
      {verification_html}
      {review_html}
      {review_form}
      {fix_form}
      {apply_form}
      <dl class="metrics"><div><dt>Putaran</dt><dd>{esc(turns)}</dd></div>
       <div><dt>Tool call</dt><dd>{esc(calls)}</dd></div>
       <div><dt>Token</dt><dd>{esc(tokens)}</dd></div></dl>
    </section><aside class="panel"><div class="panel-top"><h2>Base</h2>
    <span class="chip">COMMIT</span></div><p class="muted">{esc(base)}</p>
    <div class="limits"><h3>Apply</h3><p>Patch hanya diterapkan jika HEAD masih
    sama dan worktree bersih.</p></div></aside></div>"""
