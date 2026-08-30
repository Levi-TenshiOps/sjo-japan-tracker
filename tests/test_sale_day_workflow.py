"""The sale-day workflow, end to end.

    sweep_forever.py --stop
    sweep_forever.py --focus 1,2,3 --focus-max-age 6 --delay 5
    python -m tracker.cli --email-now

Every step is run once a year under time pressure, which is the worst
possible combination: no practice, and no time to debug. So the parts that
would fail silently are what these tests cover.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import sweeper
from tracker.sweeper import LAUNCH_SECONDS, FOCUS_HOT_EVERY


class TestTheRateSurvivesTheRestart:
    """The bug this file was written for. A restart drops to the 40s
    default, and 880 stale windows then take 16.5 h instead of 5.8 h -
    the difference between a focus finishing on the day and not."""

    def _hours(self, delay, windows=880):
        cycle = delay + LAUNCH_SECONDS
        per_hour = 3600 / cycle * (1 - 1 / FOCUS_HOT_EVERY)
        return windows / per_hour

    def test_the_default_really_is_much_slower(self):
        assert self._hours(40) > 2.5 * self._hours(5)

    def test_the_warning_exists_and_names_the_flag(self):
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8"))
        assert "the previous run was at --delay" in src
        assert "Pass --delay %.0f to continue at the old rate." in src

    def test_the_rate_is_not_persisted_into_the_default(self, tmp_path):
        """It must stay a warning, never a silent carry-over: the Startup
        launcher passes no --delay so the default is what survives a
        reboot, and a fast rate surviving unattended is what caused the
        2026-08-23 throttle."""
        import sweep_forever
        sys.argv = ["sweep_forever.py"]
        first = sweep_forever.build_args().delay

        # A different rate in the store must not change what the default is.
        # The value itself is a decision in the code (5s from 2026-08-27);
        # what this pins is that it is a *constant*, not a rate carried over
        # from whatever the last run happened to be doing.
        s = sweeper.SweepStore()
        s.delay_s = 99.0
        s.save(tmp_path / "d.json")
        assert sweep_forever.build_args().delay == first, (
            "the default followed the store instead of the code")


class TestTheFocusFlagsParseTogether:
    def _args(self, argv, monkeypatch):
        import sweep_forever
        monkeypatch.setattr(sweep_forever.sys, "argv",
                            ["sweep_forever.py"] + argv)
        return sweep_forever.build_args()

    def test_the_documented_command(self, monkeypatch):
        a = self._args(["--focus", "1,2,3", "--focus-max-age", "6",
                        "--delay", "5"], monkeypatch)
        assert a.focus == "1,2,3"
        assert a.focus_max_age == 6.0
        assert a.delay == 5.0

    def test_max_age_alone_falls_back_to_the_config_months(self, monkeypatch):
        """config.yaml already carries sweep_focus_months: [1, 2, 3], so
        --focus can be omitted. `focus is None` is what selects that."""
        a = self._args(["--focus-max-age", "6"], monkeypatch)
        assert a.focus is None
        assert a.focus_max_age == 6.0

    def test_focus_none_still_works_beside_an_age(self, monkeypatch):
        a = self._args(["--focus", "none", "--focus-max-age", "6"],
                       monkeypatch)
        assert a.focus == "none"

    def test_no_age_means_backfill_only(self, monkeypatch):
        a = self._args(["--focus", "1,2,3"], monkeypatch)
        assert a.focus_max_age is None


class TestEmailNowIsSafeBesideARunningSweep:
    """It is run while the sweep is mid-pass, so it must tolerate the store
    being rewritten underneath it and must never take the Google lock."""

    def _body(self, name):
        """A function's code, with its docstring stripped.

        The docstring has to go: `email_now`'s own prose says it "never
        contends for gate.google()", which a naive substring search reads
        as a call to it. Caught by this test failing on the comment
        explaining why the thing it checks for is absent.
        """
        from pathlib import Path
        src = Path("tracker/cli.py").read_text(encoding="utf-8")
        start = src.index("def " + name + "(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        while '"""' in body:
            a = body.index('"""')
            b = body.index('"""', a + 3)
            body = body[:a] + body[b + 3:]
        return body

    def test_it_never_takes_the_google_lock(self):
        assert "gate.google" not in self._body("email_now"), (
            "the report would contend with the sweep for the lock")

    def test_it_does_not_write_the_alert_state(self):
        body = self._body("email_now")
        assert "state.save" not in body and "record_sent" not in body

    def test_a_half_written_store_does_not_crash_it(self, tmp_path):
        """SweepStore.save is atomic, but a truncated file from disk
        trouble must still degrade to 'nothing to report'."""
        p = tmp_path / "discoveries.json"
        p.write_text('{"version": 1, "found": {"a": ', encoding="utf-8")
        s = sweeper.SweepStore.load(p)
        assert s.best(limit=12, max_age_hours=10) == []


class TestTheFocusEndsAndTheSweepCarriesOn:
    def test_a_finished_focus_hands_back(self, tmp_path):
        """After the sale day the sweep must return to normal by itself,
        without another restart.

        Behavioural, not a source search. The previous version matched the
        literal "Resuming the full rotation." and broke the moment that
        message gained a sentence, because the wrapped source has a quote
        and a space in the middle of it. Fourth source-matching test in this
        project to misfire on a message it did not care about.
        """
        from datetime import date, timedelta
        from tracker.schedule import Window
        from tracker.sweeper import SweepStore, sweep_batch
        w = [Window(date(2027, 1, 1) + timedelta(days=i),
                    date(2027, 1, 1) + timedelta(days=i + 27))
             for i in range(6)]
        s = SweepStore()
        s.recent, s.recent_blank, s.recent_worked = [0] * 25, [0] * 25, [1] * 25
        # Everything already answered, so the focus is finished on arrival.
        for x in w:
            s.checked[x.key] = {"at": sweeper._now(), "empty": True,
                                "blank": True, "healthy": True}
        dom = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "chrome_dom_32n.html"), encoding="utf-8").read()
        before = s.cursor
        sweep_batch(w, s, batch=3, fetch=lambda u: dom, delay_s=0,
                    sleep=lambda *_: None, focus_months=[1])
        assert s.focus_done_logged is True, "it did not notice it had finished"
        assert s.cursor > before, (
            "the cold rotation did not resume after the focus finished")

    def test_the_cold_cursor_resumes_where_it_stopped(self):
        """Frozen, not skipped - or the deferred months would sit behind
        the cursor with no answer, which is the one outcome the store
        exists to prevent."""
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("tracker/sweeper.py").read_text(encoding="utf-8"))
        assert "replay = True # freezes the cold cursor" in src


class TestEveryFocusFlagReachesSweepBatch:
    """The wiring test, because this is where the flags go to die.

    `sweep_batch` is the only call that decides what gets priced. A flag
    threaded into `watch_lines` and the startup banner but not into
    `sweep_batch` looks completely wired - the right backlog appears in
    --watch, the startup line reports the right count - and changes
    nothing about what the sweep actually does.

    I made exactly that mistake with --focus-max-tries on 2026-08-26,
    minutes after writing the commit message warning about it.
    """

    def _sweep_batch_call(self):
        from pathlib import Path
        src = Path("sweep_forever.py").read_text(encoding="utf-8")
        i = src.index("priced = sweep_batch(")
        return src[i:src.index(")", src.index("on_alarm=raise_alarm"))]

    def test_focus_months_is_passed(self):
        assert "focus_months=focus_months" in self._sweep_batch_call()

    def test_focus_max_age_is_passed(self):
        assert "focus_max_age_hours=args.focus_max_age" in self._sweep_batch_call()

    def test_focus_max_tries_is_passed(self):
        assert "focus_max_tries=args.focus_max_tries" in self._sweep_batch_call()

    def test_the_live_delay_is_passed(self):
        assert "delay_s=current_delay" in self._sweep_batch_call()

    def test_every_focus_flag_has_a_home(self):
        """Any --focus* flag must reach sweep_batch, or it is decoration."""
        import sweep_forever, sys
        sys.argv = ["sweep_forever.py"]
        args = sweep_forever.build_args()
        call = self._sweep_batch_call()
        for name in vars(args):
            if not name.startswith("focus"):
                continue
            assert f"args.{name}" in call, (
                f"--{name.replace('_', '-')} never reaches sweep_batch")


class TestTheTerminalShowsFocusProgress:
    """The per-batch line is what a person leaves open, and during a focus
    the cold cursor is frozen - so it repeated an unchanging "window
    1360/2745" and read as a stalled sweep. `--watch` has shown the focus
    properly since 2026-08-24; the ordinary log did not, and that is the
    one people actually watch. Asked about 2026-08-30, mid-focus."""

    def _src(self):
        import re
        from pathlib import Path
        return re.sub(r"\s+", " ",
                      Path("sweep_forever.py").read_text(encoding="utf-8"))

    def test_the_batch_line_reports_the_focus(self):
        src = self._src()
        assert "FOCUS {done_n}/{focus_total} re-priced" in src

    def test_it_counts_against_the_size_at_startup(self):
        """A percentage needs a denominator that does not move."""
        src = self._src()
        assert "focus_total = len(pend)" in src
        assert "focus_total = 0" in src, "no default for runs without a focus"

    def test_it_stops_reporting_once_the_focus_is_done(self):
        src = self._src()
        assert "if focus_months and not store.focus_done_logged:" in src


class TestTheLogSaysWhenItWillFinish:
    """Asked for 2026-08-30: the terminal should say how long is left, not
    only how far it has got. --watch had an ETA; the plain log did not, and
    the log is what people leave open."""

    def _src(self):
        import re
        from pathlib import Path
        return re.sub(r"\s+", " ",
                      Path("sweep_forever.py").read_text(encoding="utf-8"))

    def test_it_reports_an_eta(self):
        src = self._src()
        assert "done about" in src
        assert "left_txt" in src

    def test_the_rate_is_measured_not_assumed(self):
        """The configured delay is not the real pace: a blank page comes
        back in ~4s and one with fares in ~14s, and the sweep also waits
        behind the scheduled runs."""
        src = self._src()
        assert "focus_started" in src
        assert "per_min = done_n / mins" in src

    def test_it_refuses_to_guess_from_too_few_windows(self):
        src = self._src()
        assert "done_n >= 5" in src
        assert "measuring the rate..." in src

    def test_minutes_below_an_hour_and_a_half(self):
        """"~0.0 h left" is not an answer."""
        src = self._src()
        assert "eta_min < 90" in src
        assert "min left" in src


class TestStatusShowsTheFocusEta:
    """Asked mid-focus 2026-08-30: how many hours left?

    The log line only gained an ETA in a later commit, and a running sweep
    cannot pick that up - restarting to get it would clear `focus_tries`
    and re-do everything already priced. `--status` is a fresh process
    every time, so it answers without touching the run.

    The rate is measured from the check ledger rather than a start time,
    so it works on a focus already in flight and follows a pace that
    changes: blank pages come back in ~4s, pages with fares in ~14s.
    """

    def _windows(self, n=20, month=1):
        from datetime import date, timedelta
        from tracker.schedule import Window
        base = date(2027, month, 1)
        return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
                for i in range(n)]

    def _store(self, w, done, minutes_ago=10):
        from datetime import datetime, timezone, timedelta
        from tracker.sweeper import SweepStore
        s = SweepStore()
        recent = (datetime.now(timezone.utc)
                  - timedelta(minutes=minutes_ago)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        for i, x in enumerate(w):
            s.checked[x.key] = {"at": recent if i < done else old,
                                "empty": True, "blank": True, "healthy": True}
            if i < done:
                s.focus_tries[x.key] = 1
        return s

    def test_it_reports_progress(self):
        w = self._windows()
        lines = sweeper.focus_eta(w, self._store(w, 8), [1],
                                  max_age_hours=0, max_tries=1)
        assert "8 of 20 re-priced" in " ".join(lines)

    def test_it_estimates_the_time_left(self):
        w = self._windows()
        text = " ".join(sweeper.focus_eta(w, self._store(w, 8), [1],
                                          max_age_hours=0, max_tries=1))
        assert "h left" in text and "finishes about" in text

    def test_too_few_samples_refuses_to_guess(self):
        w = self._windows()
        text = " ".join(sweeper.focus_eta(w, self._store(w, 2), [1],
                                          max_age_hours=0, max_tries=1))
        assert "too few to estimate" in text
        assert "h left" not in text

    def test_a_finished_focus_says_finished(self):
        w = self._windows()
        s = self._store(w, len(w))
        for x in w:
            s.focus_tries[x.key] = 1
        text = " ".join(sweeper.focus_eta(w, s, [1],
                                          max_age_hours=0, max_tries=1))
        assert "finished" in text

    def test_no_focus_means_no_output(self):
        w = self._windows()
        assert sweeper.focus_eta(w, self._store(w, 5), [],
                                 max_age_hours=0, max_tries=1) == []

    def test_only_focus_months_count_towards_the_rate(self):
        """Counting every check would include the one-in-five freshness
        launches to the hot list and overstate the pace by a fifth."""
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("tracker/sweeper.py").read_text(encoding="utf-8"))
        assert "for key in in_focus:" in src

    def test_status_prints_it(self):
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8"))
        assert "for line in focus_eta(windows, store, s_months," in src
