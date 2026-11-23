"""Read-eval loop for reviewed coding proposals."""

import re

from .coding import apply_reviewed, solve
from .core import MODEL_ID
from .usage import take_usage, usage_totals

MAX_PROMPT = 6000
HELP = ("/diff  Preview patch  ·  /apply  Review and apply  ·  /review  Ask the model to review\n"
        "/fix [guidance]  Verify and propose a fix  ·  /verify  Run checks\n"
        "/model [id]  Switch model\n"
        "/models  List provider models  ·  /prompts  List MCP prompts  ·  /skills  List skills\n"
        "/prompt NAME k=v  Render one\n"
        "/commands  List prompt commands  ·  /sessions  List saved  ·  /fork  Branch\n"
        "/undo  Revert the last step  ·  /reset  Drop proposal  ·  /quit  Exit\n"
        "Prompt commands from .nalar/commands run as /name ARGUMENTS.")


def _split_arguments(text):
    """Split `/prompt` arguments on spaces, keeping quoted runs and backslashes."""
    words = []
    current = []
    quote = None
    started = False
    quoted_content = False
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
                if not quoted_content:
                    started = True
            else:
                current.append(char)
                quoted_content = True
                started = True
            continue
        if char in {'"', "'"}:
            quote = char
            quoted_content = False
            started = True
            continue
        if char.isspace():
            if started or current:
                words.append("".join(current))
                current = []
                started = False
            continue
        current.append(char)
        started = True
    if quote is not None:
        raise ValueError("unterminated quote")
    if started or current:
        words.append("".join(current))
    return words


def _terminal_line(value):
    return (_terminal(value).replace("\r", "\\r")
            .replace("\n", "\\n").replace("\t", "\\t"))


def _terminal(value):
    # Model output and diffs can contain terminal-control characters, even in text files.
    text = str(value).encode("utf-8", "backslashreplace").decode("utf-8")
    text = re.sub(r"[\u00ad\u061c\u200b\u200e\u200f\u2028\u2029\ufeff\u202a-\u202e\u2060\u2066-\u2069\U000e0000-\U000e007f]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]",
                  lambda m: f"\\x{ord(m[0]):02x}", text)


SESSION_ERRORS = {
    "session_not_found": "The transcript could not be found.",
    "session_too_large": "The transcript is too large to open.",
    "invalid_session_id": "The session ID is invalid.",
    "invalid_query": "The search query must be 1-200 characters.",
    "session_write_failed": "The transcript could not be saved.",
    "session_delete_failed": "The transcript could not be deleted.",
    "session_too_complex": "The transcript metadata is too deeply nested.",
    "session_directory_failed": "The session directory could not be created.",
}


def session_error(exc):
    return SESSION_ERRORS.get(str(exc), "The transcript operation failed.")


def interactive(repo, chat_factory, stdin, stdout, prior=(), store=None, session=None, *,
                belt=None, system=None, max_turns=12, max_calls=24, context_budget=120_000,
                token_budget=0, model=None, set_model=None, verify_commands=(), secret_env=(),
                budget=None, retries=2, commands=(), models_fetcher=None, prompts=(),
                skills=(), warnings=(), fix_config=None, model_holder=None,
                review_factory=None, review_model=None, subagent_usage=None):
    try:
        stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, TypeError, ValueError, OSError):
        pass

    def show(text):
        print(_terminal(text), file=stdout, flush=True)

    def ask(label):
        print(label, end="", file=stdout, flush=True)
        return stdin.readline()

    def record(kind, text, **meta):
        if store is None or session is None:
            return
        try:
            store.append(session, kind, text, **meta)
        except ValueError:
            show("Transcript could not be saved.")

    show(f"Nalar Agent · {repo.commit.decode()[:12]}")
    if prior:
        show(f"Resumed session {session.id if session else ''}: {len(prior)} previous task(s).")
        for task in prior[-4:]:
            show(f"  saved: {_terminal(task)[:120]}")
    for warning in warnings[:8]:
        show(f"warning: {warning}")
    show(HELP)
    prior = list(prior)[-4:]
    state = {"model": model}
    undo_stack = []

    def approve(name, arguments):
        import json

        payload = json.dumps(arguments, ensure_ascii=True)
        shown = payload[:300] + (" … (truncated)" if len(payload) > 300 else "")
        show(f"Approval required: {name} {shown} ({len(payload)} characters)")
        return ask("Allow this tool once? [y/N] ").strip().lower() in {"y", "yes"}
    streamed = []
    line_open = {"value": False}

    def on_text(piece):
        streamed.append(piece)
        stdout.write(_terminal(piece))
        stdout.flush()
        line_open["value"] = True

    def progress(event):
        # Progress lines are only for a real terminal; captured output stays clean.
        if not getattr(stdout, "isatty", lambda: False)():
            return
        kind = event.get("type")
        if kind == "tool":
            text = f"  · {event.get('name')} {event.get('status')}"
        elif kind == "retry":
            text = f"  · retry {event.get('code')} (attempt {event.get('attempt')})"
        else:
            return
        if line_open["value"]:
            show("")
            line_open["value"] = False
        show(text)
    while raw := ask("nalar> "):
        task = raw.strip()
        if not task:
            continue
        if task == "/quit":
            return
        if task == "/help":
            show(HELP)
            if commands:
                show("Prompt commands: " + ", ".join(f"/{item.name}" for item in commands))
            continue
        if task == "/undo":
            if not undo_stack:
                show("Nothing to undo.")
            else:
                snapshot, prior = undo_stack.pop()
                repo.restore(snapshot)
                record("undo", "Reverted the last accepted proposal step.")
                show("Restored the previous proposal state.")
            continue
        if task == "/reset":
            repo.restore({}, {})
            prior.clear()
            undo_stack.clear()
            show("Proposal cleared.")
            continue
        if task == "/review":
            from .core import ProviderError
            from .review import review

            diff = repo.patch()
            if not diff:
                show("No patch proposed.")
                continue
            reviewer = (review_factory() if review_factory is not None
                        else chat_factory())
            target_model = review_model or state["model"] or "custom"
            try:
                result = review(reviewer, diff, model=target_model,
                                retries=retries, budget=budget)
            except ProviderError as exc:
                record("review", "0 finding(s)", status="provider_error",
                       tokens=0, model=target_model)
                show(f"Review failed: {exc}")
                continue
            record("review", f"{len(result['findings'])} finding(s)",
                   status=result["status"],
                   tokens=result.get("tokens") or 0,
                   model=result.get("model") or state["model"])
            if result["status"] != "ok":
                show("Review was not parsed.")
                if result["text"]:
                    show(result["text"][:2_000])
                continue
            if not result["findings"]:
                show("No concrete defects were reported.")
            for item in result["findings"]:
                show(f"[{_terminal_line(item['severity'])}] "
                     f"{_terminal_line(item['file'])}:{_terminal_line(item['line'])} "
                     f"{_terminal_line(item['title'])} — "
                     f"{_terminal_line(item['detail'])}")
            continue
        if task == "/models":
            if models_fetcher is None:
                show("Set the provider API key to list models.")
                continue
            from .core import ProviderError

            try:
                models = models_fetcher()
            except (ProviderError, ValueError, OSError):
                show("The provider model list could not be loaded.")
                continue
            if not models:
                show("The provider listed no models.")
                continue
            show("\n".join(_terminal_line(name) for name in models[:60])
                 + ("" if len(models) <= 60 else "\n…"))
            continue
        if task == "/model" or task.startswith("/model "):
            value = task.partition(" ")[2].strip()
            if not value:
                show(f"Current model: {state['model'] or 'unset'}")
                continue
            if not MODEL_ID.fullmatch(value):
                show("Use the provider's model ID.")
                continue
            state["model"] = value
            if model_holder is not None:
                model_holder["value"] = value
            if set_model is not None:
                try:
                    set_model(value, session)
                except TypeError:
                    set_model(value)
            show(f"Model set to {value}.")
            continue
        if task == "/fix" or task.startswith("/fix "):
            from .fix import run_fix

            if fix_config is None or not fix_config.verify:
                show("Add a verify list to nalar.json first.")
                continue
            for argv in fix_config.verify:
                show("  $ " + " ".join(_terminal_line(arg) for arg in argv))
            if ask("Type yes to run: ").strip() != "yes":
                show("Cancelled.")
                continue
            show("Verifying the committed snapshot…")
            if budget is not None:
                budget.reset(token_budget)
            guidance = task[4:].strip() if task.startswith("/fix ") else ""
            payload = None
            try:
                payload = run_fix(repo, chat_factory(), fix_config, attempts=2,
                                  belt=belt, system=system, budget=budget,
                                  approve=approve, on_event=progress,
                                  instructions=guidance)
            except ValueError as exc:
                show(str(exc))
                continue
            finally:
                fix_usage = usage_totals(take_usage(subagent_usage))
                if payload is None and fix_usage:
                    record("fix", "failed", status="failed", tokens=0,
                           model=state["model"])
                    for label, amount in fix_usage.items():
                        record("subagent", f"{amount} tokens",
                               model=label, tokens=amount)
            parent_tokens = payload.get("tokens") or 0
            if budget is not None:
                parent_tokens = max(0, parent_tokens - sum(fix_usage.values()))
            record("fix", f"{payload.get('attempts', 0)} attempt(s)",
                   status="already_ok" if payload["already_ok"] else payload["status"],
                   tokens=parent_tokens, model=state["model"])
            for label, amount in fix_usage.items():
                record("subagent", f"{amount} tokens",
                       model=label, tokens=amount)
            if payload["already_ok"]:
                show("Verification already passes.")
                continue
            if payload["patch"]:
                show(payload["patch"])
                show(f"{payload['status']} · attempt {payload['attempts']}")
                if payload["status"] == "ok":
                    show("Patch ready. /diff to inspect, /review to review, /apply to apply.")
                else:
                    show("Verification still fails. /diff to inspect; applying is your call.")
            else:
                show("No patch was proposed.")
                if payload["verification"].get("error"):
                    show(str(payload["verification"]["error"]))
            continue
        if task == "/verify":
            from .verify import run_commands

            if not verify_commands:
                show("Add a verify list to nalar.json first.")
                continue
            for argv in verify_commands:
                show("  $ " + " ".join(_terminal_line(arg) for arg in argv))
            if ask("Type yes to run: ").strip() != "yes":
                show("Cancelled.")
                continue
            result = run_commands(verify_commands, repo.root, secret_names=secret_env)
            from .verify import status_text

            for item in result["commands"]:
                show(f"$ {' '.join(_terminal_line(arg) for arg in item['argv'])}\n"
                     f"{status_text(item)} · {item['duration_ms']} ms\n"
                     f"{item['output'][:4000]}")
            show("Verification passed." if result["ok"] else "Verification failed.")
            continue
        if task == "/commands":
            if not commands:
                show("No prompt commands are available.")
                continue
            for item in commands:
                show(f"/{item.name}: {item.description}")
            continue
        if task == "/skills":
            if not skills:
                show("No skills are available.")
                continue
            for item in skills:
                show(f"{item.name}: {item.description}")
            continue
        if task == "/prompts":
            if not prompts:
                show("No MCP prompts are available.")
                continue
            for item in prompts:
                names = ", ".join(_terminal_line(argument["name"])
                                 for argument in item.arguments) or "none"
                show(f"{_terminal_line(item.name)}: "
                     f"{_terminal_line(item.description)} (arguments: {names})")
            continue
        if task == "/sessions":
            if store is None:
                show("Session persistence is disabled.")
                continue
            for item in store.list(repo.root):
                show(f"{_terminal_line(item['id'])}  {_terminal_line(item['updated'])}  "
                     f"{_terminal_line(item['task'][:60])}")
            continue
        if task == "/fork":
            if store is None:
                show("Session persistence is disabled.")
                continue
            if session is None:
                show("No session is open yet.")
                continue
            try:
                session = store.fork(session.id)
            except ValueError as exc:
                show(session_error(exc))
                continue
            show(f"Forked session {session.id}.")
            continue
        if task == "/diff" or task == "/apply" or task.startswith("/apply "):
            diff = repo.patch()
            show(diff or "No patch proposed.")
            if task == "/diff" or not diff:
                continue
            allow_secrets = task.strip() == "/apply --allow-secrets"
            if ask("Apply this patch to the working tree? Type yes: ").strip() != "yes":
                show("Cancelled.")
                continue
            try:
                apply_reviewed(repo, diff, allow_secrets=allow_secrets)
            except ValueError as exc:
                show(str(exc))
                if "credentials" in str(exc):
                    show("Review them, then use /apply --allow-secrets.")
                continue
            show("Applied. Review with git diff and run your project checks. Tests were "
                 "not run. Revert with: nalar apply --last --yes")
            return
        from_prompt = False
        if task == "/prompt" or task.startswith("/prompt "):
            if not prompts:
                show("No MCP prompts are available.")
                continue
            try:
                words = _split_arguments(task)
            except ValueError:
                show("Prompt arguments are malformed; check the quotes.")
                continue
            name = words[1] if len(words) > 1 else ""
            found = next((item for item in prompts if item.name == name), None)
            if found is None:
                show("Unknown MCP prompt; use /prompts to list them.")
                continue
            arguments = {}
            invalid = False
            for word in words[2:]:
                key, separator, value = word.partition("=")
                if not separator or not key:
                    show("Prompt arguments use key=value.")
                    invalid = True
                    break
                arguments[key] = value
            if invalid:
                continue
            try:
                text = found.fetch(arguments)
            except Exception:
                show("The prompt could not be rendered by its server.")
                continue
            if not text:
                show("The prompt returned no text.")
                continue
            if len(text) > MAX_PROMPT:
                show(f"The rendered prompt exceeds {MAX_PROMPT} characters.")
                continue
            record("prompt", f"/prompt {name}")
            task = text
            from_prompt = True
        if task.startswith("/") and commands and not from_prompt:
            from .commands import find, render

            name, _, rest = task[1:].partition(" ")
            found = find(commands, name)
            if found is None:
                show("Unknown command. " + HELP)
                continue
            rest = rest.strip()
            if len(rest) > MAX_PROMPT:
                show(f"Task exceeds {MAX_PROMPT} characters.")
                continue
            from .commands import CommandError

            try:
                task = render(found, rest)
            except CommandError as exc:
                show(str(exc))
                continue
            if not task:
                show(f"/{name} expands to an empty task.")
                continue
            record("command", f"/{name} {rest}".strip())
        elif task.startswith("/") and not from_prompt:
            show("Unknown command. " + HELP)
            continue
        if len(task) > MAX_PROMPT:
            show(f"Task exceeds {MAX_PROMPT} characters.")
            continue
        revisions = "\n".join(prior[-4:])[-6000:]
        prompt = f"Previous user revisions:\n{revisions}\n\nCurrent task:\n{task}" if revisions else task
        snapshot = repo.snapshot()
        before = (snapshot, list(prior))
        if budget is not None:
            budget.reset(token_budget)
        record("user", task)
        chat = chat_factory()
        streamed.clear()
        result, _ = solve(chat, repo, prompt,
                          on_text=on_text if hasattr(chat, "stream") else None,
                          belt=belt, system=system, max_turns=max_turns,
                          max_calls=max_calls, context_budget=context_budget,
                          approve=approve, token_budget=token_budget, budget=budget,
                          retries=retries, on_event=progress)
        if streamed:
            show("")
        show(f"{result.status} · {result.turns} turns · {result.calls} tool calls")
        usage = take_usage(subagent_usage)
        delegated = usage_totals(usage)
        failed = result.status != "ok"
        if failed:
            last = result.trace[-1] if result.trace else {}
            reason = str(last.get("status") or "")
            if reason and reason != result.status:
                show(f"reason: {reason}")
            repo.restore(snapshot)
        parent_tokens = result.tokens or 0
        if budget is not None:
            parent_tokens = max(0, parent_tokens - sum(delegated.values()))
        record("assistant", result.text or result.status, status=result.status,
               model=state["model"], tokens=parent_tokens)
        for label, amount in delegated.items():
            record("subagent", f"{amount} tokens",
                   model=label, tokens=amount)
        if failed:
            show("Failed task changes discarded; existing proposal kept.")
            continue
        prior.append(task)
        prior = prior[-4:]
        undo_stack = (undo_stack + [before])[-10:]
        if result.text and not streamed:
            show(result.text)
        if repo.patch():
            show("Patch ready. /diff to inspect, /apply to review and approve.")
