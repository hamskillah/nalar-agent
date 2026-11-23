"""The agent loop: budgets, recovery, compaction, retries and events."""

import inspect
import json
from dataclasses import dataclass
from time import monotonic, sleep as default_sleep
from uuid import uuid4

from .core import SYSTEM, ProviderError, RunResult
from .workspace import ToolError


@dataclass
class Budget:
    limit: int = 0
    spent: int = 0

    def reset(self, limit):
        self.limit = int(limit)
        self.spent = 0

    def add(self, amount):
        if amount:
            self.spent += int(amount)

    def exceeded(self):
        return bool(self.limit) and self.spent > self.limit

    def remaining(self):
        return max(self.limit - self.spent, 0) if self.limit else None


def estimate_tokens(messages, reply, tools=()):
    """Rough billed-token estimate covering the whole request, not just the tail."""
    if isinstance(messages, dict):
        messages = [messages]
    payload = json.dumps(messages, ensure_ascii=False)
    if tools:
        payload += json.dumps(list(tools), ensure_ascii=False)
    payload += reply.text or ""
    for call in reply.calls:
        payload += call.raw_arguments or json.dumps(call.arguments or {})
    return max(1, len(payload) // 4)

RETRYABLE = {"transport_unavailable", "request_timeout"}
TRUNCATED_PREFIX = '{"ok": true, "truncated": true'
TRUNCATED_NOTE = ("Earlier output was dropped to keep context small; "
                  "re-read the file or search again if it matters.")


def _is_truncated(content):
    return isinstance(content, str) and content.startswith(TRUNCATED_PREFIX)


# Read-only tools with no shared mutable state; safe to run concurrently.
PARALLEL_READS = {"list_files", "search", "read_lines", "find_symbol",
                  "git_log", "git_show", "use_skill"}
READ_ONLY_TOOLS = PARALLEL_READS | {"staged_diff", "update_plan"}


class AgentLoop:
    def __init__(self, chat, belt, *, system=SYSTEM, mode="recover", max_turns=8, max_calls=12,
                 context_budget=120_000, retries=2, sleep=default_sleep, backoff=0.5,
                 cancel=None, approve=None, token_budget=0, budget=None):
        if mode not in ("baseline", "recover") or max_turns < 1 or max_calls < 0:
            raise ValueError("invalid run configuration")
        self.chat = chat
        self.belt = belt
        self.system = system
        self.mode = mode
        self.max_turns = max_turns
        self.max_calls = max_calls
        self.context_budget = context_budget
        self.retries = max(retries, 0)
        self.sleep = sleep
        self.backoff = backoff
        self.cancel = cancel
        self.approve = approve
        self.token_budget = token_budget
        self.budget = budget if budget is not None else Budget(token_budget)
        if token_budget:
            self.budget.limit = token_budget

    def _size(self, messages):
        return len(json.dumps(messages, ensure_ascii=False))

    @staticmethod
    def _tool_name(messages, index):
        call_id = messages[index].get("tool_call_id")
        for prior in reversed(messages[:index]):
            if prior.get("role") != "assistant":
                continue
            for call in prior.get("tool_calls") or ():
                if (isinstance(call, dict) and call.get("id") == call_id
                        and isinstance(call.get("function"), dict)
                        and isinstance(call["function"].get("name"), str)):
                    return call["function"]["name"]
        return None

    def _compact(self, messages):
        compacted = array = 0
        while self._size(messages) > self.context_budget:
            candidates = [index for index, message in enumerate(messages[:-4])
                          if message.get("role") == "tool"
                          and not _is_truncated(message.get("content"))]
            if not candidates:
                break
            index = candidates[0]
            marker = {"ok": True, "truncated": True, "note": TRUNCATED_NOTE}
            name = self._tool_name(messages, index)
            if name:
                marker["tool"] = name
            messages[index]["content"] = json.dumps(marker)
            compacted += 1
            array += 1
            if array > 200:
                break
        if self._size(messages) > self.context_budget:
            last_user = max((index for index, message in enumerate(messages)
                             if message.get("role") == "user"), default=-1)
            for index, message in enumerate(messages[:-4]):
                if index == last_user or message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if (isinstance(content, str) and content
                        and not content.startswith("[Earlier")):
                    message["content"] = ("[Earlier assistant reply omitted to keep "
                                          "context small.]")
                    compacted += 1
                    if self._size(messages) <= self.context_budget:
                        return compacted
            for index, message in enumerate(messages[:-4]):
                if index == last_user or message.get("role") != "user":
                    continue
                content = message.get("content")
                if (isinstance(content, str) and content
                        and not content.startswith("[Earlier")):
                    message["content"] = ("[Earlier user message omitted to keep "
                                          "context small.]")
                    compacted += 1
                    if self._size(messages) <= self.context_budget:
                        return compacted
        return compacted

    def _parallel(self, names):
        names = list(names)
        if self.mode != "recover" or len(names) < 2:
            return False
        return all(name in PARALLEL_READS and not self.belt.requires_approval(name)
                   for name in names)

    def _read_batch(self, calls, failures):
        from concurrent.futures import ThreadPoolExecutor

        def read_one(call):
            try:
                return {"ok": True, "value": self.belt.call(call.name, call.arguments)}
            except ToolError as exc:
                value = {"ok": False, "error": str(exc)}
                if getattr(exc, "detail", None):
                    value["detail"] = str(exc.detail)[:500]
                return value

        keys = [self._call_key(call) for call in calls]
        pending = [index for index, key in enumerate(keys)
                   if key is None or key not in failures]
        outputs = [None] * len(calls)
        for index, call in enumerate(calls):
            key = keys[index]
            if key is not None and key in failures:
                outputs[index] = {
                    "ok": False, "error": "repeated_call",
                    "detail": f"This identical call already failed with "
                              f"{failures[key]}; change it or inspect the current "
                              "state before retrying."}
        with ThreadPoolExecutor(max_workers=min(4, max(len(pending), 1))) as pool:
            results = list(pool.map(read_one, [calls[index] for index in pending]))
        for index, output in zip(pending, results):
            outputs[index] = output
            if (not output["ok"]
                    and output["error"] not in {"approval_required", "skipped_after_error",
                                                "repeated_call"}
                    and not output["error"].startswith("mcp_error")
                    and keys[index] is not None):
                failures[keys[index]] = output["error"]
        return outputs

    @staticmethod
    def _call_key(call):
        try:
            return (call.name, json.dumps(call.arguments, sort_keys=True,
                                          ensure_ascii=True, default=str))
        except (TypeError, ValueError):
            return None

    def _emit(self, callback, event):
        if callback is not None:
            callback(event)

    def run(self, prompt, on_event=None, cancel=None, on_text=None, history=None,
            messages_out=None):
        if history is None:
            messages = [{"role": "system", "content": self.system},
                        {"role": "user", "content": prompt}]
        else:
            if (not isinstance(history, (list, tuple)) or not history
                    or any(not isinstance(message, dict)
                           or not isinstance(message.get("role"), str)
                           for message in history)):
                raise ValueError("history must be a nonempty list of messages")
            messages = [dict(message) for message in history]
            for message in messages:
                message.pop("reasoning_content", None)
            messages.append({"role": "user", "content": prompt})
        start = monotonic()
        trace = []
        turns = count = 0
        tokens = None
        compacted = 0
        plan_items = []
        failures = {}
        estimated = False
        names = self.belt.names
        nonce = uuid4().hex[:8]

        cancel = cancel if cancel is not None else self.cancel

        def stopped():
            return cancel is not None and cancel.is_set()

        def finish(status, text=""):
            if messages_out is not None:
                messages_out[:] = [dict(message) for message in messages]
            total = self.budget.spent if self.budget.spent else tokens
            result = RunResult(status, text, turns, count, total,
                               round((monotonic() - start) * 1000), tuple(trace), compacted,
                               estimated, tuple(dict(item) for item in plan_items))
            self._emit(on_event, {"type": "final", "text": text, "status": status})
            return result

        def cancelled():
            trace.append({"turn": max(turns, 1), "status": "cancelled"})
            return finish("cancelled")

        for turns in range(1, self.max_turns + 1):
            if self.budget.exceeded():
                return finish("token_limit")
            if stopped():
                return cancelled()
            compacted += self._compact(messages)
            attempt = 0
            empty_attempt = 0
            charged = False
            self._emit(on_event, {"type": "request", "turn": turns})
            while True:
                if stopped():
                    return cancelled()
                emitted = 0
                try:
                    if on_text is not None and hasattr(self.chat, "stream"):
                        def send(piece):
                            nonlocal emitted
                            emitted += 1
                            on_text(piece)
                        kwargs = {"on_text": send}
                        try:
                            if "cancel" in inspect.signature(self.chat.stream).parameters:
                                kwargs["cancel"] = cancel if cancel is not None else self.cancel
                        except (TypeError, ValueError):
                            pass
                        reply = self.chat.stream(messages, self.belt.specs, **kwargs)
                    else:
                        kwargs = {}
                        try:
                            if "cancel" in inspect.signature(self.chat).parameters:
                                kwargs["cancel"] = cancel if cancel is not None else self.cancel
                        except (TypeError, ValueError):
                            pass
                        reply = self.chat(messages, self.belt.specs, **kwargs)
                    if not reply.calls and not reply.text.strip():
                        # An empty completion is usually transient; retry the
                        # provider call before failing the turn. Charge it once.
                        charged = True
                        amount = reply.tokens
                        if amount is None and self.budget.limit:
                            amount = estimate_tokens(messages, reply, self.belt.specs)
                            estimated = True
                        if amount is not None:
                            tokens = (tokens or 0) + amount
                            self.budget.add(amount)
                            if self.budget.exceeded():
                                return finish("token_limit")
                        if empty_attempt < self.retries and not stopped():
                            self._emit(on_event, {"type": "retry", "code": "empty_response",
                                                  "attempt": empty_attempt + 1})
                            self.sleep(min(self.backoff * (2 ** empty_attempt), 60))
                            empty_attempt += 1
                            continue
                    else:
                        charged = False
                    break
                except ProviderError as exc:
                    code = str(exc)
                    trace.append({"turn": turns, "status": code})
                    if code == "cancelled":
                        return finish("cancelled")
                    transient = (code in RETRYABLE or code.startswith("http_5")
                                 or code == "http_429")
                    if not transient or emitted or attempt >= self.retries:
                        return finish("provider_error")
                    delay = exc.retry_after if exc.retry_after is not None else self.backoff * (2 ** attempt)
                    self._emit(on_event, {"type": "retry", "code": code, "attempt": attempt + 1})
                    self.sleep(min(delay, 60))
                    attempt += 1
            if charged:
                if self.budget.exceeded():
                    return finish("token_limit")
            else:
                amount = reply.tokens
                if amount is None and self.budget.limit:
                    amount = estimate_tokens(messages, reply, self.belt.specs)
                    estimated = True
                if amount is not None:
                    tokens = (tokens or 0) + amount
                    self.budget.add(amount)
                    if self.budget.exceeded():
                        return finish("token_limit")
            self._emit(on_event, {"type": "reply", "text": reply.text, "calls": len(reply.calls)})
            if not reply.calls:
                if not reply.text.strip():
                    return finish("empty_response")
                messages.append({"role": "assistant", "content": reply.text})
                return finish("ok", reply.text)
            if count + len(reply.calls) > self.max_calls:
                return finish("call_limit")
            if turns == self.max_turns:
                return finish("turn_limit")
            ids = [call.id or f"call_{nonce}_{turns}_{index}"
                   for index, call in enumerate(reply.calls)]
            message = {"role": "assistant", "content": reply.text or None, "tool_calls": [
                {"id": call_id, "type": "function", "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments if call.raw_arguments is not None
                    else json.dumps(call.arguments)}} for call, call_id in zip(reply.calls, ids)]}
            messages.append(message)
            if self._parallel(call.name for call in reply.calls):
                results = self._read_batch(reply.calls, failures)
                for call, call_id, output in zip(reply.calls, ids, results):
                    count += 1
                    status = "ok" if output["ok"] else output["error"]
                    trace.append({"turn": turns,
                                  "tool": call.name if call.name in names else "unknown",
                                  "status": status})
                    self._emit(on_event, {"type": "tool",
                                          "name": call.name if call.name in names else "unknown",
                                          "status": status})
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps(output)})
                    if stopped():
                        return cancelled()
                continue
            batch_failed = False
            for call, call_id in zip(reply.calls, ids):
                if stopped():
                    return cancelled()
                count += 1
                label = call.name if call.name in names else "unknown"
                key = None
                try:
                    if batch_failed:
                        raise ToolError("skipped_after_error",
                                        detail="Skipped because an earlier call failed.")
                    if self.belt.requires_approval(call.name):
                        allowed = bool(self.approve(call.name, call.arguments)) if self.approve else False
                        self._emit(on_event, {"type": "approval", "name": call.name,
                                              "approved": allowed})
                        if not allowed:
                            raise ToolError("approval_required",
                                            detail="The call was not approved.")
                    key = self._call_key(call)
                    if key is not None and key in failures:
                        raise ToolError(
                            "repeated_call", nonfatal=True,
                            detail=f"This identical call already failed with "
                                   f"{failures[key]}; change it or inspect the current "
                                   "state before retrying.")
                    value = self.belt.call(call.name, call.arguments)
                    output = {"ok": True, "value": value}
                    status = "ok"
                    if call.name not in READ_ONLY_TOOLS:
                        failures.clear()
                    if (call.name == "update_plan" and isinstance(value, dict)
                            and isinstance(value.get("plan"), list)):
                        plan_items[:] = value["plan"]
                        self._emit(on_event, {"type": "plan",
                                              "items": [dict(item) for item in plan_items]})
                except ToolError as exc:
                    if not getattr(exc, "nonfatal", False):
                        batch_failed = True
                    status = str(exc)
                    output = {"ok": False, "error": status}
                    if getattr(exc, "detail", None):
                        output["detail"] = str(exc.detail)[:500]
                    if (key is not None
                            and status not in {"approval_required", "skipped_after_error",
                                               "repeated_call"}
                            and not status.startswith("mcp_error")):
                        failures[key] = status
                trace.append({"turn": turns, "tool": label, "status": status})
                self._emit(on_event, {"type": "tool", "name": label, "status": status})
                if not output["ok"] and self.mode == "baseline":
                    return finish("tool_error")
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps(output)})
        return finish("turn_limit")


Runner = AgentLoop
