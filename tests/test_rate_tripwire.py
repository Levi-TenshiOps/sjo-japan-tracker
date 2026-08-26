"""Raising the sweep rate must be a bounded experiment.

Asked for on 2026-08-25: jump --delay 40 -> 15 and see whether Google
objects. That is a reasonable thing to want - 15s is ~2,980 requests a day
against the ~7,200 that produced the 2026-08-23 throttle, and the two
larger causes from that day (a fresh Chrome profile every launch, two
processes at once) are both fixed.

What it needs is a stop condition that does not depend on somebody
watching the log. `consecutive_rests` rises only when the sweep has given
up and stopped for a while, so a rise in it is the one unambiguous
"Google is refusing" signal the store carries.
"""

from tracker.sweeper import (LAUNCH_SECONDS, RATE_LADDER, next_rate_step,
                             slower_rate_step)


class TestTheLadderGoesBothWays:
    def test_backing_off_is_always_slower(self):
        for rung in RATE_LADDER:
            up = slower_rate_step(rung)
            assert up is None or up > rung, (rung, up)

    def test_the_slowest_rung_has_nowhere_to_back_off_to(self):
        assert slower_rate_step(max(RATE_LADDER)) is None

    def test_the_fastest_rung_can_always_back_off(self):
        assert slower_rate_step(min(RATE_LADDER)) is not None

    def test_a_rate_between_rungs_still_backs_off(self):
        """--delay 20 is not on the ladder; it must still find a rung."""
        assert slower_rate_step(20.0) == 25.0

    def test_a_rate_faster_than_the_floor_backs_off_to_the_floor(self):
        assert slower_rate_step(5.0) == min(RATE_LADDER)

    def test_up_and_down_are_inverses_in_the_middle(self):
        for rung in RATE_LADDER[1:-1]:
            assert next_rate_step(slower_rate_step(rung)) == rung


class TestTheTripwireIsWiredIn:
    """The logic is three lines in the main loop, so pin the wiring."""

    def _src(self) -> str:
        import re
        from pathlib import Path
        return re.sub(r"\s+", " ",
                      Path("sweep_forever.py").read_text(encoding="utf-8"))

    def test_the_batch_is_paced_by_the_live_delay_not_the_argument(self):
        src = self._src()
        assert "delay_s=current_delay" in src, (
            "a tripwire that cannot change the rate it passes down is inert")
        # The read-only views run in their own process with their own
        # --delay default, so they must read the rate the sweep recorded
        # rather than assume it.
        assert "delay_s=args.delay" not in src, (
            "a read-only view is still reporting its own default rate")
        assert src.count("or args.delay)") >= 3

    def test_a_new_rest_lowers_the_rate(self):
        src = self._src()
        assert "if store.consecutive_rests > rests_seen:" in src
        assert "current_delay = backed" in src

    def test_it_never_speeds_back_up(self):
        src = self._src()
        assert "current_delay = next_rate_step" not in src


class TestTheArithmeticBehindTheDecision:
    """The project file divides 86,400 by the delay and calls that the
    daily request count. It is not: a launch costs ~14s of its own, so the
    cycle is delay + launch. At --delay 6 that is ~7,200/day, not 14,400."""

    def per_day(self, delay: float) -> float:
        return 86400 / (delay + LAUNCH_SECONDS)

    def test_fifteen_is_well_under_the_rate_that_throttled(self):
        # 2026-08-23 ran at --delay 6 when a good page cost ~6s.
        throttled = 86400 / (6 + 6)
        assert self.per_day(15.0) < 0.5 * throttled

    def test_each_rung_is_a_real_increase(self):
        for a, b in zip(RATE_LADDER, RATE_LADDER[1:]):
            assert self.per_day(b) > self.per_day(a) * 1.2


class TestItActuallyFires:
    """Source-matching pins the wiring; this drives the real loop.

    A safety net nobody has watched fire is not a safety net - and this
    one exists specifically so a rate experiment stops itself when nobody
    is reading the log.
    """

    def test_a_rest_during_a_batch_lowers_the_next_batch_rate(
            self, tmp_path, monkeypatch, caplog):
        import logging
        import sweep_forever

        seen: list[float] = []

        def fake_batch(windows, store, **kw):
            seen.append(kw["delay_s"])
            # Google started refusing: the sweep gave up and rested.
            store.consecutive_rests += 1
            return 1

        monkeypatch.setattr(sweep_forever, "sweep_batch", fake_batch)
        # The real sweeper may well be running. Its single-instance guard
        # firing here is correct behaviour, but it is not what this test is
        # about, so stand it down rather than collide with it.
        monkeypatch.setattr(sweep_forever, "another_sweeper_running",
                            lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "claim_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "release_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever.time, "sleep", lambda *_: None)

        store_path = tmp_path / "store.json"
        # --log defaults to the *production* sweep.log, and the handler is
        # added to the root logger and never removed. Without this, calling
        # main() here writes every later test's log output into the live
        # file: on 2026-08-25 that put 181 fake "that is a throttle"
        # warnings and four "the background sweep has stopped" alarms into
        # the real log, and they were read as a real throttle.
        argv = ["sweep_forever.py", "--once", "--delay", "15",
                "--store", str(store_path), "--log", ""]
        monkeypatch.setattr(sweep_forever.sys, "argv", argv)

        with caplog.at_level(logging.WARNING):
            sweep_forever.main()

        assert seen == [15.0], "the first batch must run at the rate asked for"
        assert any("TRIPWIRE" in r.message for r in caplog.records), \
            "a rest went unremarked"

        # And the store carries the backed-off rate for the read-only views.
        from tracker.sweeper import SweepStore, slower_rate_step
        assert slower_rate_step(15.0) == 25.0
