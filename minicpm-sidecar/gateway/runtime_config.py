"""Live-reloadable runtime config for the chat pipeline.

The Electron app POSTs assistant preferences to /api/config; they take
effect on the next request without a sidecar restart. Pure in-memory —
the Electron host owns persistence (it re-sends the patch on spawn).
"""

from __future__ import annotations

from typing import Any, Dict

CLARIFY_STRENGTHS = ("off", "ambiguous", "confirm_all")

ADDRESS_MAX_CHARS = 24

RUNTIME: Dict[str, Any] = {
    "assistant_address": "sir",
    "clarify_strength": "ambiguous",
    "auto_memory": True,
    "briefing_hour": 8,
    "recap_hour": 21,
}


def get() -> Dict[str, Any]:
    """Snapshot copy of the current runtime config."""
    return dict(RUNTIME)


def update(patch: dict) -> Dict[str, Any]:
    """Validate + apply a partial config patch; return the new state.

    - assistant_address : str, truncated to 24 chars; blank → default "sir"
    - clarify_strength  : one of CLARIFY_STRENGTHS (else ValueError)
    - auto_memory       : bool-coerced
    - briefing_hour     : int-clamped to 0–23
    - recap_hour        : int-clamped to 0–23

    Unknown keys are ignored. Raises ValueError on invalid enum values so
    the HTTP layer can surface a 422.
    """
    if not isinstance(patch, dict):
        raise ValueError("config patch must be an object")

    if "assistant_address" in patch:
        text = str(patch["assistant_address"] or "").strip()
        RUNTIME["assistant_address"] = text[:ADDRESS_MAX_CHARS] or "sir"

    if "clarify_strength" in patch:
        strength = str(patch["clarify_strength"] or "").strip().lower()
        if strength not in CLARIFY_STRENGTHS:
            raise ValueError(
                f"clarify_strength must be one of {list(CLARIFY_STRENGTHS)}, "
                f"got {strength!r}"
            )
        RUNTIME["clarify_strength"] = strength

    if "auto_memory" in patch:
        RUNTIME["auto_memory"] = bool(patch["auto_memory"])

    if "briefing_hour" in patch:
        try:
            hour = int(patch["briefing_hour"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"briefing_hour must be an integer: {patch['briefing_hour']!r}") from exc
        RUNTIME["briefing_hour"] = max(0, min(23, hour))

    if "recap_hour" in patch:
        try:
            hour = int(patch["recap_hour"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"recap_hour must be an integer: {patch['recap_hour']!r}") from exc
        RUNTIME["recap_hour"] = max(0, min(23, hour))

    return get()
