"""Stub AI signal module — unused by default.

Future: optional ML / LLM overlay that can veto or confirm VWAP entries.
Wire into execution/loop.py only behind an explicit feature flag.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AiSignal:
    """Placeholder AI opinion on direction."""

    bias: str = "neutral"  # long | short | neutral
    confidence: float = 0.0
    note: str = "stub — not consulted"


def get_ai_signal(*_args, **_kwargs) -> AiSignal:
    """Return a no-op neutral signal. Not called by the default bot loop."""
    return AiSignal()
