"""One process talks to Google at a time, enforced by a lock file.

Measured 2026-08-23: a second process pricing windows alongside the sweep
took the hit rate from 87% to 24%. Being careful is not a fix, because the
collision is structural - the sweep runs continuously and the scheduled
tracker six times a day, so they were always going to overlap.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time

import pytest

from tracker import gate as gate_mod
from tracker.gate import (
    Heartbeat, google, holder, is_stale,
)


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "google.lock"


class TestMutualExclusion:
    def test_the_lock_exists_while_held_and_is_gone_after(self, lock):
        with google("sweep", path=lock):
            assert lock.exists()
        assert not lock.exists()

    def test_the_holder_is_named(self, lock):
        with google("sweep", path=lock):
            assert "sweep" in (holder(lock) or "")
        assert holder(lock) is None

    def test_a_second_caller_does_not_get_in_while_held(self, lock):
        """The whole point. The waiter gives up rather than running alongside."""
        with google("sweep", path=lock):
            info = json.loads(lock.read_text(encoding="utf-8"))
            first_pid = info["pid"]
            # A second attempt with no patience: it must find the lock taken.
            with google("run", path=lock, timeout=0.0, poll=0.01):
                still = json.loads(lock.read_text(encoding="utf-8"))
            assert still["pid"] == first_pid
            assert still["owner"] == "sweep", "the first holder must not be evicted"

    def test_the_lock_survives_the_waiter_giving_up(self, lock):
        with google("sweep", path=lock):
            with google("run", path=lock, timeout=0.0, poll=0.01):
                pass
            assert lock.exists(), "a waiter must never delete a live lock"

    def test_released_on_an_exception(self, lock):
        with pytest.raises(ValueError):
            with google("sweep", path=lock):
                raise ValueError("boom")
        assert not lock.exists(), "a crash must not wedge the sweep forever"

    def test_sequential_callers_each_get_it(self, lock):
        for name in ("sweep", "run", "sweep"):
            with google(name, path=lock):
                assert holder(lock).startswith(name)


class TestStaleLocks:
    def _write(self, lock, *, pid, beat):
        lock.write_text(json.dumps({"pid": pid, "owner": "ghost", "beat": beat}),
                        encoding="utf-8")

    def test_a_lock_from_a_dead_process_is_stale(self, lock):
        self._write(lock, pid=999_999, beat=time.time())
        assert is_stale(lock)

    def test_a_fresh_lock_from_a_live_process_is_not(self, lock):
        self._write(lock, pid=os.getpid(), beat=time.time())
        assert not is_stale(lock)

    def test_an_old_heartbeat_is_stale_even_if_the_pid_lives(self, lock):
        """A hung holder must not block the sweep indefinitely."""
        self._write(lock, pid=os.getpid(), beat=time.time() - 10_000)
        assert is_stale(lock)

    def test_an_unreadable_lock_is_stale(self, lock):
        lock.write_text("{ not json", encoding="utf-8")
        assert is_stale(lock)

    def test_a_stale_lock_is_broken_and_taken(self, lock):
        self._write(lock, pid=999_999, beat=time.time())
        with google("run", path=lock):
            info = json.loads(lock.read_text(encoding="utf-8"))
            assert info["owner"] == "run" and info["pid"] == os.getpid()

    def test_holder_reports_nothing_for_a_stale_lock(self, lock):
        self._write(lock, pid=999_999, beat=time.time())
        assert holder(lock) is None


class TestHeartbeat:
    def test_beating_refreshes_the_timestamp(self, lock):
        with google("sweep", path=lock) as hb:
            first = json.loads(lock.read_text(encoding="utf-8"))["beat"]
            hb._last = 0.0                  # bypass the rate limit
            time.sleep(0.01)
            hb.beat()
            assert json.loads(lock.read_text(encoding="utf-8"))["beat"] > first

    def test_beating_is_rate_limited(self, lock):
        with google("sweep", path=lock) as hb:
            hb.beat()
            before = json.loads(lock.read_text(encoding="utf-8"))["beat"]
            hb.beat()
            assert json.loads(lock.read_text(encoding="utf-8"))["beat"] == before

    def test_a_beat_without_the_lock_is_a_no_op(self, tmp_path):
        """A caller that proceeded on timeout must not stamp on the holder."""
        Heartbeat(None, "run").beat()       # must not raise


class TestTimeoutBehaviour:
    def test_proceeds_rather_than_raising(self, lock):
        """A missed email is worse than one extra concurrent request, and
        the throttle detection catches the latter."""
        with google("sweep", path=lock):
            with google("run", path=lock, timeout=0.05, poll=0.01) as hb:
                assert hb.path is None, "it knows it does not hold the lock"

    def test_a_free_lock_is_taken_immediately(self, lock):
        start = time.monotonic()
        with google("run", path=lock, timeout=30.0):
            pass
        assert time.monotonic() - start < 1.0


class TestLivenessOnPosix:
    """`_alive` must give a definitive answer when POSIX gives it one.

    Found 2026-08-23 by CI, which is the only place the POSIX branch runs -
    the deployment machine is Windows and takes the `tasklist` path. On
    Linux `os.kill(dead_pid, 0)` raises ProcessLookupError, which the old
    blanket `except OSError: return True` swallowed into "assume alive". So
    a lock left behind by a dead process was never stale, and the next
    caller waited out the full 300-second timeout before proceeding.

    That is also why the CI job took five minutes to fail: three tests each
    sat through a 300s wait that should have been instant.
    """

    def _posix(self, monkeypatch):
        monkeypatch.setattr(gate_mod.os, "name", "posix")

    def test_a_dead_pid_is_definitively_not_alive(self, monkeypatch):
        self._posix(monkeypatch)

        def gone(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(gate_mod.os, "kill", gone)
        assert gate_mod._alive(999_999) is False

    def test_a_pid_we_may_not_signal_is_alive(self, monkeypatch):
        """EPERM means it exists and belongs to somebody else."""
        self._posix(monkeypatch)

        def denied(pid, sig):
            raise PermissionError()

        monkeypatch.setattr(gate_mod.os, "kill", denied)
        assert gate_mod._alive(1) is True

    def test_an_unclear_error_still_assumes_alive(self, monkeypatch):
        """Never steal a lock on a guess."""
        self._posix(monkeypatch)

        def odd(pid, sig):
            raise OSError("something else entirely")

        monkeypatch.setattr(gate_mod.os, "kill", odd)
        assert gate_mod._alive(1) is True

    def test_a_signalable_pid_is_alive(self, monkeypatch):
        self._posix(monkeypatch)
        monkeypatch.setattr(gate_mod.os, "kill", lambda pid, sig: None)
        assert gate_mod._alive(1) is True


class TestAStaleLockCostsNoWait:
    def test_breaking_a_stale_lock_is_immediate(self, lock):
        """The symptom that made CI take five minutes instead of seconds.

        A dead holder must be detected and its lock broken at once, not
        waited out for the default 300-second timeout.
        """
        lock.write_text(json.dumps(
            {"pid": 999_999, "owner": "ghost", "beat": time.time()}),
            encoding="utf-8")
        start = time.monotonic()
        with google("run", path=lock):
            pass
        assert time.monotonic() - start < 5.0


class TestTheSweepWaitsRatherThanBargingIn:
    """Proceeding on timeout is right for a run, wrong for the sweep.

    Found in the live log 2026-08-24:

        06:47:17 WARNING Waited 300s for the Google lock
                 (held by run:chrome); proceeding anyway

    The 06:42 scheduled run held the lock for its whole Chrome phase -
    06:44:34 to 06:48:47, over four minutes - and the sweep, whose timeout
    is 300s, gave up and queried Google alongside it. That is exactly the
    concurrency this module exists to prevent: measured 2026-08-23 it took
    the hit rate from 87% to 24%, and the failure is silent, so every window
    the sweep priced in that window may have been recorded as empty when it
    was not.

    A scheduled run has an email waiting and should barge in. The sweep runs
    forever and has no deadline; losing one window costs nothing, and taking
    it costs the thing the lock protects.
    """

    def test_a_run_still_proceeds_on_timeout(self, lock):
        """Unchanged: the email must not be skipped over a lock file."""
        with google("sweep", path=lock):
            with google("run", path=lock, timeout=0.05, poll=0.01) as hb:
                assert hb.path is None, "it proceeded without the lock"

    def test_the_sweep_keeps_waiting_instead(self, lock):
        import threading
        released = threading.Event()
        got = {}

        def waiter():
            with google("sweep", path=lock, timeout=0.05, poll=0.01,
                        on_timeout="wait") as hb:
                got["held"] = hb.path is not None
                got["after_release"] = released.is_set()

        with google("run:chrome", path=lock):
            t = threading.Thread(target=waiter, daemon=True)
            t.start()
            time.sleep(0.4)          # far longer than its 0.05s timeout
            assert not got, "the sweep proceeded instead of waiting"
            released.set()
        t.join(timeout=5)
        assert got.get("held") is True, "it should end up actually holding it"
        assert got.get("after_release") is True

    def test_waiting_still_breaks_a_dead_holder(self, lock):
        """Waiting must not become a deadlock."""
        lock.write_text(json.dumps(
            {"pid": 999_999, "owner": "ghost", "beat": time.time()}),
            encoding="utf-8")
        start = time.monotonic()
        with google("sweep", path=lock, timeout=0.05, poll=0.01,
                    on_timeout="wait") as hb:
            assert hb.path is not None
        assert time.monotonic() - start < 5.0

    def test_the_sweeper_asks_for_waiting(self):
        """The setting is only useful if the sweep actually passes it."""
        import inspect
        from tracker import sweeper
        src = inspect.getsource(sweeper.sweep_batch)
        assert 'on_timeout="wait"' in src
