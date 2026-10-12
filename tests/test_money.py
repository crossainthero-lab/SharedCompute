import pytest

from sharedcompute import money


@pytest.mark.parametrize("value,cents", [("2.00", 200), ("0.01", 1), (3, 300), (0.1, 10), ("100000", 10_000_000)])
def test_parse_usd(value, cents):
    assert money.parse_usd(value) == cents


@pytest.mark.parametrize("value", ["-1", "1.001", "abc", None, True, "NaN", "Infinity", "100000.01", ""])
def test_parse_usd_rejects(value):
    with pytest.raises(ValueError):
        money.parse_usd(value)


def test_quote_rounds_half_up_with_one_cent_minimum():
    assert money.quote_cents(44, 60) == 44
    assert money.quote_cents(24, 45) == 18
    assert money.quote_cents(30, 1) == 1    # 0.5 cents rounds up
    assert money.quote_cents(1, 1) == 1     # minimum charge
    assert money.quote_cents(67, 90) == 101  # 100.5 -> 101


def test_metered_cents():
    assert money.metered_cents(3600, 1) == 1
    assert money.metered_cents(30, 3.2) == 1
    assert money.metered_cents(120, 1800) == 60


@pytest.mark.parametrize("total", [1, 5, 44, 99, 100, 101, 12345])
def test_split_always_sums_and_favours_provider_on_ties(total):
    provider, platform = money.split_cents(total, 1000)
    assert provider + platform == total
    assert platform >= 0 and provider >= 0
    assert abs(provider - total * 0.9) <= 0.5


def test_split_matches_v01_draft_examples():
    assert money.split_cents(44, 1000) == (40, 4)
    assert money.split_cents(5, 1000) == (5, 0)       # 4.5 rounds to the provider
    assert money.split_cents(9000, 1000) == (8100, 900)  # $90/month example from the blueprint


def test_split_respects_configured_fee():
    assert money.split_cents(1000, 0) == (1000, 0)
    assert money.split_cents(1000, 2500) == (750, 250)
