"""After a pass, drain the queued re-checks before starting the next one.

Asked for 2026-08-26. The reasoning is sound and specific to the pass
boundary: once every window has been walked, nothing is being starved by
spending the next stretch on the queue - the re-checks *are* the questions
still open. Away from that boundary the one-in-eight share is right,
because draining flat out stalls the cold rotation and swaps one blind
spot for another.

The whole risk is termination. A window that answers blank while
`connection_proven` is False is put straight back on the queue by the very
check that just looked at it, so "drain until the queue is empty" is not a
terminating condition. `DRAIN_MAX_TRIES` is the bound, and
TestItAlwaysTerminates is the test that matters.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pytest

from tracker import sweeper
from tracker.schedule import Window
from tracker.sweeper import (DRAIN_MAX_TRIES, FOCUS_HOT_EVERY, SweepStore,
                             drain_next, sweep_batch)


def windows(n=6):
    base = date(2027, 1, 1)
    return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
            for i in range(n)]


class Chrome:
    def __init__(self, dom, blank_always=False):
        self.dom, self.blank_always, self.urls = dom, blank_always, []

    def __call__(self, url):
        self.urls.append(url)
        return "" if self.blank_always else self.dom


@pytest.fixture
def dom():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "chrome_dom_32n.html")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def run(w, s, **kw):
    kw.setdefault("batch", 2)
    kw.setdefault("delay_s", 0)
    kw.setdefault("sleep", lambda *_: None)
    return sweep_batch(w, s, **kw)


def at_end(n=6, suspect=()):
    """A store one window short of finishing a pass, connection proven.

    Not `cursor = n`: `resume_index` clamps the cursor to len(windows)-1 at
    the top of every batch, so a store cannot *start* past the end. The wrap
    only fires when the cursor walks off the end mid-batch, which is why
    every test here runs a batch of 2 from the last window.

    `windows_priced` is 3 deliberately - it is divisible by neither
    RECHECK_EVERY (8) nor FOCUS_HOT_EVERY (5), so the ordinary re-check
    share and the freshness launch do not fire on the first iteration and
    steal what the drain is being tested on.
    """
    s = SweepStore()
    s.cursor = max(n - 1, 0)
    s.windows_priced = 3
    s.suspect = list(suspect)
    s.recent = [0] * 25
    s.recent_blank = [0] * 25
    s.recent_worked = [1] * 25
    return s


class TestTheDrainStarts:
    def test_a_finished_pass_with_a_queue_starts_draining(self, dom):
        w = windows()
        s = at_end(len(w), suspect=[w[0].key, w[1].key])
        run(w, s, fetch=Chrome(dom))
        assert s.passes_completed == 1
        assert s.draining is True

    def test_a_finished_pass_with_no_queue_does_not(self, dom):
        w = windows()
        s = at_end(len(w))
        run(w, s, fetch=Chrome(dom))
        assert s.passes_completed == 1
        assert s.draining is False

    def test_mid_pass_never_starts_a_drain(self, dom):
        """Only the boundary earns it; the 1-in-8 share owns the rest."""
        w = windows()
        s = SweepStore()
        s.suspect = [w[0].key]
        s.recent, s.recent_blank, s.recent_worked = [0] * 25, [0] * 25, [1] * 25
        run(w, s, batch=3, fetch=Chrome(dom))
        assert s.draining is False


class TestItAlwaysTerminates:
    """The deadlock guard. Every re-check comes back blank and is re-queued
    by the same code path that just looked at it."""

    def _grind(self, blank=True):
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:3]])
        s.recent_worked = []          # nothing proven -> blanks are re-queued
        for _ in range(40):
            run(w, s, batch=5, fetch=Chrome("", blank_always=blank))
            if not s.draining:
                break
        return w, s

    def test_a_queue_that_refills_itself_still_ends(self):
        _, s = self._grind()
        assert s.draining is False, "the drain never let go"

    def test_no_window_exceeds_the_try_bound(self):
        _, s = self._grind()
        assert all(v <= DRAIN_MAX_TRIES for v in s.drain_tries.values()), \
            s.drain_tries

    def test_drain_next_stops_when_everything_is_exhausted(self):
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:2]])
        s.drain_tries = {x.key: DRAIN_MAX_TRIES for x in w[:2]}
        assert drain_next(s, w) is None

    def test_a_dead_key_is_never_returned(self):
        """min_lead_days rolls dates out; those must not hold the drain."""
        w = windows()
        s = at_end(len(w), suspect=["2020-01-01:2020-01-28"])
        assert drain_next(s, w) is None

    def test_a_live_key_is_returned_ahead_of_a_dead_one(self):
        w = windows()
        s = at_end(len(w), suspect=["2020-01-01:2020-01-28", w[2].key])
        assert drain_next(s, w) == w[2].key


class TestItDoesNotStarveAnythingElse:
    def test_the_hot_list_still_gets_its_launch(self, dom):
        """The email is built on the cheapest known fare; a backfill must
        not let it age out."""
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:4]])
        s.found[w[5].key] = {
            "price_usd": 1347, "depart": w[5].depart.isoformat(),
            "ret": w[5].back.isoformat(), "found_at": sweeper._now(),
            "stops": ["ZRH"], "airlines": ["SWISS"], "total_minutes": 2780,
            "deep_link": "x", "origin": "SJO", "destination": "TYO",
        }
        s.windows_priced = FOCUS_HOT_EVERY - 1   # next launch is a freshness one
        run(w, s, fetch=Chrome(dom), hot_threshold=1400)
        # Assert on `drain_tries`, not on the length of `suspect`: the queue
        # legitimately shrinks for an unrelated reason - pricing any window
        # clears its re-check, and the cold pick after the wrap lands on
        # w[0], which is queued. An empty `drain_tries` is the precise
        # statement that the drain yielded its turn.
        assert s.drain_tries == {}, "the drain took the freshness launch"
        assert s.draining is True, "yielding a turn must not end the drain"

    def test_an_unhealthy_connection_does_not_drain(self):
        """Progress must be structural, never conditional on a signal."""
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:3]])
        s.recent = [1] * 25                 # looks throttled
        s.recent_blank = [1] * 25
        priced = run(w, s, batch=2, fetch=Chrome("", blank_always=True))
        assert priced > 0, "it stalled entirely rather than falling through"

    def test_the_cold_cursor_stays_put_during_the_drain(self, dom):
        """Pass 2 must start at 0, not wherever the drain left the cursor."""
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:3]])
        run(w, s, fetch=Chrome(dom))
        assert s.cursor == 0

    def test_the_drain_prices_a_queued_window_not_a_cold_one(self, dom):
        w = windows()
        s = at_end(len(w), suspect=[w[3].key])
        run(w, s, fetch=Chrome(dom))
        assert w[3].key not in s.suspect, "the queued window was not taken"


class TestTheBookkeeping:
    def test_tries_reset_on_the_next_pass(self, dom):
        """A window the drain gave up on deserves a fresh look next pass -
        the connection may be completely different by then."""
        w = windows()
        s = at_end(len(w), suspect=[w[0].key])
        s.drain_tries = {w[0].key: DRAIN_MAX_TRIES, "stale": 9}
        run(w, s, fetch=Chrome(dom))
        assert "stale" not in s.drain_tries

    def test_giving_up_hands_the_queue_back(self):
        """A drain already in progress, mid-pass, with every window spent.

        Deliberately not parked at the pass end: the wrap resets
        `drain_tries`, which would wipe the exhausted state this is about.
        """
        w = windows()
        s = SweepStore()
        s.cursor = 1
        s.windows_priced = 3
        s.recent, s.recent_blank, s.recent_worked = [0] * 25, [0] * 25, [1] * 25
        s.suspect = [x.key for x in w[:3]]
        s.drain_tries = {x.key: DRAIN_MAX_TRIES for x in w[:3]}
        s.draining = True
        run(w, s, batch=1, fetch=Chrome("", blank_always=True))
        assert s.draining is False, "it kept draining with nothing left to try"
        assert s.suspect, "the queue was discarded rather than handed back"

    def test_the_store_survives_a_round_trip(self, tmp_path, dom):
        w = windows()
        s = at_end(len(w), suspect=[w[0].key])
        run(w, s, fetch=Chrome(dom))
        p = tmp_path / "s.json"
        s.save(p)
        again = SweepStore.load(p)
        assert again.draining == s.draining
        assert again.drain_tries == s.drain_tries


class TestCoverageIsStillGuaranteed:
    """The four-state invariant must survive a drain: every walked window
    has a fare, a trusted empty, or a place in the queue."""

    def test_no_window_is_silently_written_off(self, dom):
        w = windows()
        s = at_end(len(w), suspect=[x.key for x in w[:3]])
        for _ in range(10):
            run(w, s, batch=5, fetch=Chrome(dom))
        orphans = [
            x.key for i, x in enumerate(w)
            if i < s.cursor
            and x.key not in s.found
            and not (isinstance(s.checked.get(x.key), dict)
                     and s.checked[x.key].get("healthy"))
            and x.key not in s.suspect
        ]
        assert orphans == [], orphans


class TestItSaysWhatItIsDoing:
    """A frozen cold cursor must never look like a dead sweep. The focus
    shipped that bug once and this project spent hours misreading it."""

    def test_progress_announces_the_drain(self):
        s = SweepStore()
        s.draining = True
        s.suspect = ["a", "b"]
        line = s.progress(100)
        assert "DRAINING" in line
        assert "not stuck" in line

    def test_progress_is_unchanged_when_not_draining(self):
        s = SweepStore()
        s.suspect = ["a", "b"]
        assert "DRAINING" not in s.progress(100)
