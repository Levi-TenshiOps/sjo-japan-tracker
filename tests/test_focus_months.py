"""Finish the months that matter before spending anything on the rest.

Asked for 2026-08-24: January, then February, then March, to 100% - where
100% means every window has a fare or an answer recorded while the
connection was healthy, not merely that the cursor has walked past it.

The constraint that shapes the design: a focus must redirect effort, never
ask for more of it. Nothing here changes the request rate.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io as _io

import pytest

from tracker.schedule import Window
from tracker.sweeper import (
    SweepStore, focus_pending, sweep_batch,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")


class FakeChrome:
    def __init__(self, dom=""):
        self.dom, self.calls, self.asked = dom, 0, []

    def __call__(self, url):
        self.calls += 1
        self.asked.append(url)
        return self.dom


def windows_across_months():
    """Three January, three February, three March, three October."""
    out = []
    for m in (1, 2, 3):
        for d in (4, 5, 6):
            out.append(Window(date(2027, m, d), date(2027, m, d) + timedelta(days=21)))
    for d in (4, 5, 6):
        out.append(Window(date(2026, 10, d), date(2026, 10, d) + timedelta(days=21)))
    return out


class TestWhatCountsAsAnswered:
    def test_a_window_with_a_fare_is_done(self):
        ws = windows_across_months()
        s = SweepStore()
        s.found[ws[0].key] = {"price_usd": 1347}
        assert ws[0] not in focus_pending(ws, s, [1])

    def test_a_healthy_empty_is_done(self):
        ws = windows_across_months()
        s = SweepStore()
        s.checked[ws[0].key] = {"healthy": True, "empty": True}
        assert ws[0] not in focus_pending(ws, s, [1])

    def test_an_untrusted_empty_is_not_done(self):
        """The whole point: an empty seen on a doubtful connection."""
        ws = windows_across_months()
        s = SweepStore()
        s.checked[ws[0].key] = {"healthy": False, "empty": True}
        assert ws[0] in focus_pending(ws, s, [1])

    def test_a_window_never_walked_is_not_done(self):
        ws = windows_across_months()
        assert ws[0] in focus_pending(ws, SweepStore(), [1])

    def test_months_outside_the_focus_are_never_pending(self):
        ws = windows_across_months()
        pend = focus_pending(ws, SweepStore(), [1])
        assert {w.depart.month for w in pend} == {1}


class TestTheOrderIsTheOrderAsked:
    def test_january_comes_before_february_before_march(self):
        ws = windows_across_months()
        pend = focus_pending(ws, SweepStore(), [1, 2, 3])
        assert [w.depart.month for w in pend] == [1, 1, 1, 2, 2, 2, 3, 3, 3]

    def test_a_different_order_is_honoured(self):
        ws = windows_across_months()
        pend = focus_pending(ws, SweepStore(), [3, 1])
        assert [w.depart.month for w in pend] == [3, 3, 3, 1, 1, 1]

    def test_within_a_month_it_goes_by_date(self):
        ws = windows_across_months()
        pend = focus_pending(ws, SweepStore(), [1])
        assert [w.depart.day for w in pend] == [4, 5, 6]


class TestTheSweepActuallyFocuses:
    def test_it_prices_the_focus_months_first(self):
        """Three January windows, three launches - all of them January.

        Not more than three: once January is answered the focus is done
        and the ordinary rotation resumes, which is the point of it.
        """
        ws = windows_across_months()
        s = SweepStore()
        f = FakeChrome(_io.open(FIXTURE, encoding="utf-8").read())
        sweep_batch(ws, s, batch=3, fetch=f, sleep=lambda _: None,
                    delay_s=0, focus_months=[1])
        assert len(f.asked) == 3
        # The URL carries the dates protobuf-encoded, so read what was
        # actually recorded rather than trying to parse them back out.
        assert {k[:7] for k in s.checked} == {"2027-01"}, s.checked

    def test_the_cold_cursor_does_not_move(self):
        """So the deferred months resume exactly where they stopped."""
        ws = windows_across_months()
        s = SweepStore()
        s.cursor = 9                       # part-way into October
        sweep_batch(ws, s, batch=4, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert s.cursor == 9, "the focus walked the cold cursor forward"

    def test_without_a_focus_the_cursor_advances_as_before(self):
        ws = windows_across_months()
        s = SweepStore()
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor > 0

    def test_a_finished_focus_hands_back_to_the_rotation(self):
        ws = windows_across_months()
        s = SweepStore()
        # Answer every January window so the focus has nothing left.
        for w in ws:
            if w.depart.month == 1:
                s.checked[w.key] = {"healthy": True, "empty": True}
        before = s.cursor
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert s.cursor > before, "the rotation never resumed"

    def test_a_window_that_answers_leaves_the_queue(self):
        ws = windows_across_months()
        s = SweepStore()
        jan = [w.key for w in ws if w.depart.month == 1]
        s.suspect = list(jan)
        sweep_batch(ws, s, batch=3,
                    fetch=FakeChrome(_io.open(FIXTURE, encoding="utf-8").read()),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert not [k for k in s.suspect if k in jan], (
            "a window that returned a fare stayed queued")

    def test_a_window_that_stays_empty_stays_queued(self):
        """An empty answer on a doubtful connection is not an answer.

        This is the behaviour the focus exists to exploit, so it must not
        be mistaken for the queue failing to drain.
        """
        ws = windows_across_months()
        s = SweepStore()
        jan = [w.key for w in ws if w.depart.month == 1]
        s.suspect = list(jan)
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert set(s.suspect) >= set(jan), "an untrusted empty left the queue"


class TestItNeverAsksForMoreRequests:
    def test_one_launch_still_prices_one_window(self):
        ws = windows_across_months()
        s = SweepStore()
        f = FakeChrome("")
        sweep_batch(ws, s, batch=5, fetch=f, sleep=lambda _: None,
                    delay_s=0, focus_months=[1, 2, 3])
        assert f.calls == 5, "a focus changed the request count"

    def test_the_delay_is_still_honoured(self):
        ws = windows_across_months()
        s = SweepStore()
        naps: list = []
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=naps.append, delay_s=90.0, focus_months=[1])
        assert naps, "the focus skipped the pacing"
        assert all(n > 0 for n in naps)


class TestAnEmptyWindowCanActuallyBeFinished:
    """The bug that made both the queue and a focus unfinishable.

    `healthy` used to require the page to have taken at least
    SUSPECT_FAST_SECONDS. An empty page never does - measured 2026-08-24, a
    date with no flights answers in 3.6-4.6s, always - so a genuinely empty
    window could never be marked healthy, never left the re-check queue,
    and never left `focus_pending`.

    The live proof was the backlog sitting at ~1,330 through five hours of
    continuous sweeping, rising as often as falling, and a focus observed
    re-pricing 2027-01-03 +33n on consecutive launches for ever.

    An empty is trusted when something nearby came back with fares -
    positive evidence - and not on the strength of its own timing.
    """

    def _all_empty_then_a_fare(self):
        """A fetch that answers with fares once, then always empty."""
        dom = _io.open(FIXTURE, encoding="utf-8").read()
        state = {"n": 0}

        def fetch(url):
            state["n"] += 1
            return dom if state["n"] == 1 else ""

        return fetch

    def test_an_empty_leaves_the_queue_once_something_has_worked(self):
        ws = windows_across_months()
        s = SweepStore()
        sweep_batch(ws, s, batch=3, fetch=self._all_empty_then_a_fare(),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        # The first window returned a fare, proving the connection; the
        # empties after it are therefore believed rather than re-queued.
        trusted = [k for k, v in s.checked.items() if v.get("healthy")]
        assert trusted, "no empty was ever trusted, so nothing can finish"

    def test_the_focus_advances_instead_of_looping(self):
        ws = windows_across_months()
        s = SweepStore()
        f = self._all_empty_then_a_fare()
        seen = []
        for _ in range(3):
            pend = focus_pending(ws, s, [1])
            if not pend:
                break
            seen.append(pend[0].key)
            sweep_batch(ws, s, batch=1, fetch=f,
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert len(set(seen)) == len(seen), f"the focus repeated a window: {seen}"

    def test_a_focus_can_reach_completion(self):
        ws = windows_across_months()
        s = SweepStore()
        f = self._all_empty_then_a_fare()
        for _ in range(20):
            if not focus_pending(ws, s, [1]):
                break
            sweep_batch(ws, s, batch=1, fetch=f, sleep=lambda _: None,
                        delay_s=0, focus_months=[1])
        assert focus_pending(ws, s, [1]) == [], "the focus never finished"

    def test_nothing_working_means_nothing_is_trusted(self):
        """Absence of evidence is not evidence: an all-empty stretch waits."""
        ws = windows_across_months()
        s = SweepStore()
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert not [k for k, v in s.checked.items() if v.get("healthy")]
        assert s.suspect, "an unprovable empty was not queued"
