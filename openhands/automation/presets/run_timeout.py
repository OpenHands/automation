import math


RUN_TIMEOUT_SAFETY_MARGIN_SECONDS = 60.0
MIN_RUN_TIMEOUT_SECONDS = 30.0


def resolve_run_timeout(raw_budget, elapsed_seconds):
    try:
        budget = float(raw_budget)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(budget) or budget <= 0:
        return None
    remaining = budget - elapsed_seconds - RUN_TIMEOUT_SAFETY_MARGIN_SECONDS
    return max(remaining, MIN_RUN_TIMEOUT_SECONDS)
