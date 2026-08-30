"""`--watch` must show what the sweep is doing, not what its own defaults
would imply.

Reported live on 2026-08-30, mid sale-day rehearsal. The sweep was started
with `--focus 1,2,3 --focus-max-age 0 --focus-max-tries 1`; `--watch` was
started with none of those, because nobody expects a read-only view to
need the same arguments. It therefore computed the focus in *backfill*
mode - where every window already has an answer - and printed

    FOCUS January, February, March: complete

while the sweep was 1,085 windows into a refresh. The cold bar beside it
is frozen by design during a focus, so the whole screen said "nothing is
happening" about a sweep pricing a window every 19 seconds.

Same shape as the rate: a separate process with its own argparse defaults
guessing at what the running one is doing. `delay_s` was persisted for
exactly this reason; the focus settings now are too.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pytest

from tracker import sweeper
from tracker.schedule import Window
from tracker.sweeper import SweepStore, focus_pending


def windows(n=12):
    base = date(2027, 1, 1)
    return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
            for i in range(n)]


def answered_store(w, hours_ago=2):
    from datetime import datetime, timezone
    s = SweepStore()
    when = (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).isoformat()
    for x in w:
        s.checked[x.key] = {"at": when, "empty": True, "blank": True,
                            "healthy": True}
    return s


class TestTheStoreCarriesTheFocus:
    def test_the_fields_exist_and_default_to_nothing(self):
        s = SweepStore()
        assert s.focus_months == []
        assert s.focus_max_age_hours is None
        assert s.focus_max_tries == 0

    def test_they_survive_a_save_and_load(self, tmp_path):
        s = SweepStore()
        s.focus_months = [1, 2, 3]
        s.focus_max_age_hours = 0.0
        s.focus_max_tries = 1
        p = tmp_path / "d.json"
        s.save(p)
        again = SweepStore.load(p)
        assert again.focus_months == [1, 2, 3]
        assert again.focus_max_age_hours == 0.0
        assert again.focus_max_tries == 1

    def test_the_sweep_records_them_every_batch(self):
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8"))
        assert "store.focus_months = list(focus_months)" in src
        assert "store.focus_max_age_hours = args.focus_max_age" in src


class TestWatchWithoutTheFlagsAgreesWithTheSweep:
    """The bug: these two must not disagree."""

    def test_backfill_and_refresh_really_do_differ(self):
        """If they agreed there would be nothing to get wrong."""
        w = windows()
        s = answered_store(w)
        assert focus_pending(w, s, [1]) == []
        assert len(focus_pending(w, s, [1], max_age_hours=0,
                                 max_tries=1)) == len(w)

    def test_the_recorded_settings_reproduce_the_sweep_view(self, tmp_path):
        w = windows()
        s = answered_store(w)
        s.focus_months = [1]
        s.focus_max_age_hours = 0.0
        s.focus_max_tries = 1
        p = tmp_path / "d.json"
        s.save(p)
        snap = SweepStore.load(p)
        # exactly what --watch now does when given no flags
        seen = focus_pending(w, snap, snap.focus_months,
                             max_age_hours=snap.focus_max_age_hours,
                             max_tries=snap.focus_max_tries)
        assert len(seen) == len(w), "watch would still report 'complete'"

    def test_watch_prefers_the_store_over_its_own_defaults(self):
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8"))
        assert "snap.focus_max_age_hours" in src
        assert "list(snap.focus_months)" in src

    def test_an_explicit_flag_still_wins(self):
        """Someone typing --focus-max-age at the watch means it."""
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8"))
        assert ("w_age = (args.focus_max_age if args.focus_max_age is not None "
                "else snap.focus_max_age_hours)") in src


class TestTheWatchLineItself:
    def test_a_running_refresh_is_not_reported_complete(self):
        w = windows()
        s = answered_store(w)
        lines = sweeper.watch_lines(w, s, threshold=1400, delay_s=5.0,
                                    focus_months=[1], focus_max_age_hours=0,
                                    focus_max_tries=1)
        text = " ".join(lines)
        assert "complete" not in text.lower(), text[:200]
        assert "still open" in text

    def test_a_genuinely_finished_focus_still_says_complete(self):
        w = windows()
        s = answered_store(w)
        lines = sweeper.watch_lines(w, s, threshold=1400, delay_s=5.0,
                                    focus_months=[1])      # backfill
        assert "complete" in " ".join(lines).lower()
