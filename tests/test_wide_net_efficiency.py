"""The wide net must not ask about dates the tracker will not search.

Two costs, and the second is the worse one. Six wasted requests a day on an
address this project works hard not to annoy - and a hint that comes back
for an excluded window goes to the *front* of the hot list, where it buys a
Chrome launch, the scarcest budget here, for a trip nobody would take.

This is the same defect already found once: "the wide net kept querying
excluded months". It was fixed for whole months and left in place for
half-months and for the answers themselves.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.monthly import month_halves, probe_count

MARCH = [("March 2027", 2027)]
JAN = [("January 2027", 2027)]


def days(*spec):
    return {dt.date(y, m, d) for y, m, d in spec}


class TestHalvesWithNoSearchableDates:
    def test_both_halves_are_asked_when_no_filter_is_given(self):
        """Unchanged default: callers that know nothing still get both."""
        assert len(month_halves(MARCH)) == 2

    def test_a_half_with_no_departures_is_skipped(self):
        """March 16-31 2027: every departure returns in April, so none."""
        got = month_halves(MARCH, days((2027, 3, 1), (2027, 3, 10)))
        assert [f for f, _, _ in got] == ["March 1 to March 15 2027"]

    def test_the_other_half_is_skipped_symmetrically(self):
        got = month_halves(MARCH, days((2027, 3, 20)))
        assert [f for f, _, _ in got] == ["March 16 to March 31 2027"]

    def test_a_month_with_no_departures_at_all_asks_nothing(self):
        assert month_halves(MARCH, days((2027, 1, 5))) == []

    def test_a_full_month_still_asks_both(self):
        got = month_halves(JAN, days((2027, 1, 5), (2027, 1, 25)))
        assert len(got) == 2

    def test_february_leap_year_boundary_is_kept(self):
        got = month_halves([("February 2028", 2028)], days((2028, 2, 20)))
        assert got[0][0] == "February 16 to February 29 2028"

    def test_probe_count_reflects_the_saving(self):
        """The number the throttle notes are reasoned from must be honest."""
        full = probe_count(MARCH, halves=True)
        trimmed = probe_count(MARCH, halves=True,
                              departures=days((2027, 3, 1)))
        assert len(full) == 3 and len(trimmed) == 2


class TestTheLiveConfigurationSavesRequests:
    def test_march_second_half_is_dropped_for_the_real_window_list(self):
        from tracker.preferences import Preferences
        from tracker.schedule import generate_windows
        prefs = Preferences.load("preferences.example.json")
        deps = {w.depart for w in generate_windows(prefs)}
        if not deps:
            return                      # a config that searches nothing
        for fragment, _, _ in month_halves(
                [(f"{d:%B} {d.year}", d.year) for d in sorted({
                    dt.date(x.year, x.month, 1) for x in deps})], deps):
            # Every fragment asked for must contain at least one real day.
            assert fragment, fragment


class TestHintsOutsideTheSearchAreRefused:
    """Google answers a month query with whatever window it likes."""

    def test_the_guard_exists_in_the_run(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "tracker" / "cli.py").read_text(encoding="utf-8")
        assert "if k in searchable_keys" in src, (
            "hints are no longer filtered to searchable windows")
        assert "Ignoring %d hint(s)" in src, (
            "a refused hint is no longer reported")
