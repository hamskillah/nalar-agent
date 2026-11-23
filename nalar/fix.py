"""Test-driven repair: verify the committed snapshot, then propose a fix.

The working tree is never touched: verification runs on an exported snapshot and
the model stages a proposal in memory. `nalar fix` prints the patch for the
normal review/apply path; it never applies on its own.
"""

from .coding import solve
from .preflight import PreflightError, verify_patch, verify_snapshot

MAX_ATTEMPTS = 3


def verification_feedback(verification, diff=""):
    """Bounded, model-facing summary of a failed verification run."""
    lines = ["Verification failed; fix the code before replying."]
    if verification.get("error"):
        lines.append(f"error: {verification['error']}")
    if verification.get("detail"):
        lines.append(verification["detail"])
    from .verify import status_text

    for item in verification.get("commands", []):
        lines.append(f"$ {' '.join(item['argv'])} ({status_text(item)})\n"
                     f"{item['output'][:2_000]}")
    if diff and verification.get("error") in {"patch_rejected", "no_patch"}:
        lines.append("Failed patch:\n" + diff[:4_000])
    return "\n".join(lines)[:6_000]


def run_fix(repository, chat, config, *, initial=None, attempts=2, timeout=300,
            on_event=None, budget=None, approve=None, belt=None, system=None,
            instructions="", context=""):
    """Verify the snapshot and, on failure, ask the model for a corrected patch."""
    if not 0 <= attempts <= MAX_ATTEMPTS:
        raise ValueError(f"attempts must be between 0 and {MAX_ATTEMPTS}")
    if not config.verify:
        raise ValueError("no verify commands are configured")
    if not isinstance(instructions, str) or not isinstance(context, str):
        raise ValueError("instructions and context must be strings")
    if len(instructions) > 4_000:
        raise ValueError("instructions must be at most 4000 characters")
    if len(context) > 32_000:
        raise ValueError("context is too large for the fix prompt")
    if repository.patch():
        raise ValueError("A proposal is staged; apply or reset it before fixing.")
    secret_names = (config.provider.api_key_env,)
    supplied = initial is not None
    if initial is None:
        try:
            initial = verify_snapshot(repository, config.verify, timeout=timeout,
                                      secret_names=secret_names)
        except PreflightError as exc:
            initial = {"ok": False, "error": str(exc), "commands": []}
    payload = {
        "base": repository.commit.decode(),
        "attempts": 0,
        "verification": initial,
        "patch": "",
        "status": "ok" if initial.get("ok") else "failed",
        "already_ok": bool(initial.get("ok")),
        "tokens": 0, "turns": 0, "calls": 0,
    }
    if on_event is not None and not supplied:
        on_event({"type": "verify", "ok": payload["already_ok"], "attempt": 0})
    if payload["already_ok"] or attempts == 0:
        return payload
    if initial.get("error") and not initial.get("commands"):
        # Export/setup failure, not a failing command: a patch cannot help.
        return payload
    if chat is None:
        raise ValueError("A model is required to propose a fix.")
    extra = ""
    if instructions.strip():
        extra += "\n\nAdditional instructions:\n" + instructions.strip()
    if context.strip():
        extra += "\n\n" + context.strip()

    def build_prompt(feedback):
        return ("The committed snapshot fails the configured verification. Fix the code "
                "and produce a corrected patch; do not claim tests you did not run.\n\n"
                + feedback + extra)

    prompt = build_prompt(verification_feedback(initial))
    for used in range(1, attempts + 1):
        if on_event is not None:
            on_event({"type": "fix_attempt", "attempt": used})
        before = repository.snapshot()
        spent_before = budget.spent if budget is not None else None
        try:
            result, diff = solve(
                chat, repository, prompt, mode="recover", belt=belt, system=system,
                max_turns=config.max_turns, max_calls=config.max_calls,
                context_budget=config.context_budget, token_budget=config.token_budget,
                approve=approve, on_event=on_event, budget=budget,
                retries=config.retries)
        except BaseException:
            repository.restore(before)
            raise
        if spent_before is not None:
            payload["tokens"] += max(0, budget.spent - spent_before)
        elif result.tokens:
            payload["tokens"] += result.tokens
        payload["turns"] += result.turns
        payload["calls"] += result.calls
        if result.status != "ok" or not diff:
            repository.restore(before)
            payload["attempts"] = used
            payload["error"] = result.status if result.status != "ok" else "no_patch"
            break
        try:
            verification = verify_patch(repository, diff, config.verify,
                                        timeout=timeout, secret_names=secret_names)
        except PreflightError as exc:
            verification = {"ok": False, "error": str(exc), "commands": []}
        rejected = verification.get("error") in {"patch_rejected", "apply_failed"}
        fatal = bool(verification.get("error")) and not verification.get("commands")
        if rejected or fatal:
            repository.restore(before)
            payload["attempts"] = used
            break
        payload.update({"patch": diff, "verification": verification,
                        "attempts": used,
                        "status": "ok" if verification.get("ok") else "failed"})
        if on_event is not None:
            on_event({"type": "verify", "ok": verification.get("ok"), "attempt": used})
        if verification.get("ok"):
            break
        prompt = build_prompt(verification_feedback(verification, diff))
    return payload
