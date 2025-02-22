"""Money and token-estimate unit tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cost_firewall.models import Message
from cost_firewall.money import format_usd, micros_to_usd, price_micros, usd_to_micros
from cost_firewall.tokens import estimate_message_tokens, estimate_text_tokens


def test_usd_to_micros_rounds_half_up() -> None:
    assert usd_to_micros(Decimal("0.0000005")) == 1
    assert usd_to_micros(Decimal("0.0000004")) == 0
    assert usd_to_micros(Decimal("1")) == 1_000_000


def test_price_micros_is_exact_for_per_million_quotes() -> None:
    # 1M tokens at $0.15/1M = $0.15 = 150_000 micros, with no float drift.
    assert price_micros(1_000_000, Decimal("0.15")) == 150_000
    assert price_micros(1_000, Decimal("0.15")) == 150
    assert price_micros(0, Decimal("12.5")) == 0


def test_price_micros_rounds_half_up_at_boundary() -> None:
    assert price_micros(1, Decimal("0.5")) == 1
    assert price_micros(1, Decimal("0.49")) == 0


def test_price_micros_rejects_negative_tokens() -> None:
    with pytest.raises(ValueError):
        price_micros(-1, Decimal("1"))


def test_micros_round_trip_and_format() -> None:
    assert micros_to_usd(150_000) == Decimal("0.150000")
    assert format_usd(150_000) == "$0.150000"
    assert format_usd(1) == "$0.000001"


def test_estimate_text_tokens_rounds_up_and_never_returns_zero() -> None:
    assert estimate_text_tokens("") == 1
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2


def test_estimate_message_tokens_includes_framing() -> None:
    messages = [Message(role="user", content="abcd")]
    assert estimate_message_tokens(messages) == 1 + 4
    assert estimate_message_tokens([]) == 0
