"""`LAUNCH_SECONDS` turns a delay into a launch rate, so it has to be true.

It said 6.1s - from an early sample when a page rendered in about six
seconds - while 342 timed checks on 2026-08-25 measured a median of 12.1
and a mean of 13.8.

The error was not harmless, and raising the rate made it worse: a fixed
launch cost is a larger share of a shorter cycle. At a 40s delay,
believing 6.1s claims 78 launches an hour where there are 67, so the
derived hot share under-delivered freshness by about one refresh an hour.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.sweeper import (
    HOT_SHARE, LAUNCH_SECONDS, needed_hot_share,
)

MEASURED_MEAN = 13.8          # 2026-08-25, 342 samples


class TestTheConstantIsPlausible:
    def test_it_is_in_the_range_a_chrome_launch_takes(self):
        assert 8.0 <= LAUNCH_SECONDS <= 30.0, (
            f"{LAUNCH_SECONDS}s is not a plausible Chrome launch; the "
            f"measured mean is {MEASURED_MEAN}s")

    def test_it_errs_high_rather_than_low(self):
        """High overestimates the cycle, so it buys more freshness."""
        assert LAUNCH_SECONDS >= MEASURED_MEAN - 1.0


class TestFreshnessIsActuallyDelivered:
    """The share is derived; the question is whether it reaches the target."""

    def _delivered(self, n_hot, delay, real_cost=MEASURED_MEAN):
        share = needed_hot_share(n_hot, cycle_s=delay + LAUNCH_SECONDS,
                                 cap=HOT_SHARE)
        return share * (3600.0 / (delay + real_cost))

    def test_the_current_rate_meets_the_freshness_target(self):
        n = 89                                   # hot windows, live figure
        need = n / 10.0                          # sweep_max_age_hours
        assert self._delivered(n, 40) >= need * 0.98, (
            "the hot list would age past the email's staleness limit")

    def test_a_smaller_hot_list_needs_less(self):
        assert self._delivered(20, 40) >= 20 / 10.0 * 0.98

    def test_the_cap_still_binds(self):
        """It is a ceiling, not a setting - it must never be exceeded."""
        assert needed_hot_share(10_000, cycle_s=54.0, cap=HOT_SHARE) <= HOT_SHARE

    def test_no_hot_windows_asks_for_nothing(self):
        assert needed_hot_share(0, cycle_s=54.0, cap=HOT_SHARE) == 0.0

    def test_a_faster_rate_needs_a_smaller_share(self):
        """The whole point of deriving it rather than fixing it."""
        slow = needed_hot_share(89, cycle_s=90 + LAUNCH_SECONDS, cap=HOT_SHARE)
        fast = needed_hot_share(89, cycle_s=40 + LAUNCH_SECONDS, cap=HOT_SHARE)
        assert fast < slow, "the share did not fall as the rate rose"
