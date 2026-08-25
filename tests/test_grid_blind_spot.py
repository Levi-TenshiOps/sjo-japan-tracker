"""The HTTP grid must not spend requests on stays it cannot see.

The server-rendered HTML this path reads carries no prices past ~30
nights, while the same URL in a browser shows them - which is how a $1,390
32-night fare was nearly excluded for good in August 2026.

Measured across all of `price_history.csv`: **509 grid fares at 30 nights
or fewer, and zero at 31 or more.**

The waste is the smaller half. Those empties feed `throttle.py`, which
cuts the grid's budget - already floored at 8 - so a structural blind spot
was being read as a bad connection and answered by making the grid
smaller. On 2026-08-24 the rotation walked onto 31-36 night stays and the
empty rate stepped from a stable 25% to 75% for three consecutive runs;
every empty window was 31 nights or longer, and not one was under.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.cli import collect


class Plan:
    destinations = ("TYO",)

    def __init__(self, pairs):
        self._pairs = pairs

    def date_pairs(self):
        return self._pairs

    def describe(self):
        return "test"


def Cfg(limit=30):
    """The real Config, so this exercises what a run actually carries."""
    from tracker import config as config_mod
    cfg = config_mod.Config()
    cfg.origins = ["SJO"]
    cfg.max_stops = 2
    cfg.http_max_nights = limit
    return cfg


class Searcher:
    """Records what it was actually asked to fetch, and nothing else."""

    def __init__(self):
        self.asked = []
        self.requests_made = 0
        self.barren_requests = 0
        self.empty_requests = 0

    def run_all(self, queries):
        self.asked = list(queries)
        return []


def pairs(*nights, start=date(2027, 1, 4)):
    return [(start, start + timedelta(days=n)) for n in nights]


def nights_asked(searcher):
    return sorted((q.inbound - q.outbound).days for q in searcher.asked)


class TestLongStaysAreNotAsked:
    def test_a_31_night_stay_is_skipped(self):
        s = Searcher()
        collect(Cfg(), Plan(pairs(27, 31)), s)
        assert nights_asked(s) == [27]

    def test_the_boundary_is_inclusive_at_30(self):
        s = Searcher()
        collect(Cfg(), Plan(pairs(29, 30, 31)), s)
        assert nights_asked(s) == [29, 30]

    def test_the_live_blind_spot_range_is_all_skipped(self):
        """31-36 nights: exactly what the 75%-empty runs were asking."""
        s = Searcher()
        collect(Cfg(), Plan(pairs(21, 31, 32, 33, 34, 35, 36)), s)
        assert nights_asked(s) == [21]

    def test_zero_disables_the_skip(self):
        """The escape hatch, if Google ever starts rendering them."""
        s = Searcher()
        collect(Cfg(limit=0), Plan(pairs(27, 34)), s)
        assert nights_asked(s) == [27, 34]


class TestItNeverEmptiesTheRun:
    def test_a_plan_of_only_long_stays_still_searches_them(self):
        """Better a request that probably fails than a run with none.

        Skipping everything would return "no travel windows left", which
        aborts the grid and, on a day the sweep is also down, the email.
        """
        s = Searcher()
        collect(Cfg(), Plan(pairs(33, 34)), s)
        assert nights_asked(s) == [33, 34]

    def test_an_empty_plan_is_still_reported(self):
        s = Searcher()
        _, _, errors, _, _ = collect(Cfg(), Plan([]), s)
        assert errors and "no travel windows" in errors[0]


class TestOneWayPairsSurvive:
    def test_a_missing_return_is_not_dropped(self):
        """`date_pairs` may carry (depart, None); it must not crash here."""
        s = Searcher()
        collect(Cfg(), Plan([(date(2027, 1, 4), None)]), s)
        assert len(s.asked) == 1
