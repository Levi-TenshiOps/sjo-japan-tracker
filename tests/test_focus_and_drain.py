"""A focus and a post-pass drain both want the launches. Who wins?

The drain was added 2026-08-26 and yields to a focus - `not pending` is
one of its three guards. That seam had no tests: the focus itself has 61,
and the drain has 21, and nothing exercised them together.

It matters because the two are easy to confuse. Both freeze the cold
cursor, both re-price windows out of rotation, and both drain
`store.suspect` as a side effect. The failure to avoid is the drain being
*cancelled* by a focus rather than deferred - the queue would then wait
for the next pass boundary, hours or days away.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pytest

from tracker import sweeper
from tracker.schedule import Window
from tracker.sweeper import SweepStore, focus_pending, sweep_batch


def windows(n=40):
    base = date(2027, 1, 1)
    return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
            for i in range(n)]


@pytest.fixture
def dom():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "chrome_dom_32n.html")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def mid_drain(w, queued=10):
    """A store part-way through a post-pass drain, connection proven."""
    s = SweepStore()
    s.cursor = 5
    s.windows_priced = 3
    s.recent = [0] * 25
    s.recent_blank = [0] * 25
    s.recent_worked = [1] * 25
    s.draining = True
    s.suspect = [x.key for x in w[:queued]]
    return s


def run(w, s, **kw):
    kw.setdefault("batch", 6)
    kw.setdefault("delay_s", 0)
    kw.setdefault("sleep", lambda *_: None)
    return sweep_batch(w, s, **kw)


class TestTheFocusOutranksTheDrain:
    def test_the_drain_yields_its_launches(self, dom):
        w = windows()
        s = mid_drain(w)
        run(w, s, fetch=lambda u: dom, focus_months=[1])
        assert s.drain_tries == {}, (
            "the drain spent launches while a focus was running")

    def test_the_drain_is_deferred_not_cancelled(self, dom):
        """The failure this test exists for. If a focus cleared `draining`,
        the queue would wait for the next pass boundary instead."""
        w = windows()
        s = mid_drain(w)
        run(w, s, fetch=lambda u: dom, focus_months=[1])
        assert s.draining is True

    def test_the_drain_resumes_once_the_focus_is_done(self, dom):
        w = windows()
        s = mid_drain(w)
        # Run with a focus until it completes, then without one.
        for _ in range(30):
            run(w, s, batch=10, fetch=lambda u: dom, focus_months=[1])
            if not focus_pending(w, s, [1]):
                break
        assert not focus_pending(w, s, [1]), "the focus never finished"
        before = len(s.drain_tries)
        run(w, s, batch=6, fetch=lambda u: dom)      # no focus now
        assert len(s.drain_tries) > before or not s.suspect, (
            "the drain did not pick up after the focus")

    def test_the_cursor_is_frozen_under_both(self, dom):
        w = windows()
        s = mid_drain(w)
        run(w, s, fetch=lambda u: dom, focus_months=[1])
        assert s.cursor == 5


class TestAFocusPickAlsoClearsItsRecheck:
    """Pricing a window clears its re-check, so a focus drains the queue
    as a side effect. Worth pinning: it is why the two can coexist."""

    def test_the_queue_shrinks_during_a_focus(self, dom):
        w = windows()
        s = mid_drain(w, queued=10)
        before = len(s.suspect)
        run(w, s, fetch=lambda u: dom, focus_months=[1])
        assert len(s.suspect) < before

    def test_nothing_is_written_off(self, dom):
        """Four-state invariant across a focus running inside a drain."""
        w = windows()
        s = mid_drain(w)
        for _ in range(12):
            run(w, s, batch=8, fetch=lambda u: dom, focus_months=[1])
        orphans = [
            x.key for i, x in enumerate(w)
            if i < s.cursor
            and x.key not in s.found
            and not (isinstance(s.checked.get(x.key), dict)
                     and s.checked[x.key].get("healthy"))
            and x.key not in s.suspect
        ]
        assert orphans == [], orphans


class TestTheFocusArgumentIsParsed:
    """`--focus 1,2,3` is typed by a person under time pressure on a sale
    day. Every plausible spelling has to do the right thing or nothing."""

    def _months(self, arg, monkeypatch):
        import sweep_forever
        monkeypatch.setattr(sweep_forever.sys, "argv",
                            ["sweep_forever.py", "--focus", arg])
        args = sweep_forever.build_args()
        raw = args.focus
        if raw.strip().lower() in ("none", "off", ""):
            return []
        return [int(x) for x in raw.split(",") if x.strip()]

    @pytest.mark.parametrize("arg,want", [
        ("1,2,3", [1, 2, 3]),
        ("3,1,2", [3, 1, 2]),          # order is honoured, not sorted
        ("1", [1]),
        (" 1 , 2 , 3 ", [1, 2, 3]),    # spaces around the commas
        ("1,2,3,", [1, 2, 3]),         # trailing comma
        ("none", []),
        ("NONE", []),
        ("off", []),
        ("", []),
    ])
    def test_spellings(self, arg, want, monkeypatch):
        assert self._months(arg, monkeypatch) == want

    def test_junk_raises_rather_than_silently_focusing_on_nothing(
            self, monkeypatch):
        with pytest.raises(ValueError):
            self._months("january", monkeypatch)


class TestTheOrderIsReallyHonoured:
    def test_january_before_february_before_march(self):
        w = []
        base = date(2027, 1, 1)
        for m, n in ((1, 3), (2, 3), (3, 3)):
            for i in range(n):
                d = date(2027, m, i + 1)
                w.append(Window(d, d + timedelta(days=27)))
        s = SweepStore()
        months = [x.depart.month for x in focus_pending(w, s, [1, 2, 3])]
        assert months == sorted(months), months
        assert months[0] == 1 and months[-1] == 3

    def test_a_reversed_request_is_reversed(self):
        w = []
        for m in (1, 2, 3):
            d = date(2027, m, 1)
            w.append(Window(d, d + timedelta(days=27)))
        s = SweepStore()
        months = [x.depart.month for x in focus_pending(w, s, [3, 2, 1])]
        assert months == [3, 2, 1]


class TestAFocusCanRefreshNotOnlyBackfill:
    """The gap found 2026-08-26 while checking `--focus 1,2,3` would work
    on a sale day.

    Every one of the 1,089 January-March windows had a trusted answer, so
    `focus_pending` returned 0 and the focus would have reported "complete"
    and gone straight back to the rotation - while 400 of those answers
    were over a day old and the oldest was 3.5 days.

    A focus that only backfills does nothing on the one day it is most
    wanted. `max_age_hours` makes an old answer stale again.
    """

    def _answered(self, w, hours_ago):
        """A store where every window was answered `hours_ago` ago."""
        from datetime import datetime, timezone, timedelta
        s = SweepStore()
        when = (datetime.now(timezone.utc)
                - timedelta(hours=hours_ago)).isoformat()
        for x in w:
            s.checked[x.key] = {"at": when, "empty": True, "blank": True,
                                "healthy": True}
        return s

    def test_without_an_age_a_fully_answered_focus_is_empty(self):
        w = windows(10)
        s = self._answered(w, hours_ago=30)
        assert focus_pending(w, s, [1]) == [], (
            "the old behaviour must be untouched")

    def test_with_an_age_stale_answers_come_back(self):
        w = windows(10)
        s = self._answered(w, hours_ago=30)
        assert len(focus_pending(w, s, [1], max_age_hours=6)) == len(w)

    def test_fresh_answers_stay_answered(self):
        w = windows(10)
        s = self._answered(w, hours_ago=1)
        assert focus_pending(w, s, [1], max_age_hours=6) == []

    def test_the_boundary_is_the_age_given(self):
        w = windows(4)
        assert focus_pending(w, self._answered(w, 5.0), [1],
                             max_age_hours=6) == []
        assert len(focus_pending(w, self._answered(w, 7.0), [1],
                                 max_age_hours=6)) == len(w)

    def test_a_fare_does_not_exempt_a_stale_window(self):
        """`found` means 'has a fare', not 'has a current price'."""
        w = windows(4)
        s = self._answered(w, hours_ago=30)
        for x in w:
            s.found[x.key] = {"price_usd": 1343, "depart": x.depart.isoformat(),
                              "ret": x.back.isoformat(),
                              "found_at": sweeper._now(), "stops": ["ZRH"],
                              "airlines": ["SWISS"], "total_minutes": 2780,
                              "deep_link": "x", "origin": "SJO",
                              "destination": "TYO"}
        assert len(focus_pending(w, s, [1], max_age_hours=6)) == len(w)

    def test_a_never_checked_window_is_stale_by_definition(self):
        w = windows(4)
        assert len(focus_pending(w, SweepStore(), [1],
                                 max_age_hours=6)) == len(w)

    def test_a_corrupt_timestamp_counts_as_stale(self):
        w = windows(4)
        s = SweepStore()
        for x in w:
            s.checked[x.key] = {"at": "not-a-date", "healthy": True}
        assert len(focus_pending(w, s, [1], max_age_hours=6)) == len(w)

    def test_a_refresh_focus_terminates(self, dom):
        """It must not re-queue what it just priced, or it never ends."""
        w = windows(12)
        s = self._answered(w, hours_ago=30)
        s.recent, s.recent_blank, s.recent_worked = [0]*25, [0]*25, [1]*25
        for _ in range(40):
            run(w, s, batch=10, fetch=lambda u: dom, focus_months=[1],
                focus_max_age_hours=6)
            if not focus_pending(w, s, [1], max_age_hours=6):
                break
        assert focus_pending(w, s, [1], max_age_hours=6) == [], \
            "the refresh focus never finished"
