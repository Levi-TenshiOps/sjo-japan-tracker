"""The watch table must not contradict the header above it.

Spotted by the trip owner, 2026-08-26, reading a live --watch during the
first post-pass drain:

    window 4 of 2,745   405 fares remembered   291 awaiting a re-check
    month        walked   with a fare   re-check
    2027-01   4/558 (1%)            0          0
    2027-02   0/476 (0%)            0          0

405 and 291 in the header, zeros in every row. The table counted a window
only `if i < store.cursor`, and a pass had just wrapped, so the cursor was
4 - it inspected four windows and reported on those.

"with a fare" and "re-check" are facts about the store, not about where
the cursor is. Only "walked" is cursor-relative, and it is honest: it is
progress through *this* pass.

Same family as every other counting bug in this project: a column that
counts something other than what its heading claims.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from datetime import date, datetime, timedelta, timezone

from tracker import sweeper
from tracker.schedule import Window
from tracker.sweeper import SweepStore, watch_lines

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def windows(n=12):
    base = date(2027, 1, 1)
    return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
            for i in range(n)]


def store_after_a_wrap(w, fares=4, queued=3):
    """A pass has just finished: the cursor is near zero, but the store is
    full of fares and queued re-checks."""
    s = SweepStore()
    s.cursor = 1
    s.passes_completed = 1
    for x in w[:fares]:
        s.found[x.key] = {
            "price_usd": 1343, "depart": x.depart.isoformat(),
            "ret": x.back.isoformat(), "found_at": sweeper._now(),
            "stops": ["ZRH"], "airlines": ["SWISS"], "total_minutes": 2780,
            "deep_link": "x", "origin": "SJO", "destination": "TYO",
        }
    s.suspect = [x.key for x in w[fares:fares + queued]]
    return s


def table_rows(lines):
    out = []
    for ln in lines:
        # The walked cell contains a space - "1/12 (8%)" - so it cannot be
        # matched as a single \S+ token.
        m = re.match(r"\s+(\d{4}-\d{2})\s+(\d+)/(\d+)\s+\(\d+%\)"
                     r"\s+(\d+)\s+(\d+)\s*$", ln)
        if m:
            out.append((m.group(1), int(m.group(2)), int(m.group(4)),
                        int(m.group(5))))
    return out


class TestTheTableAgreesWithTheHeader:
    def test_fares_are_counted_after_a_wrap(self):
        w = windows()
        s = store_after_a_wrap(w, fares=4, queued=3)
        rows = table_rows(watch_lines(w, s, threshold=1400, now=NOW))
        assert sum(r[2] for r in rows) == 4, rows

    def test_rechecks_are_counted_after_a_wrap(self):
        w = windows()
        s = store_after_a_wrap(w, fares=4, queued=3)
        rows = table_rows(watch_lines(w, s, threshold=1400, now=NOW))
        assert sum(r[3] for r in rows) == 3, rows

    def test_the_totals_match_the_store_exactly(self):
        w = windows()
        s = store_after_a_wrap(w, fares=5, queued=4)
        rows = table_rows(watch_lines(w, s, threshold=1400, now=NOW))
        assert sum(r[2] for r in rows) == len(s.found)
        assert sum(r[3] for r in rows) == len(s.suspect)

    def test_walked_stays_cursor_relative(self):
        """It is progress through this pass, and near zero after a wrap is
        the truth, not a bug."""
        w = windows()
        s = store_after_a_wrap(w)
        rows = table_rows(watch_lines(w, s, threshold=1400, now=NOW))
        walked = sum(r[1] for r in rows)
        assert walked == s.cursor

    def test_a_window_cannot_be_both_fare_and_recheck(self):
        w = windows()
        s = store_after_a_wrap(w, fares=4, queued=3)
        rows = table_rows(watch_lines(w, s, threshold=1400, now=NOW))
        for month, _, fare, again in rows:
            total = sum(1 for x in w if x.depart.strftime("%Y-%m") == month)
            assert fare + again <= total


class TestTheDrainIsVisible:
    def test_it_says_the_sweep_is_paused_not_stalled(self):
        w = windows()
        s = store_after_a_wrap(w)
        s.draining = True
        text = " ".join(watch_lines(w, s, threshold=1400, now=NOW))
        assert "DRAINING" in text
        assert "paused" in text

    def test_it_rates_the_queue_rather_than_the_frozen_cursor(self):
        w = windows()
        s = store_after_a_wrap(w, fares=4, queued=3)
        s.draining = True
        started = (NOW - timedelta(hours=1), s.cursor, 0, 9)   # queue was 9
        text = " ".join(watch_lines(w, s, threshold=1400,
                                    started=started, now=NOW))
        assert "cleared/hour" in text, text
        assert "h left" in text

    def test_no_drain_line_when_not_draining(self):
        w = windows()
        s = store_after_a_wrap(w)
        text = " ".join(watch_lines(w, s, threshold=1400, now=NOW))
        assert "DRAINING" not in text
