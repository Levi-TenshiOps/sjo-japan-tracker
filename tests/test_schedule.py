"""Date generation and the hot-list / rotation coverage strategy."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
import pytest

from tracker.preferences import Preferences, PreferencesError
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


class TestExcludedMonths:
    """A month the trip owner rules out must cost nothing at all.

    Added 2026-08-23 when September was dropped from the search. The obvious
    alternative - pinning earliest_departure past it - would silently switch
    the 8-month window from rolling to fixed, so the horizon would stop
    moving forward and quietly go stale. Excluding by month number keeps the
    window rolling.
    """

    def prefs(self, excluded=(), priority=(1, 2, 3)):
        return Preferences(
            alert_email="a@b.c", search_months=8, min_lead_days=21,
            departure_step_days=1, trip_weeks=[3, 4], extra_nights=[],
            destinations=["TYO"], priority_months=list(priority),
            excluded_months=list(excluded))

    def test_no_window_departs_in_an_excluded_month(self):
        ws = generate_windows(self.prefs(excluded=[9]), today=date(2026, 8, 23))
        assert ws, "excluding one month must not empty the search"
        assert not any(w.depart.month == 9 for w in ws)

    def test_the_other_months_are_untouched(self):
        base = generate_windows(self.prefs(), today=date(2026, 8, 23))
        cut = generate_windows(self.prefs(excluded=[9]), today=date(2026, 8, 23))
        assert {w.key for w in cut} == {w.key for w in base if w.depart.month != 9}

    def test_a_trip_returning_in_an_excluded_month_still_counts(self):
        """Departure month decides. An August trip home in September is an
        August trip, and excluding September must not delete it."""
        ws = generate_windows(self.prefs(excluded=[9]), today=date(2026, 8, 23))
        crossing = [w for w in ws
                    if w.depart.month == 10 and w.back.month == 11]
        assert crossing, "windows crossing a month boundary must survive"

    def test_excluding_nothing_changes_nothing(self):
        a = generate_windows(self.prefs(), today=date(2026, 8, 23))
        b = generate_windows(self.prefs(excluded=[]), today=date(2026, 8, 23))
        assert {w.key for w in a} == {w.key for w in b}

    def test_a_month_cannot_be_both_priority_and_excluded(self):
        with pytest.raises(PreferencesError, match="both a priority and excluded"):
            self.prefs(excluded=[1], priority=[1, 2, 3]).validate()

    def test_excluding_every_month_is_refused(self):
        with pytest.raises(PreferencesError, match="nothing to search"):
            self.prefs(excluded=list(range(1, 13)), priority=[]).validate()

    def test_a_nonsense_month_number_is_refused(self):
        with pytest.raises(PreferencesError, match="not a month number"):
            self.prefs(excluded=[13]).validate()

    def test_the_window_stays_rolling(self):
        """The whole reason for this feature rather than a pinned date."""
        assert self.prefs(excluded=[9]).is_rolling

    def test_status_names_the_excluded_months(self):
        text = self.prefs(excluded=[9]).describe(today=date(2026, 8, 23))
        assert "September" in text and "never searched" in text

    def test_status_stays_quiet_when_nothing_is_excluded(self):
        assert "Excluded" not in self.prefs().describe(today=date(2026, 8, 23))


class TestIncludedMonths:
    """Naming months makes them the search, rather than a filter on it.

    Added 2026-08-23. The trip owner asked why April was being searched; the
    answer was that nobody had chosen it - it was simply the tail of an
    8-month rolling horizon. `search_months` is now only the horizon, i.e.
    how far ahead to look for the months actually named.
    """

    def prefs(self, included=(), excluded=(), priority=(1, 2, 3), horizon=8):
        return Preferences(
            alert_email="a@b.c", search_months=horizon, min_lead_days=21,
            departure_step_days=1, trip_weeks=[3, 4], extra_nights=[],
            destinations=["TYO"], priority_months=list(priority),
            included_months=list(included), excluded_months=list(excluded))

    def test_only_the_named_months_are_searched(self):
        p = self.prefs(included=[1, 2, 3, 10, 11, 12])
        ws = generate_windows(p, today=date(2026, 8, 23))
        assert {w.depart.month for w in ws} == {1, 2, 3, 10, 11, 12}

    def test_the_trip_owners_six_months_in_order(self):
        """The exact configuration in use."""
        p = self.prefs(included=[1, 2, 3, 10, 11, 12])
        assert p.searched_months(today=date(2026, 8, 23)) == [
            (2026, 10), (2026, 11), (2026, 12),
            (2027, 1), (2027, 2), (2027, 3)]

    def test_april_is_gone(self):
        """It was only ever there as the tail of the horizon."""
        ws = generate_windows(self.prefs(included=[1, 2, 3, 10, 11, 12]),
                              today=date(2026, 8, 23))
        assert not any(w.depart.month == 4 for w in ws)

    def test_one_month_is_a_valid_search(self):
        """'it can be 1, 6 or any number we want'."""
        p = self.prefs(included=[2], priority=[2])
        ws = generate_windows(p, today=date(2026, 8, 23))
        assert ws and {w.depart.month for w in ws} == {2}

    def test_empty_means_every_month_as_before(self):
        a = generate_windows(self.prefs(), today=date(2026, 8, 23))
        b = generate_windows(self.prefs(included=[]), today=date(2026, 8, 23))
        assert {w.key for w in a} == {w.key for w in b}

    def test_exclusion_still_applies_on_top(self):
        p = self.prefs(included=[1, 2, 3, 10, 11, 12], excluded=[11])
        ws = generate_windows(p, today=date(2026, 8, 23))
        assert {w.depart.month for w in ws} == {1, 2, 3, 10, 12}

    def test_the_window_stays_rolling(self):
        assert self.prefs(included=[1, 2, 3]).is_rolling

    def test_a_priority_month_must_be_searched(self):
        with pytest.raises(PreferencesError, match="not in included_months"):
            self.prefs(included=[10, 11, 12], priority=[1, 2, 3]).validate()

    def test_excluding_everything_included_is_refused(self):
        with pytest.raises(PreferencesError, match="nothing to search"):
            self.prefs(included=[10], excluded=[10], priority=[]).validate()

    def test_a_nonsense_month_number_is_refused(self):
        with pytest.raises(PreferencesError, match="not a month number"):
            self.prefs(included=[0]).validate()


class TestAMonthTheHorizonCannotReach:
    """Silently searching nothing is the worst failure mode here.

    A named month outside the horizon produces no windows, which looks
    exactly like a month with no cheap fares. It has to be reported.
    """

    def prefs(self, included, horizon=8):
        return Preferences(
            alert_email="a@b.c", search_months=horizon, min_lead_days=21,
            departure_step_days=1, trip_weeks=[3], extra_nights=[],
            destinations=["TYO"], priority_months=[],
            included_months=list(included))

    def test_it_is_reported(self):
        """From August, an 8-month horizon reaches only to April."""
        p = self.prefs([1, 6])
        assert p.unreachable_months(today=date(2026, 8, 23)) == [6]

    def test_reachable_months_are_not_reported(self):
        p = self.prefs([1, 2, 3, 10, 11, 12])
        assert p.unreachable_months(today=date(2026, 8, 23)) == []

    def test_a_longer_horizon_reaches_it(self):
        p = self.prefs([1, 6], horizon=12)
        assert p.unreachable_months(today=date(2026, 8, 23)) == []

    def test_status_shouts_about_it(self):
        text = self.prefs([1, 6]).describe(today=date(2026, 8, 23))
        assert "NOT REACHED" in text and "June" in text

    def test_status_stays_quiet_when_all_are_reachable(self):
        text = self.prefs([1, 2, 3]).describe(today=date(2026, 8, 23))
        assert "NOT REACHED" not in text

    def test_nothing_named_means_nothing_unreachable(self):
        assert self.prefs([]).unreachable_months(today=date(2026, 8, 23)) == []


class TestBugsFoundInTheMonthAudit:
    """Five defects the first month-filtering pass shipped, 2026-08-23.

    All five passed the suite that existed when they were written, which is
    the point: the tests covered the happy path of a feature and none of the
    seams around it.
    """

    def prefs(self, **kw):
        base = dict(alert_email="a@b.c", search_months=8, min_lead_days=21,
                    departure_step_days=1, trip_weeks=[3], extra_nights=[],
                    destinations=["TYO"], priority_months=[])
        base.update(kw)
        return Preferences(**base)

    def test_an_excluded_month_is_not_called_unreachable(self):
        """It is reachable and deliberately skipped.

        Reporting it as outside the horizon sends the reader off to raise
        search_months, which would change nothing at all.
        """
        p = self.prefs(included_months=[10, 11], excluded_months=[11])
        assert p.unreachable_months(today=date(2026, 8, 23)) == []
        assert p.searched_months(today=date(2026, 8, 23)) == [(2026, 10)]

    def test_a_genuinely_unreachable_month_is_still_caught(self):
        """The fix must not silence the real case."""
        p = self.prefs(included_months=[1, 6])
        assert p.unreachable_months(today=date(2026, 8, 23)) == [6]

    def test_the_default_description_reads_properly(self):
        """'Depart in next 8 months' - the article was lost in an edit."""
        line = self.prefs().describe(today=date(2026, 8, 23)).splitlines()[0]
        assert line.startswith("Depart in the next 8 months"), line

    def test_the_named_month_description_still_reads_properly(self):
        line = self.prefs(included_months=[1, 2]).describe(
            today=date(2026, 8, 23)).splitlines()[0]
        assert line.startswith("Depart in January 2027, February 2027"), line

    def test_months_in_horizon_ignores_the_month_filters(self):
        """It answers 'what can the horizon reach', nothing else."""
        wide = self.prefs().months_in_horizon(today=date(2026, 8, 23))
        narrow = self.prefs(included_months=[1]).months_in_horizon(
            today=date(2026, 8, 23))
        assert wide == narrow


class TestTheWholeTripMustBeInSearchedMonths:
    """Filtering on the departure day alone models the wrong thing.

    Raised by the trip owner 2026-08-23: "a departure day on 31 Mar 2027
    won't make sense, because the trip duration minimum is 21 days". They
    were right. With October to March searched and 21-38 night trips, 531
    windows - 16% of the grid - returned in April or May. A 2027-03-31
    departure returns between 21 April and 8 May: the entire holiday
    happens in months that were deliberately excluded.
    """

    def prefs(self, whole=True, **kw):
        base = dict(alert_email="a@b.c", search_months=8, min_lead_days=21,
                    departure_step_days=1, trip_weeks=[3], extra_nights=list(range(21, 39)),
                    destinations=["TYO"], priority_months=[],
                    included_months=[1, 2, 3, 10, 11, 12],
                    whole_trip_in_searched_months=whole)
        base.update(kw)
        return Preferences(**base)

    def windows(self, **kw):
        return generate_windows(self.prefs(**kw), today=date(2026, 8, 23))

    def test_no_trip_returns_outside_the_searched_months(self):
        assert all(w.back.month in {1, 2, 3, 10, 11, 12}
                   for w in self.windows())

    def test_the_last_departure_is_the_one_that_can_still_come_home(self):
        """March 10 + 21 nights lands exactly on March 31."""
        ws = self.windows()
        assert max(w.depart for w in ws) == date(2027, 3, 10)
        assert max(w.back for w in ws) == date(2027, 3, 31)

    def test_march_tapers_rather_than_stopping_dead(self):
        """Each March day keeps only the lengths that still end in March."""
        ws = self.windows()
        by_day = {}
        for w in ws:
            if w.depart.month == 3:
                by_day.setdefault(w.depart, []).append(w)
        assert len(by_day[date(2027, 3, 1)]) == 10     # 21n..30n
        assert len(by_day[date(2027, 3, 10)]) == 1     # 21n only
        assert date(2027, 3, 11) not in by_day

    def test_the_count_is_what_the_arithmetic_says(self):
        """February loses 7+6+..+1 = 28; March keeps 10+9+..+1 = 55."""
        ws = self.windows()
        feb = [w for w in ws if w.depart.month == 2]
        mar = [w for w in ws if w.depart.month == 3]
        assert len(feb) == 28 * 18 - 28
        assert len(mar) == 55
        assert len(ws) == 2745

    def test_a_trip_passing_through_an_excluded_month_is_dropped(self):
        """Testing only the two ends is not enough.

        At 21-38 nights a trip spans up to three calendar months, so a
        middle month must be checked too - otherwise a trip could pass
        straight through an excluded month.
        """
        p = self.prefs(included_months=[1, 3], priority_months=[])
        # 31 January + 38 nights = 10 March: starts and ends in an included
        # month, but the whole of February is excluded.
        assert p.trip_is_searchable(date(2027, 1, 31), date(2027, 3, 10)) is False

    def test_turning_it_off_restores_departure_only_filtering(self):
        loose = self.windows(whole=False)
        assert len(loose) == 3276
        assert any(w.back.month == 4 for w in loose)

    def test_it_is_a_no_op_when_no_months_are_filtered(self):
        p = self.prefs(included_months=[], excluded_months=[])
        assert p.trip_is_searchable(date(2027, 3, 31), date(2027, 5, 8)) is True
