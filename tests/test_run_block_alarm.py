"""The six scheduled runs must be able to say when Google stops answering.

Until 2026-08-24 only `sweep_forever.py` could raise a block alarm. The
sweep is a separate process that is routinely stopped - it was down for
hours on 2026-08-23 - and while it is down nothing watches the scheduled
runs at all. The 12:26 run on 2026-08-24 came back empty on the HTTP grid
(6 of 8) and on all nine Chrome launches, and told nobody.

These tests pin the detector's shape, not just its happy path, because the
previous throttle detector shipped with a false-positive that emailed the
trip owner at 70% on a healthy day.
"""
from __future__ import annotations

from tracker.alarm import run_blocked_email, run_recovered_email
from tracker.cli import BLOCKED_GRID_RATE, run_looks_blocked
from tracker.throttle import ThrottleState


class TestBothChannelsMustBeDark:
    """One quiet channel is ordinary; two at once is a refusal."""

    def test_the_12_26_blackout_is_caught(self):
        # The run this was written for: grid 6/8 empty, Chrome 9/9 blank.
        assert run_looks_blocked(chrome_attempts=9, chrome_blank=9,
                                 grid_requests=8, grid_empty=6)

    def test_a_healthy_run_is_silent(self):
        # The 10:45 run the same morning: Chrome found fares, grid 2/8.
        assert not run_looks_blocked(chrome_attempts=10, chrome_blank=1,
                                     grid_requests=8, grid_empty=2)

    def test_a_quiet_grid_alone_is_not_a_block(self):
        # The grid is empty on most windows by design and its budget has
        # been floored at 8 since 2026-08-23. On its own it proves nothing.
        assert not run_looks_blocked(chrome_attempts=9, chrome_blank=0,
                                     grid_requests=8, grid_empty=8)

    def test_blank_chrome_alone_is_not_a_block(self):
        # Chrome is one process sharing one profile directory. A corrupt
        # profile looks exactly like a throttle from here, so it needs the
        # grid's corroboration before it wakes anybody.
        assert not run_looks_blocked(chrome_attempts=9, chrome_blank=9,
                                     grid_requests=8, grid_empty=0)

    def test_one_surviving_window_clears_it(self):
        assert not run_looks_blocked(chrome_attempts=9, chrome_blank=8,
                                     grid_requests=8, grid_empty=8)


class TestItRefusesToGuess:
    def test_too_few_launches_to_conclude(self):
        # chrome_max_per_run can be configured low, and two blanks in a row
        # is a normal Tuesday.
        assert not run_looks_blocked(chrome_attempts=2, chrome_blank=2,
                                     grid_requests=8, grid_empty=8)

    def test_chrome_disabled_never_alarms(self):
        assert not run_looks_blocked(chrome_attempts=0, chrome_blank=0,
                                     grid_requests=8, grid_empty=8)

    def test_no_grid_requests_never_alarms(self):
        # Division by zero, and no corroboration either.
        assert not run_looks_blocked(chrome_attempts=9, chrome_blank=9,
                                     grid_requests=0, grid_empty=0)

    def test_the_grid_threshold_is_a_floor_not_a_ceiling(self):
        n = 8
        at = int(BLOCKED_GRID_RATE * n)
        assert run_looks_blocked(chrome_attempts=9, chrome_blank=9,
                                 grid_requests=n, grid_empty=at)
        assert not run_looks_blocked(chrome_attempts=9, chrome_blank=9,
                                     grid_requests=n, grid_empty=at - 1)


class TestTheAlarmIsSentOnceNotSixTimesADay:
    def test_the_flag_persists_across_runs(self, tmp_path):
        p = tmp_path / "throttle.json"
        s = ThrottleState()
        assert s.blocked_alarm_sent is False
        s.blocked_alarm_sent = True
        s.save(p)
        assert ThrottleState.load(p).blocked_alarm_sent is True

    def test_an_old_file_without_the_field_still_loads(self, tmp_path):
        p = tmp_path / "throttle.json"
        p.write_text('{"budget": 8, "consecutive_bad": 1}', encoding="utf-8")
        assert ThrottleState.load(p).blocked_alarm_sent is False


class TestTheEmailsSayWhatHappened:
    def test_blocked_email_names_both_channels(self):
        c = run_blocked_email(chrome_blank=9, chrome_attempts=9,
                              grid_rate=75.0, when="12:26")
        body = c.text
        assert "9 of 9" in body
        assert "75%" in body
        assert "12:26" in body
        # The one instruction that matters, learned on 2026-08-23.
        assert "Do not query Google" in body

    def test_blocked_email_promises_the_email_still_arrives(self):
        c = run_blocked_email(chrome_blank=9, chrome_attempts=9,
                              grid_rate=75.0, when="12:26")
        assert "not lost" in c.text

    def test_recovered_email_carries_the_price(self):
        c = run_recovered_email(when="15:45", cheapest="$1,347")
        assert "$1,347" in c.text
        assert "15:45" in c.text
