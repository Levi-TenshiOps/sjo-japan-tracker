"""A fast default must not survive a reboot into an active throttle.

Asked for 2026-08-27: make --delay 5 the default so a restart comes back
at the rate actually wanted. That is the right place to put it - the code
default, never the launcher, which is what `sweep_forever.py` has said
since the 2026-08-23 incident.

But it reopens the hole that incident came through. The rate tripwire
backs off when Google refuses and then dies with the process, so a machine
that throttles at 03:00 and reboots at 06:00 comes back at the fast
default into an address still refusing. In August that was `--delay 6`
re-armed by the Startup launcher at every boot.

`safe_start_delay` closes it: a fresh start shortly after a throttle does
not run fast. An explicit --delay is always obeyed, because someone typing
it is present and watching.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

import pytest

import sweep_forever
from sweep_forever import SAFE_START_AFTER_THROTTLE_H, safe_start_delay
from tracker.sweeper import SweepStore


def store(*, throttled_h=None, rests=0):
    s = SweepStore()
    s.consecutive_rests = rests
    if throttled_h is not None:
        s.last_throttle = (datetime.now(timezone.utc)
                           - timedelta(hours=throttled_h)).isoformat()
    return s


class TestTheDefaultIsFast:
    def test_the_default_is_five(self, monkeypatch):
        monkeypatch.setattr(sweep_forever.sys, "argv", ["sweep_forever.py"])
        assert sweep_forever.build_args().delay == 5.0

    def test_a_reboot_comes_back_fast_when_all_is_well(self):
        d, why = safe_start_delay(store(throttled_h=100), 5.0, asked=False)
        assert d == 5.0 and why == ""

    def test_a_store_that_never_throttled_starts_fast(self):
        d, why = safe_start_delay(store(), 5.0, asked=False)
        assert d == 5.0 and why == ""


class TestItRefusesToStartFastAfterATrouble:
    def test_a_recent_throttle_slows_the_start(self):
        d, why = safe_start_delay(store(throttled_h=1), 5.0, asked=False)
        assert d == 40.0
        assert "throttled" in why

    def test_still_backing_off_slows_the_start(self):
        """consecutive_rests > 0 means the last run was resting when it
        died - the strongest possible signal not to come back fast."""
        d, why = safe_start_delay(store(rests=2), 5.0, asked=False)
        assert d == 40.0
        assert "backing off" in why

    def test_the_boundary_is_the_quiet_window(self):
        assert safe_start_delay(store(throttled_h=SAFE_START_AFTER_THROTTLE_H - 1),
                                5.0, asked=False)[0] == 40.0
        assert safe_start_delay(store(throttled_h=SAFE_START_AFTER_THROTTLE_H + 1),
                                5.0, asked=False)[0] == 5.0

    def test_it_never_speeds_a_slow_request_up(self):
        """max(), not a replacement: asking for 90 must stay 90."""
        d, _ = safe_start_delay(store(throttled_h=1), 90.0, asked=False)
        assert d == 90.0


class TestAnExplicitFlagIsAlwaysObeyed:
    def test_typing_the_delay_overrides_the_guard(self):
        d, why = safe_start_delay(store(throttled_h=1), 5.0, asked=True)
        assert d == 5.0 and why == ""

    def test_even_while_backing_off(self):
        d, _ = safe_start_delay(store(rests=3), 5.0, asked=True)
        assert d == 5.0


class TestItDegradesQuietly:
    def test_a_corrupt_timestamp_does_not_crash_the_start(self):
        s = SweepStore()
        s.last_throttle = "not-a-date"
        d, why = safe_start_delay(s, 5.0, asked=False)
        assert d == 5.0

    def test_an_object_missing_the_fields_is_tolerated(self):
        class Bare:
            pass
        d, why = safe_start_delay(Bare(), 5.0, asked=False)
        assert d == 5.0


class TestTheLauncherStillCarriesNoRate:
    """The rule that outlives every fix: the rate lives in the code, never
    in a file that runs unattended at boot."""

    def test_the_installer_prints_no_delay(self):
        import re
        from pathlib import Path
        src = Path("install_schedule.py").read_text(encoding="utf-8")
        launcher = "".join(l for l in src.splitlines()
                           if "sweep_forever.py" in l and "start" in l)
        assert "--delay" not in launcher, (
            "a rate in the boot launcher outlives every later fix")
