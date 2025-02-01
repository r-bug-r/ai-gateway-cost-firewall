"""Integer micro-USD money helpers.

All persisted money is stored as integer micro-USD (1 USD = 1_000_000 micros) so
budget arithmetic is exact and comparable across processes.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICROS_PER_USD = 1_000_000


def decimal_from_config(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number, got {type(value).__name__}")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite")
    return amount


def usd_to_micros(value: Decimal) -> int:
    """Convert USD to micro-USD with half-up rounding."""
    return int((value * MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def micros_to_usd(value: int) -> Decimal:
    return (Decimal(value) / MICROS_PER_USD).quantize(Decimal("0.000001"))


def format_usd(value: int) -> str:
    return f"${micros_to_usd(value):.6f}"


def price_micros(tokens: int, usd_per_1m_tokens: Decimal) -> int:
    """Cost in micro-USD for `tokens` at a price quoted per 1M tokens.

    One token at $p per 1M tokens costs exactly p micro-USD, so the arithmetic
    is a single multiplication with half-up rounding.
    """
    if tokens < 0:
        raise ValueError("tokens must be non-negative")
    return int((Decimal(tokens) * usd_per_1m_tokens).to_integral_value(rounding=ROUND_HALF_UP))
