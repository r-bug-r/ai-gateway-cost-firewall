"""Deterministic token estimation used before a provider reports real usage.

The heuristic (four characters per token, four tokens of per-message framing) is
an estimate, never a claim about provider billing. Settled ledger rows prefer
provider-reported usage when the provider returns it.
"""

from __future__ import annotations

from collections.abc import Sequence

from cost_firewall.models import Message

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_text_tokens(text: str) -> int:
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    if not messages:
        return 0
    return sum(
        estimate_text_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS for message in messages
    )
