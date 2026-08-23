"""Date generation and the hot-list / rotation coverage strategy."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
import pytest

from tracker.preferences import Preferences
from tracker.schedule import (
    RotationState, ScanPlan, Window, build_plan, coverage_days,
    estimate_requests, generate_windows, hot_keys_from_history, take_slice,
)

TODAY = date(2026, 8, 22)


def prefs(**kw):
    base = dict(alert_email="u@e.com", earliest_departure="2027-01-05",
                latest_departure="2027-03-31", trip_weeks=[2, 3, 4, 5],
                departure_step_days=3, destinations=["NRT", "HND", "KIX"],
                extra_nights=[])   # these tests are about the grid, not fares
    base.update(kw)
    return Preferences(**base)


class TestWindowGeneration:
    def test_respects_trip_lengths(self):
        ws = generate_windows(prefs(trip_weeks=[2, 4]), today=TODAY)
        assert {w.nights for w in ws} == {14, 28}

    def test_steps_departures(self):
        ws = generate_windows(prefs(trip_weeks=[2], departure_step_days=7),
                              today=TODAY)
        departures = sorted({w.depart for w in ws})
        assert (departures[1] - departures[0]).days == 7

    def test_stays_inside_window(self):
        p = prefs()
        early, late = p.window
        for w in generate_windows(p, today=TODAY):
            assert early <= w.depart <= late

    def test_skips_departures_too_soon(self):
        p = prefs(earliest_departure=TODAY.isoformat(),
                  latest_departure=(TODAY + timedelta(days=60)).isoformat())
        ws = generate_windows(p, today=TODAY, min_lead_days=30)
        assert all((w.depart - TODAY).days >= 30 for w in ws)

    def test_empty_when_window_fully_past(self):
        p = prefs(earliest_departure="2020-01-01", latest_departure="2020-02-01")
        assert generate_windows(p, today=TODAY) == []

    def test_no_duplicates(self):
        ws = generate_windows(prefs(trip_weeks=[2, 2]), today=TODAY)
        assert len({w.key for w in ws}) == len(ws)

    def test_adding_a_bookable_length_grows_the_space(self):
        a = len(generate_windows(prefs(trip_weeks=[2, 3]), today=TODAY))
        b = len(generate_windows(prefs(trip_weeks=[2, 3, 4]), today=TODAY))
        assert b > a

    def test_only_an_absurd_length_is_ignored(self):
        """MAX_STAY_NIGHTS catches a typo; it must not clip a real trip."""
        a = len(generate_windows(prefs(trip_weeks=[2, 3, 4]), today=TODAY))
        real = len(generate_windows(prefs(trip_weeks=[2, 3, 4, 6]), today=TODAY))
        absurd = len(generate_windows(prefs(trip_weeks=[2, 3, 4], extra_nights=[400]),
                                      today=TODAY))
        assert real > a, "six weeks is a real trip and must be searched"
        assert absurd == a


class TestScale:
    def test_full_scan_would_be_far_too_big(self):
        """This is the reason rotation exists.

        Stated against a realistic per-run budget rather than a magic
        number, so trimming an unbookable trip length does not look like a
        regression: what matters is that one run cannot cover the space.
        """
        budget = 24
        combos, searches = estimate_requests(prefs(), today=TODAY)
        assert combos > budget * 3
        assert searches > budget * 10

    def test_plan_stays_inside_budget(self):
        plan = build_plan(prefs(), request_budget=24, today=TODAY)
        assert plan.request_estimate <= 24 + len(plan.destinations)

    def test_tiny_budget_still_produces_work(self):
        plan = build_plan(prefs(), request_budget=1, today=TODAY)
        assert len(plan.windows) >= 1


class TestSlicing:
    def test_partitions_without_gaps(self):
        items = [Window(date(2027, 1, 1) + timedelta(days=i),
                        date(2027, 1, 15) + timedelta(days=i)) for i in range(10)]
        seen = []
        _, total = take_slice(items, index=0, size=3)
        for i in range(total):
            chunk, _ = take_slice(items, index=i, size=3)
            seen.extend(w.key for w in chunk)
        assert sorted(seen) == sorted(w.key for w in items)

    def test_index_wraps(self):
        items = [Window(date(2027, 1, 1), date(2027, 1, 15))]
        a, _ = take_slice(items, index=0, size=1)
        b, _ = take_slice(items, index=99, size=1)
        assert a[0].key == b[0].key

    def test_empty_input(self):
        assert take_slice([], index=0, size=5) == ([], 1)


class TestRotation:
    def test_advances_and_wraps(self):
        r = RotationState()
        for _ in range(5):
            r.advance(5)
        assert r.slice_index == 0

    def test_persists(self, tmp_path):
        p = tmp_path / "rot.json"
        r = RotationState(); r.advance(10); r.advance(10)
        r.save(p)
        assert RotationState.load(p).slice_index == 2

    def test_corrupt_resets(self, tmp_path):
        p = tmp_path / "rot.json"; p.write_text("nope")
        assert RotationState.load(p).slice_index == 0

    def test_successive_runs_see_different_windows(self):
        p, rot = prefs(), RotationState()
        first = build_plan(p, rotation=rot, request_budget=12, today=TODAY)
        rot.advance(first.slices_total, first.priority_slices_total)
        second = build_plan(p, rotation=rot, request_budget=12, today=TODAY)
        assert {w.key for w in first.windows} != {w.key for w in second.windows}

    def test_whole_space_covered_within_a_cycle(self):
        """Both pools together must eventually cover every window."""
        p, rot = prefs(), RotationState()
        seen: set[str] = set()
        plan = build_plan(p, rotation=rot, request_budget=12, today=TODAY)
        cycles = max(plan.slices_total, plan.priority_slices_total)
        for _ in range(cycles):
            plan = build_plan(p, rotation=rot, request_budget=12, today=TODAY)
            seen.update(w.key for w in plan.windows)
            rot.advance(plan.slices_total, plan.priority_slices_total)
        assert len(seen) == len(generate_windows(p, today=TODAY))


class TestHotList:
    def test_cheapest_windows_win(self):
        rows = [
            {"depart_date": "2027-02-10", "return_date": "2027-02-24", "price_usd": "1290"},
            {"depart_date": "2027-01-05", "return_date": "2027-01-19", "price_usd": "1900"},
            {"depart_date": "2027-03-01", "return_date": "2027-03-15", "price_usd": "1100"},
        ]
        assert hot_keys_from_history(rows, limit=2) == [
            "2027-03-01_2027-03-15", "2027-02-10_2027-02-24"]

    def test_keeps_min_price_per_window(self):
        rows = [
            {"depart_date": "2027-02-10", "return_date": "2027-02-24", "price_usd": "1800"},
            {"depart_date": "2027-02-10", "return_date": "2027-02-24", "price_usd": "1200"},
            {"depart_date": "2027-03-01", "return_date": "2027-03-15", "price_usd": "1500"},
        ]
        assert hot_keys_from_history(rows, limit=1) == ["2027-02-10_2027-02-24"]

    def test_ignores_bad_rows(self):
        rows = [{"depart_date": "", "return_date": "", "price_usd": "1"},
                {"depart_date": "2027-02-10", "return_date": "2027-02-24",
                 "price_usd": "abc"}]
        assert hot_keys_from_history(rows) == []

    def test_hot_windows_always_included(self):
        """The point of the hot list: cheap options get re-priced every run."""
        p = prefs()
        key = generate_windows(p, today=TODAY)[7].key
        for idx in range(4):
            plan = build_plan(p, hot_keys=[key],
                              rotation=RotationState(slice_index=idx),
                              request_budget=18, today=TODAY)
            assert key in {w.key for w in plan.windows}

    def test_unknown_hot_keys_ignored(self):
        plan = build_plan(prefs(), hot_keys=["1999-01-01_1999-01-15"],
                          request_budget=18, today=TODAY)
        assert plan.hot == []


class TestCoverage:
    def test_more_runs_cover_faster(self):
        assert coverage_days(28, 4) == 7.0
        assert coverage_days(28, 2) == 14.0

    def test_describe(self):
        assert "slice" in build_plan(prefs(), request_budget=18,
                                     today=TODAY).describe()


class TestRollingWindow:
    """Requirement: search anywhere in the next 8 months, rolling forward."""

    def rolling(self, **kw):
        base = dict(alert_email="u@e.com", search_months=8, min_lead_days=21,
                    trip_weeks=[2, 3, 4, 5], departure_step_days=4,
                    priority_months=[1, 2, 3], destinations=["NRT", "HND", "KIX"])
        base.update(kw)
        return Preferences(**base)

    def test_window_spans_eight_months(self):
        early, late = self.rolling().window_on(TODAY)
        assert early == date(2026, 9, 12)      # today + 21 days lead
        assert late == date(2027, 4, 22)       # today + 8 months

    def test_window_moves_with_the_calendar(self):
        p = self.rolling()
        a = p.window_on(date(2026, 8, 22))
        b = p.window_on(date(2026, 9, 22))
        assert b[0] > a[0] and b[1] > a[1]

    def test_never_runs_out_of_dates(self):
        """A pinned window eventually empties; a rolling one cannot."""
        p = self.rolling()
        for month in range(1, 13):
            day = date(2027, month, 15)
            assert generate_windows(p, today=day), f"empty on {day}"

    def test_pinned_dates_override_rolling(self):
        p = self.rolling(earliest_departure="2027-01-05",
                         latest_departure="2027-03-31")
        assert not p.is_rolling
        assert p.window_on(TODAY) == (date(2027, 1, 5), date(2027, 3, 31))

    def test_respects_minimum_lead_time(self):
        p = self.rolling(min_lead_days=45)
        assert all((w.depart - TODAY).days >= 45
                   for w in generate_windows(p, today=TODAY))

    def test_eight_months_is_a_big_space(self):
        combos, searches = estimate_requests(self.rolling(), today=TODAY)
        assert combos > 200 and searches > 600


class TestPriorityMonths:
    """Requirement: concentrate the search on the chosen months."""

    def rolling(self, **kw):
        base = dict(alert_email="u@e.com", search_months=8, min_lead_days=21,
                    trip_weeks=[2, 3, 4, 5], departure_step_days=4,
                    priority_months=[1, 2, 3], destinations=["NRT", "HND", "KIX"])
        base.update(kw)
        return Preferences(**base)

    def test_windows_are_flagged(self):
        ws = generate_windows(self.rolling(), today=TODAY)
        assert all(w.priority == (w.depart.month in {1, 2, 3}) for w in ws)

    def test_half_of_each_run_goes_to_priority(self):
        p = self.rolling()
        plan = build_plan(p, request_budget=24, today=TODAY)
        got = sum(1 for w in plan.windows if w.priority)
        assert got / len(plan.windows) >= 0.5

    def test_priority_is_oversampled_versus_its_share_of_the_space(self):
        """Priority months are 41% of the space but get 50% of the budget."""
        p = self.rolling()
        ws = generate_windows(p, today=TODAY)
        natural = sum(1 for w in ws if w.priority) / len(ws)
        plan = build_plan(p, request_budget=24, today=TODAY)
        actual = sum(1 for w in plan.windows if w.priority) / len(plan.windows)
        assert actual > natural

    def test_priority_pool_cycles_faster(self):
        """A smaller pool should be revisited more often, not less."""
        p = self.rolling()
        plan = build_plan(p, request_budget=24, today=TODAY)
        assert plan.priority_slices_total < plan.slices_total

    def test_cursors_advance_independently(self):
        rot = RotationState()
        rot.advance(30, 5)
        rot.advance(30, 5)
        assert rot.slice_index == 2 and rot.priority_index == 2
        for _ in range(4):
            rot.advance(30, 5)
        assert rot.priority_index == 1 and rot.slice_index == 6

    def test_no_priority_months_puts_everything_in_general(self):
        p = self.rolling(priority_months=[])
        plan = build_plan(p, request_budget=24, today=TODAY)
        assert plan.priority_cold == [] and plan.cold

    def test_all_priority_months_puts_everything_in_priority(self):
        p = self.rolling(priority_months=list(range(1, 13)))
        plan = build_plan(p, request_budget=24, today=TODAY)
        assert plan.cold == [] and plan.priority_cold

    def test_budget_is_not_wasted_when_a_pool_is_empty(self):
        p = self.rolling(priority_months=list(range(1, 13)))
        plan = build_plan(p, request_budget=24, today=TODAY)
        assert plan.request_estimate >= 20
