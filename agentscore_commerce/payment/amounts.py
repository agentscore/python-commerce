"""USD ↔ atomic-unit conversion for token amounts.

`usd_to_atomic(usd, decimals=6)` returns the integer atomic value of a USD
amount for a token with `decimals` places of precision (USDC is 6). Uses
``Decimal`` + ``ROUND_HALF_UP`` so a USD value at exactly half a base unit
rounds away from zero, matching the cross-language Node sibling.

Rejects negative, NaN, and infinite inputs. Scientific-notation strings
(``"1e6"``) are accepted on the Python side via ``Decimal``; the Node sibling
rejects them and requires fixed notation, so cross-language byte-parity tests
fix on fixed-notation fixtures.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def usd_to_atomic(usd: str | float | int | Decimal, *, decimals: int) -> int:
    """Convert a USD amount to atomic units for a token with ``decimals`` places.

    Args:
        usd: USD amount. Strings (``"1.23"``), ``float`` (``1.23``), ``int``,
            and ``Decimal`` instances are accepted. The value is converted via
            ``str()`` before parsing with ``Decimal``.
        decimals: Number of decimal places in the atomic unit (6 for USDC,
            18 for ETH, etc.). Must be a non-negative ``int``.

    Returns:
        Integer atomic units. ``1.23`` with ``decimals=6`` returns ``1_230_000``.

    Raises:
        ValueError: if ``usd`` is negative, NaN, infinite, or unparseable, or
            if ``decimals`` is not a non-negative ``int``.
    """
    if not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
        msg = f"decimals must be a non-negative int, got {decimals!r}"
        raise ValueError(msg)

    # Strip whitespace on string input so Python matches Node's `.trim()` behavior
    # (Decimal itself rejects whitespace-padded strings with InvalidOperation).
    raw = usd.strip() if isinstance(usd, str) else usd
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        msg = f"invalid usd value: {usd!r}"
        raise ValueError(msg) from exc

    if not amount.is_finite():
        msg = f"usd must be finite, got {usd!r}"
        raise ValueError(msg)
    if amount < 0:
        msg = f"usd must be non-negative, got {amount}"
        raise ValueError(msg)

    scaled = (amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_HALF_UP)
    return int(scaled)


def format_usd_cents(cents: int) -> str:
    """Format an integer cent amount as a fixed-2-decimal USD string.

    ``500`` → ``"5.00"``. Negative values are formatted with a leading minus.
    Use everywhere a merchant emits ``f"{cents / 100:.2f}"`` today; consistent
    formatting across catalog rows, order responses, and 402 bodies prevents
    agent-side string-comparison flakiness.
    """
    return f"{cents / 100:.2f}"
