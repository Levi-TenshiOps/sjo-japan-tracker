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
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io as _io

import pytest

from tracker.schedule import Window
from tracker.sweeper import (
    FOCUS_HOT_EVERY, SweepStore, focus_pending, sweep_batch,
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


class TestAFocusCannotDeadlock:
    """Progress must be structural, not a lucky side effect.

    A window that answers blank while nothing else has recently returned
    fares is not trusted, so it stays at the head of `pending` and gets
    picked again - and since it is then the *only* thing being priced, the
    evidence needed to trust it can never arrive. Self-reinforcing.

    Reproduced 2026-08-24 with an empty hot list: eight launches, one
    window. In production the one-in-five freshness launch broke it by
    pricing a hot window that returned fares - luck, not design, and only
    while the hot list is non-empty.
    """

    def _three_january(self):
        return [Window(date(2027, 1, d), date(2027, 1, d) + timedelta(days=21))
                for d in (4, 5, 6)]

    def test_it_rotates_instead_of_repeating(self):
        ws = self._three_january()
        s = SweepStore()
        seen = []
        for _ in range(8):
            sweep_batch(ws, s, batch=1, fetch=lambda u: seen.append(u) or "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert len(set(seen)) == 3, (
            f"the focus deadlocked: {len(set(seen))} distinct of {len(seen)}")

    def test_every_window_gets_touched(self):
        ws = self._three_january()
        s = SweepStore()
        for _ in range(6):
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert set(s.checked) == {w.key for w in ws}


class TestFocusNextPicksFairly:
    def _win(self, day):
        return Window(date(2027, 1, day), date(2027, 1, day) + timedelta(days=21))

    def test_a_never_checked_window_wins(self):
        from tracker.sweeper import focus_next
        a, b = self._win(4), self._win(5)
        s = SweepStore()
        s.checked[a.key] = {"at": datetime.now(timezone.utc).isoformat()}
        assert focus_next([a, b], s) is b

    def test_order_is_kept_when_nothing_is_recent(self):
        from tracker.sweeper import focus_next
        a, b = self._win(4), self._win(5)
        s = SweepStore()
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        s.checked[a.key] = {"at": old}
        s.checked[b.key] = {"at": old}
        assert focus_next([a, b], s) is a, "January order was not preserved"

    def test_all_inside_the_cooldown_takes_the_oldest(self):
        """Near the end a focus laps a small set; it must not stall."""
        from tracker.sweeper import focus_next
        a, b = self._win(4), self._win(5)
        s = SweepStore()
        now = datetime.now(timezone.utc)
        s.checked[a.key] = {"at": now.isoformat()}
        s.checked[b.key] = {"at": (now - timedelta(minutes=5)).isoformat()}
        assert focus_next([a, b], s) is b

    def test_an_unparsable_timestamp_does_not_stall_it(self):
        from tracker.sweeper import focus_next
        a = self._win(4)
        s = SweepStore()
        s.checked[a.key] = {"at": "not a date"}
        assert focus_next([a], s) is a

    def test_nothing_pending_is_nothing(self):
        from tracker.sweeper import focus_next
        assert focus_next([], SweepStore()) is None


class TestTheWatchShowsFocusProgress:
    """A frozen bar for a day reads as a stalled sweep."""

    def test_it_leads_with_the_focus_when_one_is_running(self):
        from tracker.sweeper import watch_lines
        ws = windows_across_months()
        s = SweepStore()
        s.cursor = 6
        text = "\n".join(watch_lines(ws, s, threshold=1400, focus_months=[1]))
        assert "FOCUS January" in text
        assert "still open" in text
        assert "full sweep (paused)" in text, "the frozen cursor is unlabelled"

    def test_a_finished_focus_says_so(self):
        from tracker.sweeper import watch_lines
        ws = windows_across_months()
        s = SweepStore()
        for w in ws:
            if w.depart.month == 1:
                s.checked[w.key] = {"healthy": True}
        text = "\n".join(watch_lines(ws, s, threshold=1400, focus_months=[1]))
        assert "complete" in text

    def test_without_a_focus_it_is_unchanged(self):
        from tracker.sweeper import watch_lines
        ws = windows_across_months()
        text = "\n".join(watch_lines(ws, SweepStore(), threshold=1400))
        assert "FOCUS" not in text
        assert "(paused)" not in text


class TestAFocusCanActuallyEnd:
    """"100% trusted" is not reachable; "100% fairly attempted" is.

    A date that is genuinely blank can only be believed once something
    *else* has returned fares. If nothing does - a barren stretch with an
    empty hot list - nothing ever can, and the focus rotates through the
    same tail for ever. Measured 2026-08-24: 40 blank launches over 30
    windows left all 30 still pending.

    So a focus gives each window FOCUS_MAX_TRIES honest attempts and then
    moves on. The window is not written off: it stays in the ordinary
    re-check queue.
    """

    def _windows(self, n=5):
        return [Window(date(2027, 1, 1) + timedelta(days=i),
                       date(2027, 1, 1) + timedelta(days=i + 21))
                for i in range(n)]

    def test_an_all_blank_focus_terminates(self):
        from tracker.sweeper import FOCUS_MAX_TRIES
        ws = self._windows()
        s = SweepStore()
        for i in range(200):
            if not focus_pending(ws, s, [1]):
                break
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        else:
            raise AssertionError("the focus never ended")
        assert i <= len(ws) * FOCUS_MAX_TRIES + 2, f"took {i} launches"

    def test_nothing_is_written_off_when_it_gives_up(self):
        ws = self._windows()
        s = SweepStore()
        for _ in range(60):
            if not focus_pending(ws, s, [1]):
                break
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert set(s.suspect) == {w.key for w in ws}, (
            "a window the focus gave up on left the re-check queue too")

    def test_a_window_that_answers_needs_only_one_try(self):
        ws = self._windows()
        s = SweepStore()
        sweep_batch(ws, s, batch=1,
                    fetch=FakeChrome(_io.open(FIXTURE, encoding="utf-8").read()),
                    sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert max(int(v) for v in s.focus_tries.values()) == 1


class TestASecondFocusStillDoesWork:
    """The Labor Day case: run the same focus again on a sale day.

    The attempt counters persist in the store, so without clearing them a
    second focus finds every window already at the cap and spends zero
    launches - silently. Measured 2026-08-24 before the fix: 0 launches.
    """

    def test_the_counters_are_cleared_at_startup_not_on_completion(self):
        import pathlib
        import re
        src = re.sub(r"\s+", " ", (pathlib.Path(__file__).resolve().parent.parent
                                   / "sweep_forever.py").read_text(encoding="utf-8"))
        assert "store.focus_tries = {}" in src, "the counters are never cleared"
        # Clearing on completion would let the focus restart and loop.
        i = src.find("store.focus_tries = {}")
        assert "clearing" in src[max(0, i - 400):i]

    def test_clearing_the_counters_makes_the_focus_live_again(self):
        ws = [Window(date(2027, 1, 1) + timedelta(days=i),
                     date(2027, 1, 1) + timedelta(days=i + 21))
              for i in range(4)]
        s = SweepStore()
        for _ in range(40):
            if not focus_pending(ws, s, [1]):
                break
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert focus_pending(ws, s, [1]) == []
        s.focus_tries = {}                      # what a restart does
        assert len(focus_pending(ws, s, [1])) == len(ws)


class TestAFocusMonthThatSearchesNothing:
    """Same trap as a named month the horizon cannot reach."""

    def test_a_month_with_no_windows_yields_no_pending(self):
        ws = windows_across_months()
        assert focus_pending(ws, SweepStore(), [6]) == []

    def test_the_run_warns_rather_than_looking_finished(self):
        import pathlib
        import re
        src = re.sub(r"\s+", " ",
                     (pathlib.Path(__file__).resolve().parent.parent
                      / "sweep_forever.py").read_text(encoding="utf-8"))
        # Collapsed whitespace: these are wrapped log strings, and an exact
        # match would break on a rewrap rather than on the warning going.
        # Fragments that do not span a string-literal boundary, so a
        # rewrap cannot break this for a reason unrelated to the warning.
        assert "are not in the searched window" in src
        assert "none of the requested months are searched" in src


class TestTheCursorIsGenuinelyFrozen:
    """"Paused" has to be true, not nearly true.

    The freshness launch used to ask `next_window` for a hot window with
    hot_share=1.0. Its interleave works out to "every second launch", so on
    the others it fell through to the cold cursor and advanced it -
    measured 2026-08-24, the cursor crept during a focus that had just
    logged "the cold rotation is paused". Nothing was lost, but the focus
    was diluted and the log said something untrue.
    """

    def _with_a_hot_window(self, n=12):
        ws = [Window(date(2027, 1, 1) + timedelta(days=i),
                     date(2027, 1, 1) + timedelta(days=i + 21))
              for i in range(n)]
        s = SweepStore()
        s.found[ws[0].key] = {
            "depart": ws[0].depart.isoformat(), "ret": ws[0].back.isoformat(),
            "price_usd": 1300, "origin": "SJO", "destination": "TYO",
            "stops": [], "airlines": "", "total_minutes": 100,
            "deep_link": "", "seen_at": "2026-08-24T23:00:00+00:00"}
        return ws, s

    def test_the_cursor_never_moves_with_a_hot_list(self):
        ws, s = self._with_a_hot_window()
        for _ in range(20):
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0,
                        focus_months=[1], hot_threshold=1400)
        assert s.cursor == 0, f"the cold cursor crept to {s.cursor}"

    def test_the_cursor_never_moves_without_one(self):
        ws = [Window(date(2027, 1, 1) + timedelta(days=i),
                     date(2027, 1, 1) + timedelta(days=i + 21))
              for i in range(12)]
        s = SweepStore()
        for _ in range(20):
            sweep_batch(ws, s, batch=1, fetch=lambda u: "",
                        sleep=lambda _: None, delay_s=0, focus_months=[1])
        assert s.cursor == 0, f"the cold cursor crept to {s.cursor}"

    def test_every_launch_still_prices_something(self):
        """Freezing the cursor must not mean doing nothing."""
        ws, s = self._with_a_hot_window()
        sweep_batch(ws, s, batch=20, fetch=lambda u: "",
                    sleep=lambda _: None, delay_s=0,
                    focus_months=[1], hot_threshold=1400)
        assert s.windows_priced == 20

    def test_the_hot_window_really_is_refreshed(self):
        """The whole reason for the freshness launch."""
        ws, s = self._with_a_hot_window()
        before = s.found[ws[0].key]["seen_at"]
        dom = _io.open(FIXTURE, encoding="utf-8").read()
        for _ in range(FOCUS_HOT_EVERY + 1):
            sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                        sleep=lambda _: None, delay_s=0,
                        focus_months=[1], hot_threshold=1400)
        assert s.found[ws[0].key]["seen_at"] != before, (
            "the cheapest known fare was never refreshed")
