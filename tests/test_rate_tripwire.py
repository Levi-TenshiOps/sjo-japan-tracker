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
        assert "if store.rests_total > rests_seen:" in src
        # consecutive_rests is reset on recovery, so watching it makes the
        # tripwire a one-shot. See test_it_keeps_firing_after_a_recovery.
        assert "if store.consecutive_rests > rests_seen:" not in src
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
        # 1.15, not 1.2: a page costs LAUNCH_SECONDS by itself, so the last
        # rungs buy less throughput than their delays suggest (15 -> 10 is
        # 2,979 -> 3,600, a 1.21x). They still buy real coverage - the
        # one-day catch rate goes 74% -> 89% - which is the point.
        for a, b in zip(RATE_LADDER, RATE_LADDER[1:]):
            assert self.per_day(b) > self.per_day(a) * 1.15, (a, b)


class TestItActuallyFires:
    """Source-matching pins the wiring; this drives the real loop.

    A safety net nobody has watched fire is not a safety net - and this
    one exists specifically so a rate experiment stops itself when nobody
    is reading the log.

    Everything the run needs is built here. The first version of this test
    borrowed the developer's own `preferences.json`, which is gitignored by
    non-negotiable #8 (no personal data in tracked files), so CI had no copy
    and `main()` returned 2 before the loop ever started - `seen` came back
    empty and the whole suite went red on every push. A test that reads the
    machine it was written on is not a test.
    """

    def _prefs(self, tmp_path):
        from tracker.preferences import Preferences
        p = Preferences(alert_email="a@b.c", search_months=12,
                        min_lead_days=21, departure_step_days=1,
                        trip_weeks=[3], extra_nights=[],
                        destinations=["TYO"], priority_months=[])
        path = tmp_path / "preferences.json"
        p.save(path)
        return path

    def test_a_rest_during_a_batch_lowers_the_next_batch_rate(
            self, tmp_path, monkeypatch, caplog):
        import logging
        import sweep_forever

        seen: list[float] = []

        def fake_batch(windows, store, **kw):
            seen.append(kw["delay_s"])
            # Google started refusing: the sweep gave up and rested. Both
            # counters move, exactly as the real rest path does.
            store.consecutive_rests += 1
            store.rests_total += 1
            return 1

        monkeypatch.setattr(sweep_forever, "sweep_batch", fake_batch)
        # CI has no browser, and this test is not about finding one.
        monkeypatch.setattr(sweep_forever, "chrome_path",
                            lambda *a, **k: "/nonexistent/chrome")
        # The real sweeper may well be running on a developer machine. Its
        # single-instance guard firing here is correct behaviour, but it is
        # not what this test is about, so stand it down.
        monkeypatch.setattr(sweep_forever, "another_sweeper_running",
                            lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "claim_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "release_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever.time, "sleep", lambda *_: None)

        # --log defaults to the *production* sweep.log, and the handler is
        # added to the root logger and never removed. Without this, calling
        # main() here writes every later test's log output into the live
        # file: on 2026-08-25 that put 182 fake "that is a throttle"
        # warnings and four "the background sweep has stopped" alarms into
        # the real log, and they were read as a real throttle.
        #
        # --focus none keeps the run independent of config.yaml's own
        # sweep_focus_months, which would otherwise steer the picks.
        argv = ["sweep_forever.py", "--once", "--delay", "15",
                "--store", str(tmp_path / "store.json"),
                "--preferences", str(self._prefs(tmp_path)),
                "--focus", "none", "--log", ""]
        monkeypatch.setattr(sweep_forever.sys, "argv", argv)

        with caplog.at_level(logging.WARNING):
            rc = sweep_forever.main()

        assert rc == 0, f"main() bailed out with {rc} before the loop"
        assert seen == [15.0], "the first batch must run at the rate asked for"
        assert any("TRIPWIRE" in r.message for r in caplog.records),             "a rest went unremarked"

        from tracker.sweeper import slower_rate_step
        assert slower_rate_step(15.0) == 25.0

    def test_it_keeps_firing_after_a_recovery(self, tmp_path, monkeypatch, caplog):
        """The bug this test exists for.

        `consecutive_rests` is reset to 0 on recovery and by
        `forget_stale_health`. The tripwire originally watched it, so:

            rest #1  consecutive_rests 1 > 0  -> fires, 15 -> 25
            recovery consecutive_rests reset to 0
            rest #2  consecutive_rests 1 > 1  -> FALSE, never fires again

        It backed off exactly once and then sat there for the life of the
        process. The documented 15 -> 25 -> 40 -> 60 -> 90 ladder could not
        happen. `rests_total` is never reset, which is the whole point.
        """
        import logging
        import sweep_forever

        seen: list[float] = []
        calls = {"n": 0}

        def fake_batch(windows, store, **kw):
            seen.append(kw["delay_s"])
            calls["n"] += 1
            store.consecutive_rests += 1
            store.rests_total += 1
            # Google recovers between rests, exactly as production does.
            store.consecutive_rests = 0
            if calls["n"] >= 4:
                raise KeyboardInterrupt
            return 1

        monkeypatch.setattr(sweep_forever, "sweep_batch", fake_batch)
        monkeypatch.setattr(sweep_forever, "chrome_path",
                            lambda *a, **k: "/nonexistent/chrome")
        monkeypatch.setattr(sweep_forever, "another_sweeper_running",
                            lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "claim_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever, "release_instance", lambda *a, **k: None)
        monkeypatch.setattr(sweep_forever.time, "sleep", lambda *_: None)

        argv = ["sweep_forever.py", "--delay", "15",
                "--store", str(tmp_path / "store.json"),
                "--preferences", str(self._prefs(tmp_path)),
                "--focus", "none", "--log", ""]
        monkeypatch.setattr(sweep_forever.sys, "argv", argv)

        with caplog.at_level(logging.WARNING):
            try:
                sweep_forever.main()
            except KeyboardInterrupt:
                pass

        assert seen == [15.0, 25.0, 40.0, 60.0], (
            f"the ladder stalled: {seen}")
