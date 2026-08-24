"""One process talks to Google at a time. Enforced, not agreed.

Measured 2026-08-23: a second process pricing windows alongside the sweep
took the hit rate from 87% to 24%. Google answered in 3-4 seconds with
empty pages, and every one of those was a window whose fares went unseen.

Resolving to be careful is not a fix, because the collision is built into
the design. The sweep runs continuously and the scheduled tracker runs six
times a day - they were *always* going to overlap, roughly every four
hours, and nobody would have noticed because the symptom is silence rather
than an error.

So the rule is enforced with a lock file. Every path that reaches Google
takes it first: `sweep_forever.py` around each window, `cli.py` around its
whole search phase, and any throwaway diagnostic script that ever gets
written. Whoever arrives second waits.

The granularity is deliberate. The sweep takes and releases the lock per
*window*, not per run, so it never holds it for more than about twenty
seconds. A scheduled run therefore waits one window at most - it has an
email to send and should not queue behind a fourteen-hour sweep - while the
sweep loses only the couple of minutes the run needs.

A lock file is a liability if the holder dies, so this one carries a PID and
a heartbeat. A lock whose owning process is gone, or that has not been
touched in `stale_after` seconds, is broken and taken. That trades a small
chance of two writers for the certainty of a permanently wedged sweep, which
is the right way round: the worst case here is a throttle we already detect
and recover from.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LOCK = "google.lock"

# A sweep window takes ~12s and a scheduled run's search phase a few minutes.
# Past this with no heartbeat, assume the holder died rather than wait out
# a lock nobody owns.
STALE_AFTER_SECONDS = 600
HEARTBEAT_EVERY = 20.0


def _alive(pid: int) -> bool:
    """Is that process still running? Conservative: unsure means yes."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        except (OSError, ValueError, subprocess.SubprocessError):
            return True    # cannot tell - do not steal the lock on a guess
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # Definitive: no such process. This used to fall into the catch-all
        # below and come back True, so on POSIX a lock left by a dead holder
        # was never detected as stale - the next caller sat out the whole
        # 300-second timeout and only then proceeded. Windows took the
        # tasklist branch and worked, which is exactly why it survived: the
        # deployment machine is Windows and only CI ever ran the other path.
        return False
    except PermissionError:
        return True        # it exists, it is just not ours to signal
    except (OSError, ValueError):
        return True        # cannot tell - do not steal the lock on a guess
    return True


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def is_stale(path: Path, *, stale_after: float = STALE_AFTER_SECONDS) -> bool:
    """True when the lock file exists but nobody is really behind it."""
    info = _read(path)
    if info is None:
        return True                     # unreadable - treat as abandoned
    age = time.time() - float(info.get("beat", 0) or 0)
    if age > stale_after:
        return True
    return not _alive(int(info.get("pid", -1) or -1))


def _claim(path: Path, owner: str) -> bool:
    """Create the lock exclusively. False if somebody else got there."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "owner": owner, "beat": time.time()}, fh)
    return True


@contextlib.contextmanager
def google(owner: str, *, path: str | Path = DEFAULT_LOCK,
           timeout: float = 300.0, poll: float = 1.0,
           stale_after: float = STALE_AFTER_SECONDS,
           on_timeout: str = "proceed"):
    """Hold the right to query Google for the duration of the block.

    Waits up to `timeout` for the current holder, then does what
    `on_timeout` says:

    * ``"proceed"`` - go ahead anyway. Right for a *scheduled run*: it has
      an email waiting, and skipping it because a lock file was untidy is a
      worse outcome than one extra concurrent request.
    * ``"wait"`` - keep waiting. Right for the *sweep*, which has no
      deadline at all. Proceeding buys it one window and costs the thing the
      lock exists to protect.

    The default was "proceed" for everyone, and on 2026-08-24 the sweep took
    it: the 06:42 run held the lock for its whole Chrome phase, four minutes
    and change, the sweep waited its 300 seconds and then queried Google
    alongside it. That is precisely the concurrency this module exists to
    prevent - measured 2026-08-23, it took the hit rate from 87% to 24%.

    Waiting cannot deadlock. A holder that dies, or stops heartbeating for
    `stale_after`, has its lock broken by the loop below regardless of this
    setting.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    held = False

    while True:
        if _claim(p, owner):
            held = True
            break
        if is_stale(p, stale_after=stale_after):
            info = _read(p) or {}
            log.warning("Breaking a stale Google lock held by %s (pid %s)",
                        info.get("owner", "?"), info.get("pid", "?"))
            with contextlib.suppress(OSError):
                p.unlink()
            continue
        if time.monotonic() >= deadline:
            info = _read(p) or {}
            if on_timeout == "wait":
                log.info("Still waiting for the Google lock (held by %s). "
                         "Waiting is correct here: this caller has no "
                         "deadline, and querying alongside the holder is "
                         "what the lock exists to prevent.",
                         info.get("owner", "?"))
                deadline = time.monotonic() + timeout
                time.sleep(poll)
                continue
            log.warning("Waited %.0fs for the Google lock (held by %s); "
                        "proceeding anyway", timeout, info.get("owner", "?"))
            break
        time.sleep(poll)

    try:
        yield Heartbeat(p if held else None, owner)
    finally:
        if held:
            with contextlib.suppress(OSError):
                p.unlink()


class Heartbeat:
    """Keeps a long-held lock looking alive so nobody breaks it."""

    def __init__(self, path: Path | None, owner: str) -> None:
        self.path, self.owner, self._last = path, owner, 0.0

    def beat(self) -> None:
        """Call periodically during long work. Cheap and rate-limited."""
        if self.path is None:
            return
        now = time.time()
        if now - self._last < HEARTBEAT_EVERY:
            return
        self._last = now
        try:
            self.path.write_text(json.dumps(
                {"pid": os.getpid(), "owner": self.owner, "beat": now}),
                encoding="utf-8")
        except OSError:
            pass            # losing a heartbeat is survivable; crashing is not


def holder(path: str | Path = DEFAULT_LOCK) -> str | None:
    """Who holds the lock right now, for status output. None if free."""
    p = Path(path)
    if not p.exists() or is_stale(p):
        return None
    info = _read(p) or {}
    return f"{info.get('owner', '?')} (pid {info.get('pid', '?')})"
