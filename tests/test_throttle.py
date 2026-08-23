"""Adaptive request budget: measure, don't guess."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

from tracker.throttle import (
    MAX_BUDGET, MIN_BUDGET, START_BUDGET, ThrottleState,
    recommended_runs_per_day,
)


class TestAdaptation:
    def test_clean_runs_grow_the_budget(self):
        t = ThrottleState()
        t.record(requests=20, empty=0)
        assert t.budget > START_BUDGET

    def test_throttled_runs_shrink_it(self):
        t = ThrottleState()
        t.record(requests=20, empty=15)     # 75% empty
        assert t.budget < START_BUDGET

    def test_shrink_is_faster_than_growth(self):
        """Backing off should be decisive; recovering should be cautious."""
        a = ThrottleState(); a.record(requests=20, empty=15)
        drop = START_BUDGET - a.budget
        b = ThrottleState(); b.record(requests=20, empty=0)
        gain = b.budget - START_BUDGET
        assert drop > gain

    def test_never_below_floor(self):
        t = ThrottleState()
        for _ in range(30):
            t.record(requests=20, empty=20)
        assert t.budget == MIN_BUDGET

    def test_never_above_ceiling(self):
        t = ThrottleState()
        for _ in range(50):
            t.record(requests=20, empty=0)
        assert t.budget == MAX_BUDGET

    def test_middling_rate_holds_steady(self):
        t = ThrottleState()
        before = t.budget
        t.record(requests=20, empty=6)      # 30%: noisy but not blocked
        assert t.budget == before

    def test_recovers_after_backing_off(self):
        t = ThrottleState()
        for _ in range(3):
            t.record(requests=20, empty=18)
        low = t.budget
        for _ in range(10):
            t.record(requests=20, empty=0)
        assert t.budget > low

    def test_zero_requests_is_neutral(self):
        t = ThrottleState()
        before = t.budget
        t.record(requests=0, empty=0)
        assert t.budget == before


class TestBlockDetection:
    def test_three_bad_runs_flags_it(self):
        t = ThrottleState()
        for _ in range(3):
            t.record(requests=20, empty=18)
        assert t.looks_blocked

    def test_two_bad_runs_do_not(self):
        t = ThrottleState()
        for _ in range(2):
            t.record(requests=20, empty=18)
        assert not t.looks_blocked

    def test_one_good_run_clears_the_streak(self):
        t = ThrottleState()
        t.record(requests=20, empty=18)
        t.record(requests=20, empty=18)
        t.record(requests=20, empty=0)
        t.record(requests=20, empty=18)
        assert not t.looks_blocked

    def test_advice_mentions_cloud_when_blocked(self):
        t = ThrottleState()
        for _ in range(4):
            t.record(requests=20, empty=20)
        assert "home connection" in t.advice(4)

    def test_advice_reports_daily_total(self):
        assert "requests/day" in ThrottleState().advice(4)


class TestHistory:
    def test_keeps_a_bounded_window(self):
        t = ThrottleState()
        for _ in range(50):
            t.record(requests=10, empty=1)
        assert len(t.recent) <= 10

    def test_empty_rate_across_runs(self):
        t = ThrottleState()
        t.record(requests=10, empty=1)
        t.record(requests=10, empty=3)
        assert t.empty_rate == pytest.approx(0.2)

    def test_persists(self, tmp_path):
        p = tmp_path / "throttle.json"
        t = ThrottleState(); t.record(requests=20, empty=15); t.save(p)
        assert ThrottleState.load(p).budget == t.budget

    def test_corrupt_resets(self, tmp_path):
        p = tmp_path / "t.json"; p.write_text("~~~")
        assert ThrottleState.load(p).budget == START_BUDGET


class TestRunSplitting:
    def test_more_runs_for_a_bigger_daily_budget(self):
        assert recommended_runs_per_day(96, 24) == 4
        assert recommended_runs_per_day(48, 24) == 2

    def test_capped_at_six(self):
        assert recommended_runs_per_day(10_000, 10) == 6

    def test_at_least_one(self):
        assert recommended_runs_per_day(5, 24) == 1

    def test_same_daily_load_more_runs_is_smaller_bursts(self):
        """The core correction: runs/day is the wrong unit, requests are."""
        daily = 96
        assert daily // recommended_runs_per_day(daily, 24) == 24
        assert daily // recommended_runs_per_day(daily, 16) == 16
