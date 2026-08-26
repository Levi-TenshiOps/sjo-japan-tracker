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
        s = sweeper.SweepStore()
        s.delay_s = 5.0
        p = tmp_path / "d.json"
        s.save(p)
        sys.argv = ["sweep_forever.py"]
        assert sweep_forever.build_args().delay == 90.0 or \
               sweep_forever.build_args().delay == 40.0, \
               "the default must be a safe constant, not the last rate used"


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
    def test_a_finished_focus_hands_back(self):
        """After the sale day the sweep must return to normal by itself,
        without another restart."""
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("tracker/sweeper.py").read_text(encoding="utf-8"))
        assert "Resuming the full rotation." in src

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
