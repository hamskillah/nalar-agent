"""Explicit, OS-level verification commands from configuration.

Run only on request (or with --yes). No shell is used, output is bounded,
provider credentials and common secret variables are removed from the child
environment, and a timeout kills the whole process group.
"""

import os
import re
import signal
import subprocess
import time

from .coding import git_environment

MAX_OUTPUT = 16_000
CLEANUP_TIMEOUT = 2.0
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
SAFE_ENV_PREFIXES = ("XDG_", "DBUS_")
SECRET = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|SECRET_KEY|PASSWORD|PASSWD|PASS|CREDENTIAL|"
    r"CREDENTIALS|API_?KEY|ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET|AUTH|SESSION|"
    r"DSN|KEY)(?:_|$)"
    r"|^(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|"
    r"GH_TOKEN|GITHUB_TOKEN|GITLAB_TOKEN|NPM_TOKEN|DOCKER_AUTH_CONFIG|"
    r"STRIPE_KEY|STRIPE_SECRET_KEY|DATABASE_URL|REDIS_URL|MONGO_URI|MONGO_URL|"
    r"POSTGRES_URL|POSTGRESQL_URL|MYSQL_URL|MARIADB_URL|AMQP_URL|RABBITMQ_URL|"
    r"SMTP_PASSWORD|SENDGRID_API_KEY|MAILGUN_API_KEY|TWILIO_AUTH_TOKEN|"
    r"SENTRY_AUTH_TOKEN|VERCEL_TOKEN|NETLIFY_AUTH_TOKEN|CLOUDFLARE_API_TOKEN|"
    r"HEROKU_API_KEY|LINEAR_API_KEY|JIRA_API_TOKEN|ATLASSIAN_API_TOKEN|"
    r"SUPABASE_SERVICE_KEY|FIREBASE_TOKEN)$",
    re.IGNORECASE)


def _clean(value):
    return CONTROL.sub("", value)[:MAX_OUTPUT]


def status_text(entry):
    """Describe how a configured command ended, without guessing."""
    status = entry.get("status")
    if status == "timeout":
        return "timed out"
    if status == "not_found":
        return "command not found"
    if status == "start_failed":
        return "failed to start"
    if entry.get("exit") is None:
        return "did not run"
    return f"exit {entry['exit']}"


def _environment(secret_names):
    environment = git_environment()
    for name in list(environment):
        if name in secret_names or (SECRET.search(name)
                                    and not name.startswith(SAFE_ENV_PREFIXES)):
            environment.pop(name, None)
    return environment


def run_commands(commands, root, *, timeout=300, secret_names=()):
    environment = _environment(secret_names)
    results = []
    for argv in commands:
        start = time.monotonic()
        entry = {"argv": list(argv), "exit": None, "status": "unknown",
                 "duration_ms": 0, "output": ""}
        try:
            process = subprocess.Popen(list(argv), cwd=str(root), env=environment,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
        except FileNotFoundError:
            entry["status"] = "not_found"
            entry["output"] = "command not found"
            entry["duration_ms"] = round((time.monotonic() - start) * 1000)
            results.append(entry)
            continue
        except OSError:
            entry["status"] = "start_failed"
            entry["output"] = "command could not be started"
            entry["duration_ms"] = round((time.monotonic() - start) * 1000)
            results.append(entry)
            continue
        try:
            out, err = process.communicate(timeout=timeout)
            entry["exit"] = process.returncode
            entry["status"] = "exited"
        except subprocess.TimeoutExpired:
            if hasattr(os, "killpg"):
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
            else:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                out, err = process.communicate(timeout=CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                # A child that escaped the process group can hold the pipes open.
                def _bytes(value):
                    if isinstance(value, str):
                        return value.encode("utf-8", "replace")
                    return value or b""

                partial = _bytes(getattr(exc, "output", None)) + _bytes(
                    getattr(exc, "stderr", None))
                for stream in (process.stdout, process.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                out, err = partial, b""
            entry["status"] = "timeout"
            entry["output"] = f"timed out after {timeout} seconds"
        raw = (out or b"") + b"\n" + (err or b"")
        output = _clean(raw.decode("utf-8", "replace"))
        entry["output"] = (entry["output"] + "\n" + output).strip() if entry["output"] else output
        entry["duration_ms"] = round((time.monotonic() - start) * 1000)
        results.append(entry)
    return {"ok": all(item["exit"] == 0 for item in results), "commands": results}
