"""When is it safe to raise the sweep rate or the Chrome budget?

The old rule was "do not raise it while the health line reports an empty
rate above ~20%". That number cannot be used any more: the empty rate
measures the calendar, not the connection, which is the finding of
2026-08-24. So readiness is judged on things that mean something, and it
is a command rather than a feeling.

Nothing here - and nothing in `readiness_report` - queries Google.
Asking Google whether it is still refusing is what turned a short throttle
into an hour of one on 2026-08-23.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.sweeper import READY_QUIET_HOURS, SweepStore, readiness_report
from tracker.throttle import ThrottleState


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def healthy_store(**kw):
    s = SweepStore()
    s.last_throttle = ago(READY_QUIET_HOURS + 1)
    s.consecutive_rests = 0
    for i in range(12):
        s.checked[f"w{i}"] = {"at": ago(1), "empty": False, "blank": False,
                              "healthy": True, "secs": 15.0}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def run(store=None, throttle=None, hours_since_email=2.0):
    return readiness_report(store or healthy_store(),
                            throttle_state=throttle or ThrottleState(),
                            hours_since_email=hours_since_email)


class TestTheHappyPath:
    def test_a_quiet_day_is_ready(self):
        ready, lines = run()
        assert ready, "\n".join(lines)
        assert "READY." in "\n".join(lines)

    def test_it_says_to_raise_one_thing_at_a_time(self):
        _, lines = run()
        text = "\n".join(lines)
        assert "ONE thing" in text
        assert "Never both at once" in text

    def test_a_store_that_never_throttled_is_ready(self):
        s = healthy_store()
        s.last_throttle = ""
        ready, _ = run(s)
        assert ready


class TestEachGate:
    def test_a_recent_throttle_blocks_it(self):
        s = healthy_store()
        s.last_throttle = ago(0.5)
        ready, lines = run(s)
        assert not ready
        assert "quiet since the last throttle" in "\n".join(lines)

    def test_the_boundary_is_the_quiet_window(self):
        s = healthy_store()
        s.last_throttle = ago(READY_QUIET_HOURS - 0.1)
        assert not run(s)[0]
        s.last_throttle = ago(READY_QUIET_HOURS + 0.1)
        assert run(s)[0]

    def test_backing_off_blocks_it(self):
        assert not run(healthy_store(consecutive_rests=2))[0]

    def test_an_outstanding_blocked_alarm_blocks_it(self):
        t = ThrottleState()
        t.blocked_alarm_sent = True
        assert not run(throttle=t)[0]

    def test_a_degrading_grid_blocks_it(self):
        t = ThrottleState()
        t.consecutive_bad = 4
        assert not run(throttle=t)[0]

    def test_silence_blocks_it(self):
        """If the product is not delivering, do not touch the traffic."""
        assert not run(hours_since_email=20.0)[0]
        assert not run(hours_since_email=None)[0]


class TestTheTimingGate:
    def test_too_few_samples_is_not_a_blocker(self):
        s = healthy_store()
        s.checked = {"only": {"at": ago(1), "blank": False, "secs": 15.0,
                              "empty": False, "healthy": True}}
        ready, lines = run(s)
        assert ready
        assert "not judged" in "\n".join(lines)

    def test_pages_slowing_badly_blocks_it(self):
        s = healthy_store()
        for k in s.checked:
            s.checked[k]["secs"] = 90.0
        assert not run(s)[0]

    def test_blank_pages_are_ignored_by_the_timing_gate(self):
        """Empty pages are always fast; only real pages say anything."""
        s = healthy_store()
        for i in range(30):
            s.checked[f"blank{i}"] = {"at": ago(1), "empty": True,
                                      "blank": True, "healthy": False,
                                      "secs": 3.9}
        assert run(s)[0]


class TestItNeverTouchesTheNetwork:
    def test_the_report_makes_no_requests(self, monkeypatch):
        import tracker.sweeper as sw

        def boom(*a, **k):
            raise AssertionError("readiness queried Google")

        monkeypatch.setattr(sw, "fetch_dom", boom, raising=False)
        run()


class TestReportingCommandsNeverWriteTheStore:
    """`--status`, `--coverage` and `--readiness` only report.

    The sweep holds the store in memory and rewrites it after every window,
    so a second process writing it is the "two sweepers" hazard: the
    cursors overwrite each other and coverage silently goes backwards.

    `--coverage` was missed when the other two were excluded, so asking how
    complete the sweep was could perturb the very thing being asked about.
    Found 2026-08-24 after it had been run twice against a live sweep.
    """

    def _guard(self) -> str:
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        return (root / "sweep_forever.py").read_text(encoding="utf-8")

    def test_coverage_is_treated_as_read_only(self):
        src = self._guard()
        assert "read_only = args.status or args.readiness or args.coverage" in src

    def test_the_startup_prune_is_behind_that_guard(self):
        src = self._guard()
        i = src.find("read_only = args.status")
        j = src.find("store.prune()", i)
        assert i > 0 and j > i, "prune no longer follows the read_only guard"
        assert "if not read_only:" in src[i:j]

    def test_forgetting_health_is_behind_it_too(self):
        src = self._guard()
        assert "if not read_only and store.forget_stale_health():" in src
