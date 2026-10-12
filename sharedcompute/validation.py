"""Input validation shared by services. Raises InvalidInput with the offending field."""
from __future__ import annotations

from . import money, regions
from .errors import InvalidInput


def text(value: object, field: str, label: str, *, min_len: int = 1, max_len: int = 80) -> str:
    if not isinstance(value, str):
        raise InvalidInput(f"{label} is required", field=field)
    value = " ".join(value.split())
    if not min_len <= len(value) <= max_len:
        raise InvalidInput(f"{label} must be {min_len}–{max_len} characters", field=field)
    if any(ord(c) < 32 or c in "<>" for c in value):
        raise InvalidInput(f"{label} contains characters that are not allowed", field=field)
    return value


def integer(value: object, field: str, label: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise InvalidInput(f"{label} must be a whole number", field=field)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise InvalidInput(f"{label} must be a whole number", field=field)
    if not low <= value <= high:
        raise InvalidInput(f"{label} must be between {low:,} and {high:,}", field=field)
    return value


def usd(value: object, field: str, label: str, *, min_cents: int = 1, max_cents: int | None = None) -> int:
    try:
        cents = money.parse_usd(value, label)
    except ValueError as exc:
        raise InvalidInput(str(exc), field=field) from None
    if cents < min_cents:
        raise InvalidInput(f"{label} must be at least {money.fmt(min_cents)}", field=field)
    if max_cents is not None and cents > max_cents:
        raise InvalidInput(f"{label} must be at most {money.fmt(max_cents)}", field=field)
    return cents


def region(value: object, field: str = "region", *, allow_any: bool = False) -> str:
    if allow_any and value in (None, "", regions.ANY):
        return regions.ANY
    if value not in regions.REGIONS:
        raise InvalidInput("Choose a supported region", field=field)
    return value  # type: ignore[return-value]


def boolean(value: object, field: str, label: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidInput(f"{label} must be true or false", field=field)
    return value
