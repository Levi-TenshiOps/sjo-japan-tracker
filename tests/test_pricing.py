"""Cheap / typical / expensive classification."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

from tracker.pricing import (
    MIN_HISTORY_DAYS,
    CRC_PER_USD, MIN_HISTORY_POINTS, SEED_BANDS, PriceBands,
    bands_from_history, resolve_bands, savings_vs_usual, verdict_sentence,
)


class TestSeedBands:
    def test_derived_from_google_email(self):
        """CRC 550k-1050k at the rate implied by the two screenshots."""
        assert SEED_BANDS.low == round(550_000 / CRC_PER_USD) == 1188
        assert SEED_BANDS.high == round(1_050_000 / CRC_PER_USD) == 2269
        assert SEED_BANDS.usual == round(615_055 / CRC_PER_USD) == 1329

    def test_fx_rate_matches_screenshots(self):
        """CRC 767,308 and $1,658 were the same Jan 15-24 itinerary."""
        assert round(767_308 / 1_658, 2) == CRC_PER_USD

    @pytest.mark.parametrize("price,band", [
        (900, "CHEAP"), (1187, "CHEAP"), (1188, "TYPICAL"),
        (1380, "TYPICAL"), (1658, "TYPICAL"), (2269, "TYPICAL"),
        (2270, "EXPENSIVE"), (5000, "EXPENSIVE"),
    ])
    def test_classification(self, price, band):
        assert SEED_BANDS.classify(price) == band

    def test_screenshot_price_is_typical(self):
        """Google itself labelled the $1,658 fare 'typical'. So must we."""
        assert SEED_BANDS.classify(1658) == "TYPICAL"

    def test_users_threshold_is_above_usual(self):
        """$1,380 sits above the $1,329 travellers usually pay — worth knowing."""
        assert SEED_BANDS.usual < 1380


class TestPosition:
    def test_always_clamped(self):
        for p in (1, 500, 1188, 1700, 2269, 9999, 100000):
            assert 0.0 <= SEED_BANDS.position(p) <= 1.0

    def test_cheap_lands_in_green_quarter(self):
        assert SEED_BANDS.position(800) <= 0.25

    def test_expensive_lands_in_red_quarter(self):
        assert SEED_BANDS.position(4000) >= 0.75

    def test_typical_lands_in_middle(self):
        assert 0.25 <= SEED_BANDS.position(1700) <= 0.75

    def test_monotonic(self):
        prices = [500, 1000, 1188, 1500, 2000, 2269, 3000, 5000]
        pos = [SEED_BANDS.position(p) for p in prices]
        assert pos == sorted(pos)


class TestHistoryBands:
    def test_too_little_data(self):
        assert bands_from_history([1200, 1300, 1400]) is None

    def test_enough_data(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        bands = bands_from_history(prices)
        assert bands is not None and bands.source == "HISTORY"
        assert bands.low < bands.usual < bands.high

    def test_percentiles_are_right(self):
        prices = list(range(100, 1100, 10))  # 100 values, 100..1090
        b = bands_from_history(prices)
        assert 280 <= b.low <= 300     # ~p20
        assert 890 <= b.high <= 910    # ~p80

    def test_flat_prices_rejected(self):
        assert bands_from_history([1200] * 50) is None

    def test_ignores_zero_and_negative(self):
        prices = [0, -5] + list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert bands_from_history(prices) is not None


class TestResolve:
    def test_prefers_google(self):
        g = PriceBands(low=1, high=2, usual=None, source="GOOGLE")
        assert resolve_bands(google_bands=g, history_prices=[1] * 100) is g

    def test_falls_back_to_history(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert resolve_bands(history_prices=prices).source == "HISTORY"

    def test_falls_back_to_seed(self):
        assert resolve_bands().source == "SEED"
        assert resolve_bands(history_prices=[1200, 1300]).source == "SEED"


class TestPresentation:
    def test_verdict_sentence(self):
        assert verdict_sentence(1000, SEED_BANDS) == "$1,000 is cheap for this route"
        assert verdict_sentence(1658, SEED_BANDS) == "$1,658 is typical for this route"

    def test_savings(self):
        assert savings_vs_usual(1200, SEED_BANDS) == 129
        assert savings_vs_usual(1400, SEED_BANDS) is None


class TestHistoryNeedsMultipleDays:
    """Regression: one run can log 25+ rows; that is a snapshot, not a trend."""

    def test_enough_rows_but_one_day_is_rejected(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert bands_from_history(prices, distinct_days=1) is None

    def test_enough_rows_and_enough_days_accepted(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert bands_from_history(prices, distinct_days=7) is not None

    def test_resolve_falls_back_to_seed_on_single_day(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert resolve_bands(history_prices=prices, history_days=1).source == "SEED"

    def test_resolve_uses_history_once_spread_out(self):
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert resolve_bands(history_prices=prices, history_days=9).source == "HISTORY"

    def test_unspecified_days_stays_permissive(self):
        """Callers that cannot count days keep the old behaviour."""
        prices = list(range(1000, 1000 + MIN_HISTORY_POINTS * 10, 10))
        assert bands_from_history(prices) is not None
