"""Each zone on the price bar must show two real numbers.

The trip owner read an email saying "$1,641 is cheap" above a green zone
labelled "under $2,213", and asked the obvious question: cheap reaching
down to *what*? The cut-offs are percentiles, so on their own they leave
both ends of the bar open - and the bottom end sat below the cheapest fare
that has ever existed on this route.

So the ends are closed with observed values: the cheapest fare recorded at
the bottom, the dearest at the top, from every Chrome observation the
project holds - most of which come from the background sweep, the only
thing that prices the whole calendar.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.email_render import band_ranges
from tracker.pricing import PriceBands

DASH = "\u2013"


def bands(low=2213, high=3202, seen_low=None, seen_high=None):
    return PriceBands(low=low, high=high, usual=2866, source="SEED",
                      seen_low=seen_low, seen_high=seen_high)


class TestClosedEnds:
    def test_cheap_shows_the_cheapest_ever_seen(self):
        cheap, _, _ = band_ranges(bands(seen_low=1347, seen_high=13127))
        assert cheap == f"$1,347 {DASH} $2,213"

    def test_expensive_shows_the_dearest_ever_seen(self):
        _, _, dear = band_ranges(bands(seen_low=1347, seen_high=13127))
        assert dear == f"$3,202 {DASH} $13,127"

    def test_typical_is_unchanged(self):
        _, typical, _ = band_ranges(bands(seen_low=1347, seen_high=13127))
        assert typical == f"$2,213 {DASH} $3,202"

    def test_all_three_zones_show_two_numbers(self):
        for zone in band_ranges(bands(seen_low=1347, seen_high=13127)):
            assert zone.count("$") == 2, zone


class TestItNeverInventsAnEnd:
    def test_no_observations_keeps_the_open_form(self):
        cheap, _, dear = band_ranges(bands())
        assert cheap == "under $2,213" and dear == "over $3,202"

    def test_a_minimum_inside_the_cheap_zone_is_not_used(self):
        """Nothing below the cut-off means the zone has no reached floor."""
        cheap, _, _ = band_ranges(bands(seen_low=2500, seen_high=13127))
        assert cheap == "under $2,213"

    def test_a_maximum_inside_the_typical_zone_is_not_used(self):
        _, _, dear = band_ranges(bands(seen_low=1347, seen_high=3000))
        assert dear == "over $3,202"

    def test_one_end_can_close_without_the_other(self):
        cheap, _, dear = band_ranges(bands(seen_low=1347))
        assert "$1,347" in cheap and dear == "over $3,202"


class TestTheObservedRangeComesFromTheData:
    def test_with_observed_takes_the_real_extremes(self):
        b = bands().with_observed([2400, 1347, 13127, 1800])
        assert (b.seen_low, b.seen_high) == (1347, 13127)

    def test_it_ignores_junk_and_zeroes(self):
        b = bands().with_observed([0, -5, 1347, 2400])
        assert b.seen_low == 1347

    def test_an_empty_list_changes_nothing(self):
        b = bands()
        assert b.with_observed([]) == b

    def test_the_cut_offs_are_untouched(self):
        b = bands().with_observed([1347, 13127])
        assert (b.low, b.high, b.source) == (2213, 3202, "SEED")

    def test_history_bands_carry_their_own_range(self):
        from tracker.pricing import bands_from_history
        prices = [1347 + i * 7 for i in range(200)]
        b = bands_from_history(prices, distinct_days=5)
        assert b is not None
        assert b.seen_low == min(prices) and b.seen_high == max(prices)


class TestTheDayCountUsesEveryLog:
    """The prices come from both logs, so the day count must too."""

    def test_the_union_is_taken_not_the_sum(self, tmp_path):
        from tracker.history import distinct_days_across
        head = "checked_at_utc,origin,destination,price_usd,band_source\n"
        a = tmp_path / "a.csv"
        b = tmp_path / "b.csv"
        a.write_text(head + "2026-08-23T01:00:00+00:00,SJO,TYO,1400,CHROME\n"
                            "2026-08-24T01:00:00+00:00,SJO,TYO,1500,CHROME\n",
                     encoding="utf-8")
        b.write_text(head + "2026-08-24T02:00:00+00:00,SJO,TYO,1600,CHROME\n"
                            "2026-08-25T02:00:00+00:00,SJO,TYO,1700,CHROME\n",
                     encoding="utf-8")
        assert distinct_days_across([a, b], origin="SJO") == 3

    def test_a_missing_file_is_skipped(self, tmp_path):
        from tracker.history import distinct_days_across
        head = "checked_at_utc,origin,destination,price_usd,band_source\n"
        a = tmp_path / "a.csv"
        a.write_text(head + "2026-08-23T01:00:00+00:00,SJO,TYO,1400,CHROME\n",
                     encoding="utf-8")
        assert distinct_days_across([a, tmp_path / "nope.csv"],
                                    origin="SJO") == 1

    def test_the_run_counts_across_both(self):
        import pathlib
        import re
        src = re.sub(r"\s+", " ", (pathlib.Path(__file__).resolve().parent.parent
                                   / "tracker" / "cli.py").read_text(encoding="utf-8"))
        assert "distinct_days_across(" in src, (
            "the day count went back to a single file")
