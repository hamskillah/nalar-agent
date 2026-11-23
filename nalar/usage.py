"""Helpers for delegated token usage reported by extensions."""

MAX_TOKENS = 10**12


def take_usage(usage):
    """Snapshot and clear a delegated-usage list, tolerating None."""
    if usage is None:
        return []
    items = list(usage)
    usage.clear()
    return items


def usage_totals(usage):
    """Aggregate positive token counts per model from delegated entries.

    Malformed entries are ignored so a broken extension result cannot break
    transcript recording.
    """
    totals = {}
    for item in usage or ():
        if not isinstance(item, dict):
            continue
        label = str(item.get("model") or "").strip()[:200]
        if isinstance(item.get("tokens"), bool):
            continue
        try:
            amount = int(item.get("tokens") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if label and 0 < amount <= MAX_TOKENS:
            totals[label] = min(MAX_TOKENS, totals.get(label, 0) + amount)
    return totals
