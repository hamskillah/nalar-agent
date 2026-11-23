"""Programmatic API for embedding the Nalar coding agent."""

import copy
import os
from dataclasses import asdict
from pathlib import Path

from .agent import AgentLoop
from .coding import Repository
from .config import load_config
from .extensions import build_extensions
from .go import ChatCompletions
from .usage import take_usage, usage_totals
from .workspace import Workspace


class Agent:
    """One repository session; call run() for each task and close() when done."""

    def __init__(self, root, *, model=None, chat=None, config=None, start_mcp=True,
                 trust_project=False, approve=None, review_chat=None, record=False):
        self.config = config or load_config(root, trust_project=trust_project)
        self.approve = approve
        self.store = None
        self.session = None
        if record:
            from .store import SessionStore

            self.store = SessionStore()
        self.model = model or self.config.model
        self.repo = Repository(root)
        if chat is not None:
            self.chat = chat
            self.model = self.model or "custom"
            self._new_client = lambda: chat
        else:
            if not self.model:
                raise ValueError("Set a model in nalar.json or pass model=")
            provider = self.config.provider

            from .go import FallbackChat

            def build(name):
                return ChatCompletions(name, os.environ.get(provider.api_key_env),
                                  base_url=provider.base_url,
                                  extra_headers=dict(provider.headers),
                                  session=provider.session_header,
                                  max_tokens=provider.max_tokens,
                                  temperature=provider.temperature,
                                  timeout=provider.timeout)

            models = list(dict.fromkeys([self.model, *self.config.fallback_models]))

            def new_client():
                return FallbackChat(models, build) if len(models) > 1 else build(self.model)

            self._new_client = new_client
            self.chat = new_client()
        self.review_model = self.model
        self._review_client = self._new_client
        if review_chat is not None:
            self._review_client = lambda: review_chat
            if self.config.review_model:
                self.review_model = self.config.review_model
        elif (chat is None and self.config.review_model
              and self.config.review_model != self.model):
            self.review_model = self.config.review_model
            review_models = list(dict.fromkeys(
                [self.review_model, *self.config.fallback_models]))
            self._review_client = (
                lambda: FallbackChat(review_models, build)
                if len(review_models) > 1 else build(self.review_model))
        self.subagent_model = self.config.subagent_model or self.model
        self._subagent_client = self._new_client
        if (chat is None and self.config.subagent_model
                and self.config.subagent_model != self.model):
            sub_models = list(dict.fromkeys(
                [self.subagent_model, *self.config.fallback_models]))
            self._subagent_client = (
                lambda: FallbackChat(sub_models, build)
                if len(sub_models) > 1 else build(self.subagent_model))
        from .agent import Budget

        self.budget = Budget(self.config.token_budget)
        self.history = []
        self.extensions = build_extensions(
            self.repo, self.config, start_mcp=start_mcp,
            chat_factory=self._new_client,
            subagent_factory=self._subagent_client,
            budget=self.budget)

    def run(self, task, *, on_event=None, on_text=None, max_turns=None, max_calls=None,
            continue_conversation=False):
        self.budget.reset(self.config.token_budget)
        loop = AgentLoop(self.chat, self.extensions.belt, system=self.extensions.system,
                         max_turns=max_turns or self.config.max_turns,
                         max_calls=max_calls if max_calls is not None else self.config.max_calls,
                         context_budget=self.config.context_budget, approve=self.approve,
                         token_budget=self.config.token_budget, budget=self.budget,
                         retries=self.config.retries)
        snapshot = self.repo.snapshot()
        history = list(self.history) if continue_conversation and self.history else None
        captured = []
        result = loop.run(task, on_event=on_event, on_text=on_text, history=history,
                          messages_out=captured)
        if result.status == "ok" and captured:
            self.history = captured
        if result.status != "ok":
            self.repo.restore(snapshot)
        payload = asdict(result)
        payload.update({
            "base": self.repo.commit.decode(),
            "model": self.model,
            "patch": self.repo.patch() if result.status == "ok" else "",
            "skills": [skill.name for skill in self.extensions.skills],
            "warnings": list(self.extensions.warnings),
        })
        self._record(payload, task)
        return payload

    def _record(self, payload, task, *, review=None):
        """Write a private run summary; never store patch contents."""
        usage = take_usage(self.extensions.subagent_usage
                           if self.extensions is not None else None)
        if self.store is None:
            return
        try:
            from .apply import patch_paths

            patch = payload.get("patch") or ""
            delegated = sum(usage_totals(usage).values())
            try:
                billed = int(payload.get("tokens") or 0)
            except (TypeError, ValueError, OverflowError):
                billed = 0
            session = self.store.record_run(
                repo=self.repo.root, model=self.model,
                base=self.repo.commit.decode(), task=task,
                status=payload.get("status", "failed"),
                text=payload.get("text") or "",
                tokens=max(0, billed - delegated),
                turns=payload.get("turns") or 0,
                calls=payload.get("calls") or 0,
                verification=payload.get("verification"),
                review=review, diff=patch,
                files=patch_paths(patch) if patch else (), usage=usage)
            self.session = session
            payload["session"] = session.id
        except (ValueError, OSError) as exc:
            from .session import session_error

            payload["record_error"] = session_error(exc)

    def follow_up(self, task, **kwargs):
        """Run another task with the previous successful conversation as context."""
        return self.run(task, continue_conversation=True, **kwargs)

    def reset_conversation(self):
        """Forget the conversation used by follow_up()."""
        self.history = []

    @property
    def conversation(self):
        return tuple(copy.deepcopy(message) for message in self.history)

    @property
    def patch(self):
        """The staged proposal as a unified diff ('' when nothing is staged)."""
        return self.repo.patch()

    @property
    def commands(self):
        """Prompt commands discovered for this repository."""
        from .commands import discover

        return discover(self.config.commands)

    def command(self, name, arguments="", *, on_event=None, on_text=None):
        """Run a prompt command discovered from .nalar/commands.

        Raises ValueError for an unknown name or oversized arguments.
        """
        from .commands import CommandError, find, render
        from .session import MAX_PROMPT

        found = find(self.commands, name)
        if found is None:
            raise ValueError(f"unknown prompt command: {name}")
        if len(arguments) > MAX_PROMPT:
            raise ValueError(f"arguments must be at most {MAX_PROMPT} characters")
        try:
            prompt = render(found, arguments)
        except CommandError as exc:
            raise ValueError(str(exc)) from None
        return self.run(prompt, on_event=on_event, on_text=on_text)

    def reset(self):
        """Discard every staged edit."""
        self.repo.restore({}, {})

    def apply(self, *, allow_secrets=False):
        """Apply and consume the staged proposal with the CLI's checks."""
        from .coding import apply_reviewed

        patch = self.repo.patch()
        if not patch:
            raise ValueError("nothing staged to apply")
        result = apply_reviewed(self.repo, patch, allow_secrets=allow_secrets)
        self.repo.restore({}, {})
        return result

    def undo(self):
        """Revert the most recently applied patch recorded in `.git`."""
        from .apply import read_last_apply
        from .coding import apply_reviewed

        if self.patch:
            raise ValueError("A proposal is staged; apply or reset it before undoing.")
        record = read_last_apply(self.repo)
        if (record["base"] != self.repo.commit.decode()
                or self.repo._git("rev-parse", "HEAD").strip() != self.repo.commit):
            raise ValueError("HEAD moved since the recorded apply; revert that patch manually.")
        result = apply_reviewed(self.repo, record["patch"], reverse=True,
                                allowed_files=record["files"])
        self.repo.restore({}, {})
        return result

    def fix(self, *, attempts=2, timeout=300, instructions="", context=""):
        """Verify the snapshot; on failure, stage a corrected proposal."""
        from .fix import run_fix

        if self.patch:
            raise ValueError("A proposal is staged; apply or reset it before fixing.")
        self.budget.reset(self.config.token_budget)
        payload = run_fix(self.repo, self.chat, self.config, attempts=attempts,
                          timeout=timeout, budget=self.budget, approve=self.approve,
                          belt=self.extensions.belt, system=self.extensions.system,
                          instructions=instructions, context=context)
        self._record(payload, "Verify and fix the committed snapshot")
        return payload

    def review(self):
        """Review the staged proposal and return structured findings."""
        from .review import review as run_review

        patch = self.repo.patch()
        if not patch:
            raise ValueError("nothing staged to review")
        from .core import ProviderError

        try:
            result = run_review(self._review_client(), patch,
                                model=self.review_model,
                                retries=self.config.retries, budget=self.budget)
        except ProviderError:
            if self.store is not None and self.session is not None:
                try:
                    self.store.append(
                        self.session, "review", "0 finding(s)",
                        status="provider_error", tokens=0,
                        model=self.review_model)
                except (ValueError, OSError):
                    pass
            raise
        if self.store is not None and self.session is not None:
            try:
                self.store.append(
                    self.session, "review",
                    f"{len(result['findings'])} finding(s)",
                    status=result["status"], tokens=result.get("tokens") or 0,
                    model=result.get("model") or self.review_model)
            except (ValueError, OSError):
                pass
        return result

    def verify(self, *, timeout=300):
        """Run the configured verification commands in the real checkout."""
        if not self.config.verify:
            raise ValueError("no verify commands are configured")
        from .verify import run_commands

        return run_commands(self.config.verify, self.repo.root, timeout=timeout,
                            secret_names=(self.config.provider.api_key_env,))

    def close(self):
        self.extensions.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WorkspaceAgent:
    """One flat-directory workspace; use run() for tasks and close() when done."""

    def __init__(self, root, *, model=None, chat=None, config=None, approve=None,
                 record=False):
        self.root = Path(root).expanduser()
        self.config = config or load_config(self.root)
        self.approve = approve
        self.store = None
        self.session = None
        if record:
            from .store import SessionStore

            self.store = SessionStore()
        self.model = model or self.config.model
        if chat is not None:
            self.chat = chat
            self.model = self.model or "custom"
        else:
            if not self.model:
                raise ValueError("Set a model in nalar.json or pass model=")
            provider = self.config.provider

            from .go import FallbackChat

            def build(name):
                return ChatCompletions(name, os.environ.get(provider.api_key_env),
                                  base_url=provider.base_url,
                                  extra_headers=dict(provider.headers),
                                  session=provider.session_header,
                                  max_tokens=provider.max_tokens,
                                  temperature=provider.temperature,
                                  timeout=provider.timeout)

            models = list(dict.fromkeys([self.model, *self.config.fallback_models]))
            self.chat = (FallbackChat(models, build) if len(models) > 1
                         else build(self.model))
        self.history = []

    def run(self, task, *, mode="recover", on_event=None, on_text=None,
            max_turns=None, max_calls=None, continue_conversation=False):
        from .core import run as run_workspace

        history = (list(self.history)
                   if continue_conversation and self.history else None)
        captured = []
        with Workspace(self.root) as workspace:
            result = run_workspace(
                self.chat, workspace, task, mode=mode,
                max_turns=max_turns or self.config.max_turns,
                max_calls=(max_calls if max_calls is not None
                           else self.config.max_calls),
                context_budget=self.config.context_budget,
                token_budget=self.config.token_budget,
                retries=self.config.retries, approve=self.approve,
                on_event=on_event, on_text=on_text, history=history,
                messages_out=captured)
            files = workspace.call("list", {})
        if result.status == "ok" and captured:
            self.history = captured
        payload = asdict(result)
        payload.update({"model": self.model, "workspace": str(self.root),
                        "files": files})
        self._record(payload, task)
        return payload

    def _record(self, payload, task):
        if self.store is None:
            return
        try:
            session = self.store.record_run(
                repo=self.root.resolve(), model=self.model, base="", task=task,
                status=payload.get("status", "failed"),
                text=payload.get("text") or "",
                tokens=payload.get("tokens") or 0,
                turns=payload.get("turns") or 0,
                calls=payload.get("calls") or 0)
            self.session = session
            payload["session"] = session.id
        except (ValueError, OSError) as exc:
            from .session import session_error

            payload["record_error"] = session_error(exc)

    def follow_up(self, task, **kwargs):
        """Run another task with the previous successful conversation as context."""
        return self.run(task, continue_conversation=True, **kwargs)

    def reset_conversation(self):
        """Forget the conversation used by follow_up()."""
        self.history = []

    @property
    def conversation(self):
        return tuple(copy.deepcopy(message) for message in self.history)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
