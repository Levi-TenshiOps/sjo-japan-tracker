"""`--stop` must not report success until the sweep has actually stopped.

Reported 2026-08-30 while rehearsing the sale-day steps. It wrote the stop
flag and returned immediately, printing "Stop requested. The running sweep
will finish its current window..." - which reads as done and is not. The
sweep finishes its current window first, and can be queued behind a
scheduled run's whole Chrome phase, about four minutes.

So step 1 of the sale-day sequence looks finished, step 2 runs into a
still-live sweeper, and the single-instance guard refuses it. The user has
to notice, wait, and try again - on the one day there is no time for that.

Run with nothing running it was worse: the same hopeful message, plus a
`sweep.stop` file left behind for a sweep that did not exist.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import sweep_forever
from sweep_forever import STOP_TIMEOUT_S, stop_and_wait


class Fake:
    """A sweeper that exits after `alive_for` polls."""

    def __init__(self, alive_for):
        self.left = alive_for

    def __call__(self, *a, **k):
        if self.left <= 0:
            return None
        self.left -= 1
        return 4242


@pytest.fixture(autouse=True)
def _no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class TestNothingRunning:
    def test_it_says_so_and_succeeds(self, capsys):
        assert stop_and_wait(running=lambda *a, **k: None) == 0
        assert "Nothing to stop" in capsys.readouterr().out

    def test_it_leaves_no_stop_flag_behind(self):
        """A flag for a sweep that does not exist is a lie on disk."""
        stop_and_wait(running=lambda *a, **k: None)
        assert not os.path.exists(sweep_forever.STOP_FILE)

    def test_it_clears_a_flag_someone_left_earlier(self):
        sweep_forever.request_stop()
        stop_and_wait(running=lambda *a, **k: None)
        assert not os.path.exists(sweep_forever.STOP_FILE)


class TestItWaitsForTheRealExit:
    def test_it_does_not_return_while_the_sweep_lives(self):
        polls = []
        rc = stop_and_wait(running=Fake(3),
                           sleep=lambda s: polls.append(s), poll_s=2.0)
        assert rc == 0
        assert len(polls) >= 3, "it returned before the sweeper had gone"

    def test_it_reports_success_only_after_the_exit(self, capsys):
        stop_and_wait(running=Fake(2), sleep=lambda s: None, poll_s=2.0)
        out = capsys.readouterr().out
        assert "Stopped after" in out
        assert "Safe to start" in out

    def test_it_clears_the_flag_once_stopped(self):
        stop_and_wait(running=Fake(1), sleep=lambda s: None, poll_s=2.0)
        assert not os.path.exists(sweep_forever.STOP_FILE)

    def test_the_flag_is_written_while_it_waits(self):
        seen = []

        def watch(_s):
            seen.append(os.path.exists(sweep_forever.STOP_FILE))

        stop_and_wait(running=Fake(2), sleep=watch, poll_s=2.0)
        assert seen and all(seen), "the sweep was never actually asked to stop"


class TestItGivesUpHonestly:
    def test_a_sweep_that_never_exits_times_out(self, capsys):
        rc = stop_and_wait(running=lambda *a, **k: 4242,
                           sleep=lambda s: None, poll_s=2.0, timeout_s=10.0)
        assert rc == 1, "a timeout reported success"
        out = capsys.readouterr().out
        assert "Still running" in out
        assert "--stop" in out, "it did not say what to do next"

    def test_the_request_stands_after_a_timeout(self):
        """Do not withdraw the stop: the sweep may be about to act on it."""
        stop_and_wait(running=lambda *a, **k: 4242,
                      sleep=lambda s: None, poll_s=2.0, timeout_s=6.0)
        assert os.path.exists(sweep_forever.STOP_FILE)

    def test_the_timeout_covers_a_scheduled_run_holding_the_lock(self):
        """The sweep waits behind a run's whole Chrome phase, ~4 minutes."""
        assert STOP_TIMEOUT_S >= 300


class TestTheFlagIsWired:
    def test_stop_timeout_is_configurable(self, monkeypatch):
        monkeypatch.setattr(sweep_forever.sys, "argv",
                            ["sweep_forever.py", "--stop",
                             "--stop-timeout", "12"])
        assert sweep_forever.build_args().stop_timeout == 12.0

    def test_main_passes_it_through(self):
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("sweep_forever.py").read_text(encoding="utf-8")) \
            if os.path.exists("sweep_forever.py") else None
        if src is None:                      # chdir'd to tmp_path
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            src = re.sub(r"\s+", " ", open(os.path.join(root, "sweep_forever.py"),
                                           encoding="utf-8").read())
        assert "stop_and_wait(args.store, timeout_s=args.stop_timeout)" in src
