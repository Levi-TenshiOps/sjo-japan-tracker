#!/usr/bin/env python3
"""Run the sale-day sequence unattended: focus, wait, email.

    python sale_day.py                 # do it now
    python sale_day.py --at 02:00      # wait until 02:00, then do it

The four steps in the README's Sale day section, with the waiting done for
you:

1. stop any running sweep cleanly;
2. start it again focused on the priority months, re-pricing every one of
   them exactly once;
3. wait for that to finish - which is the part a person cannot schedule,
   because `sweep_forever` does not exit when a focus completes. It hands
   back to the ordinary rotation and keeps running, so the finish has to be
   detected rather than timed;
4. email the results, then leave the sweep running normally.

Detection reads `focus_done_logged` from the store. The sweep clears that
flag at startup and sets it the moment the focus has nothing left, so it is
the same signal the log line comes from rather than a guess at the log's
wording.

Everything is written to `sale_day.log`, because this runs at 2am with
nobody watching and the whole point is being able to see afterwards what
happened.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tracker.sweeper import SweepStore                      # noqa: E402
from sweep_forever import (DEFAULT_STORE, another_sweeper_running,  # noqa: E402
                           stop_and_wait)

log = logging.getLogger("saleday")

#: Give up waiting for the focus after this. Seven hours is the measured
#: run; twelve leaves room for a throttle, which slows it to about nine
#: rather than stopping it.
MAX_WAIT_H = 12.0
POLL_S = 120.0


def _python() -> str:
    """The interpreter running this, so a venv is preserved."""
    return sys.executable


def wait_until(clock: str) -> None:
    """Sleep until the next occurrence of HH:MM, local time."""
    want = datetime.strptime(clock, "%H:%M").time()
    now = datetime.now()
    target = datetime.combine(now.date(), want)
    if target <= now:
        target += timedelta(days=1)
    secs = (target - now).total_seconds()
    log.info("Waiting until %s (%.1f h).", target.strftime("%a %H:%M"), secs / 3600)
    time.sleep(secs)


def start_focus(months: str, store: str) -> None:
    """Launch the sweep, focused, detached so this script can outlive it."""
    cmd = [_python(), "-u", str(ROOT / "sweep_forever.py"),
           "--focus", months, "--focus-max-age", "0", "--focus-max-tries", "1",
           "--batch", "25", "--store", store,
           "--log", str(ROOT / "sweep.log")]
    log.info("Starting: %s", " ".join(cmd[2:]))
    kwargs = {"cwd": str(ROOT)}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):     # Windows
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def wait_for_focus(store_path: str, *, max_wait_h: float = MAX_WAIT_H,
                   poll_s: float = POLL_S) -> bool:
    """Block until the focus finishes. True if it did, False on timeout."""
    started = time.time()
    last_left = None
    while time.time() - started < max_wait_h * 3600:
        time.sleep(poll_s)
        try:
            store = SweepStore.load(store_path)
        except Exception as exc:                            # noqa: BLE001
            log.warning("could not read the store (%s); still waiting", exc)
            continue
        if store.focus_done_logged:
            log.info("Focus complete after %.1f h.",
                     (time.time() - started) / 3600)
            return True
        if another_sweeper_running() is None:
            log.error("The sweep is no longer running, and the focus never "
                      "completed. Emailing what was collected anyway.")
            return False
        left = len(store.suspect)
        if left != last_left:
            last_left = left
        log.info("still running - %.1f h in, %d window(s) priced",
                 (time.time() - started) / 3600, store.windows_priced)
    log.error("Focus did not finish within %.0f h; emailing anyway.", max_wait_h)
    return False


def send_email() -> int:
    """Report whatever has been collected. Makes no requests to Google."""
    log.info("Sending the on-demand email.")
    done = subprocess.run([_python(), "-m", "tracker.cli", "--email-now"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300)
    for line in (done.stdout + done.stderr).splitlines():
        if line.strip():
            log.info("  %s", line.strip())
    return done.returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--at", metavar="HH:MM",
                   help="wait until this local time before starting")
    p.add_argument("--months", default="1,2,3",
                   help="focus months (default 1,2,3)")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--max-wait-hours", type=float, default=MAX_WAIT_H)
    p.add_argument("--log", default=str(ROOT / "sale_day.log"))
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    if args.log:
        fh = logging.FileHandler(args.log, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)

    if args.at:
        wait_until(args.at)

    log.info("=== sale day: focus on month(s) %s ===", args.months)
    stop_and_wait(args.store)
    start_focus(args.months, args.store)
    finished = wait_for_focus(args.store, max_wait_h=args.max_wait_hours)
    rc = send_email()
    log.info("=== done: focus %s, email exit %d. The sweep is still "
             "running, back on the ordinary rotation. ===",
             "completed" if finished else "did NOT complete", rc)
    return 0 if finished and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
