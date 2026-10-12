"""Money is stored as integer US cents. All rounding is half-up, per job."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_USD = Decimal("100000")


def parse_usd(value: object, field: str = "Amount") -> int:
    """Parse a dollar amount (string or number, max two decimals) into cents."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a USD amount")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a USD amount") from None
    if not dec.is_finite() or dec < 0 or dec > MAX_USD:
        raise ValueError(f"{field} must be between $0 and $100,000")
    if dec.as_tuple().exponent < -2:
        raise ValueError(f"{field} can have at most two decimal places")
    return int(dec * 100)


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quote_cents(hourly_cents: int, minutes: int) -> int:
    """Price of `minutes` GPU-minutes at an hourly rate. Minimum charge is one cent."""
    return max(1, _round(Decimal(hourly_cents) * minutes / 60))


def metered_cents(hourly_cents: int, seconds: float) -> int:
    """Price of measured runtime. Minimum charge is one cent."""
    return max(1, _round(Decimal(hourly_cents) * Decimal(str(seconds)) / 3600))


def split_cents(total_cents: int, fee_bps: int) -> tuple[int, int]:
    """Split a charge into (provider_cents, platform_cents).

    The provider share is rounded half-up and the platform keeps the remainder,
    so ties favour the provider and the two parts always sum to the total.
    """
    provider = _round(Decimal(total_cents) * (10_000 - fee_bps) / 10_000)
    return provider, total_cents - provider


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
