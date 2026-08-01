from __future__ import annotations

import json
from collections.abc import Mapping

from barrier_free_mobility_agent.domain.errors import AgentBudgetExceededError


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON-compatible state deterministically for the application hard gate."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def merged_state_size_bytes(
    current: Mapping[str, object],
    update: Mapping[str, object],
) -> int:
    merged = dict(current)
    merged.update(update)
    return len(canonical_json_bytes(merged))


def ensure_merged_state_within_budget(
    current: Mapping[str, object],
    update: Mapping[str, object],
    *,
    limit: int,
) -> int:
    actual = merged_state_size_bytes(current, update)
    if actual > limit:
        raise AgentBudgetExceededError(
            context={"budget": "state_bytes", "limit": limit, "actual": actual}
        )
    return actual


def ensure_interrupt_payload_within_budget(payload: object, *, limit: int) -> int:
    actual = len(canonical_json_bytes(payload))
    if actual > limit:
        raise AgentBudgetExceededError(
            context={
                "budget": "interrupt_payload_bytes",
                "limit": limit,
                "actual": actual,
            }
        )
    return actual
