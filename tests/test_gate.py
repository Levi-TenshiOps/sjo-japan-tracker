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
