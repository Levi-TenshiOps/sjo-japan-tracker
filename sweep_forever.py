#!/usr/bin/env python3
"""Run the full-coverage sweep, indefinitely.

The scheduled tracker cannot cover the search space: Chrome prices ~120
windows a day against ~4,000, so a bargain on an unwatched date can sit
there for weeks. This process closes that hole by walking every window in
order, forever, and writing what it finds to `discoveries.json`, which the
scheduled run folds into the email.

    python sweep_forever.py                 # run until stopped
    python sweep_forever.py --watch         # live progress, leave it running
    python sweep_forever.py --status        # what has it found so far?
    python sweep_forever.py --readiness     # safe to raise the rate yet?
    python sweep_forever.py --once          # a single batch, then exit

`--watch`, `--status`, `--readiness` and `--coverage` only read. They are
safe to run beside the sweep and none of them touches Google.

Leave it running in its own terminal, or install it as a service. It is
safe to stop at any time: the cursor is saved after every batch, so it
resumes where it left off rather than starting the pass again.

On pacing: a Chrome launch measures ~6s, so at the default 8-second delay
a window costs ~14s and a 4,000-window pass takes about 15 hours.

Do not lower the delay to go faster, and do not query Google from anything
else while this runs. Measured 2026-08-23, a second process doing exactly
that halved nothing and took the hit rate from 87% to 24% - Google answered
with empty pages in 3-4s, and every one of those was a window whose fares
went unseen until the next pass. The sweep now detects that and backs off,
but the cheapest fix is not to provoke it.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import alarm as alarm_mod            # noqa: E402
from tracker import gate                          # noqa: E402
from tracker import config as config_mod          # noqa: E402
from tracker.browser import chrome_path           # noqa: E402
from tracker.preferences import Preferences, PreferencesError  # noqa: E402
from tracker.schedule import generate_windows     # noqa: E402
from tracker.sweeper import (                     # noqa: E402
    DEFAULT_STORE, FOCUS_MAX_TRIES, LAUNCH_SECONDS, RECHECK_EVERY,
    Discovery, SweepStore,
    coverage_report,
    focus_pending, queue_unverified, readiness_report, slower_rate_step,
    sweep_batch,
    sweep_order,
    unverified_windows, watch_lines,
)

log = logging.getLogger("sweep")
_stop = False

# A file the running sweep watches for, so it can be stopped cleanly from
# anywhere - another terminal, a script, or a session that no longer has the
# window it was launched from.
#
# Killing the process instead leaves the Google lock behind, and the next
# sweep has to break it: "Breaking a stale Google lock held by sweep (pid
# 14944)". That warning is harmless and reads exactly like a real problem,
# which on 2026-08-24 is precisely how the trip owner read it - twice.
STOP_FILE = "sweep.stop"


def stop_requested(path: str = STOP_FILE) -> bool:
    """Has somebody asked for a clean stop?"""
    try:
        return Path(path).exists()
    except OSError:
        return False


def request_stop(path: str = STOP_FILE) -> None:
    Path(path).write_text("stop requested\n", encoding="utf-8")


def clear_stop(path: str = STOP_FILE) -> None:
    """Never let a leftover file stop the next run before it starts."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# One sweeper at a time. `gate.py` stops two processes querying Google
# simultaneously, but nothing stopped two *sweepers* existing - and they
# would both hold the store in memory and write it per window, so the two
# cursors would overwrite each other and coverage would silently go
# backwards. Easy to do by accident: start it twice, or add a start-at-boot
# task while one is already running.
INSTANCE_LOCK = "sweep.pid"


def another_sweeper_running(path: str = INSTANCE_LOCK) -> int | None:
    """PID of a live sweeper other than this one, or None."""
    p = Path(path)
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    return pid if gate._alive(pid) else None


def claim_instance(path: str = INSTANCE_LOCK) -> None:
    Path(path).write_text(str(os.getpid()), encoding="utf-8")


def release_instance(path: str = INSTANCE_LOCK) -> None:
    p = Path(path)
    try:
        if p.exists() and p.read_text(encoding="utf-8").strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


#: How long `--stop` waits before giving up and saying so. A window takes
#: ~20s, but the sweep can also be queued behind a scheduled run's whole
#: Chrome phase, which is about four minutes.
STOP_TIMEOUT_S = 420.0


def stop_and_wait(store_path: str = DEFAULT_STORE, *,
                  timeout_s: float = STOP_TIMEOUT_S,
                  poll_s: float = 2.0,
                  sleep=time.sleep,
                  running=another_sweeper_running) -> int:
    """Ask the sweep to stop, then wait until it actually has.

    It used to write the flag and return immediately, which reads as
    success and is not: the sweep finishes its current window first, and
    can be queued behind a scheduled run's Chrome phase for four minutes.
    Anyone following the sale-day steps then starts the next command into
    a still-running sweep and gets "Another sweeper is already running".

    Running it with nothing running was worse - it printed the same
    hopeful message and left a `sweep.stop` file behind. Harmless, because
    startup clears it, but it told you a sweep had been asked to stop when
    there was none.

    Exit 0 when stopped, 1 on timeout - so a script can rely on it.
    """
    other = running()
    if other is None:
        clear_stop()            # never leave a flag that means nothing
        print("Nothing to stop: no sweep is running.")
        return 0

    request_stop()
    print(f"Stopping the sweep (pid {other}). It finishes the current "
          f"window, saves, and releases the Google lock first.")
    waited = 0.0
    while waited < timeout_s:
        sleep(poll_s)
        waited += poll_s
        if running() is None:
            clear_stop()
            where = ""
            try:
                st = SweepStore.load(store_path)
                where = f" The cursor is at {st.cursor:,}; it resumes there."
            except Exception:                       # noqa: BLE001
                pass
            print(f"Stopped after {waited:.0f}s. Safe to start a new "
                  f"one.{where}")
            return 0
        if waited % 30 < poll_s:
            print(f"   still finishing... ({waited:.0f}s)", flush=True)

    print(f"Still running after {timeout_s:.0f}s (pid {other}). It may be "
          f"waiting on the Google lock behind a scheduled run. The stop "
          f"request stands - re-run --stop to keep waiting.")
    return 1


def _handle_signal(signum, frame):      # noqa: ARG001
    global _stop
    _stop = True
    log.info("Stop requested; finishing the current window then saving.")


#: How long after a throttle a fresh start still refuses to run at the fast
#: default, and what it uses instead.
SAFE_START_AFTER_THROTTLE_H = 12.0


def safe_start_delay(store, wanted: float, *, asked: bool,
                     quiet_h: float = SAFE_START_AFTER_THROTTLE_H) -> tuple:
    """The delay to actually start at, and why.

    The in-process tripwire backs the rate off when Google refuses, but it
    dies with the process. So a machine that throttles at 03:00 and reboots
    at 06:00 would come straight back at the fast default, into an address
    still refusing - which is exactly the 2026-08-23 failure, where the
    Startup launcher re-armed `--delay 6` at every boot.

    An explicit `--delay` is always obeyed: someone typing it is present
    and can watch. Only the *default* is second-guessed.

    Returns (delay, reason) - reason is "" when nothing was changed.
    """
    if asked:
        return wanted, ""
    if getattr(store, "consecutive_rests", 0):
        return max(wanted, 40.0), (
            f"the last run was still backing off "
            f"(consecutive_rests={store.consecutive_rests})")
    last = getattr(store, "last_throttle", "") or ""
    if last:
        try:
            age = _age_hours_iso(last)
        except ValueError:
            return wanted, ""
        if age < quiet_h:
            return max(wanted, 40.0), (
                f"Google throttled {age:.1f} h ago and this is a fresh start, "
                f"not something you typed")
    return wanted, ""


def _age_hours_iso(stamp: str) -> float:
    return (datetime.now(timezone.utc)
            - datetime.fromisoformat(stamp)).total_seconds() / 3600.0


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep every window, forever.")
    p.add_argument("-c", "--config", default=config_mod.DEFAULT_CONFIG_PATH)
    p.add_argument("-p", "--preferences", default="preferences.json")
    p.add_argument("--store", default=DEFAULT_STORE)
    # Raised 90 -> 40 on 2026-08-25, after two clean days at 90 and with
    # every cause of the 2026-08-23 block fixed: the browser profile now
    # persists, `gate.py` stops two processes searching at once, and every
    # wait is jittered. Measured, not guessed: at a ~14s fetch, 40s is a
    # 54s cycle, ~1,600 requests a day. The delay that caused the block was
    # 6s, about 14,000 a day, so this is roughly a ninth of it.
    #
    # **Change the default here, never in the launcher.** A rate written
    # into a file that runs unattended at every boot outlives every later
    # fix to the default - `--delay 6` survived in the Startup launcher
    # long after the code had been made safe, and re-armed at every reboot.
    # Lowered 40 -> 5 on 2026-08-27, asked for so a reboot comes back at the
    # rate actually wanted rather than resetting to a cautious one. 5s ran
    # ~18 hours clean at ~4,600 requests/day before a restart ended it, with
    # throttle_events unchanged at 6 throughout.
    #
    # This is only safe because of `safe_start_delay` below. A fast default
    # that survives an unattended reboot is precisely the 2026-08-23 bug -
    # there the sweep came back at --delay 6 into an address that was
    # already refusing. The default is fast now, but a machine that comes
    # back up shortly after a throttle does not start fast.
    p.add_argument("--delay", type=float, default=5.0,
                   help="seconds between launches (default 5, ~4,600 req/day; "
                        "automatically slowed at startup if the store shows a "
                        "recent throttle)")
    p.add_argument("--batch", type=int, default=10,
                   help="windows priced before each save (default 10)")
    p.add_argument("--once", action="store_true", help="one batch, then exit")
    p.add_argument("--stop", action="store_true",
                   help="ask a running sweep to finish its window and exit "
                        "cleanly, then exit. Better than killing it: a killed "
                        "sweep leaves the Google lock behind.")
    p.add_argument("--stop-timeout", type=float, default=STOP_TIMEOUT_S,
                   metavar="SECONDS",
                   help=f"how long --stop waits for the sweep to actually "
                        f"exit (default {STOP_TIMEOUT_S:.0f})")
    p.add_argument("--recheck-unverified", action="store_true",
                   help="queue every walked window that produced no fare and "
                        "was not checked on a healthy connection, then exit")
    p.add_argument("--coverage", action="store_true",
                   help="how often each kind of window is revisited, and what "
                        "length of price drop that catches")
    p.add_argument("--focus", default=None, metavar="MONTHS",
                   help="finish these departure months before the rest, "
                        "e.g. --focus 1,2,3 for January then February then "
                        "March. 'none' clears it for this run. Redirects "
                        "effort; never raises the request rate.")
    p.add_argument("--focus-max-age", type=float, default=None,
                   metavar="HOURS",
                   help="treat a focus month's answer as stale once it is "
                        "older than this, so the focus re-prices instead of "
                        "only backfilling. Without it, --focus finishes the "
                        "moment every window has any trusted answer - which "
                        "on 2026-08-26 was all 1,089 January-March windows, "
                        "400 of them over a day old. Use it on a sale day: "
                        "--focus 1,2,3 --focus-max-age 6")
    p.add_argument("--focus-max-tries", type=int, default=None, metavar="N",
                   help="how many times one window may be priced during a "
                        "focus (default 3). Use 1 with --focus-max-age 0 to "
                        "re-price every window in the focus months exactly "
                        "once and then stop - the sale-day case, where even "
                        "a window checked an hour ago holds a pre-sale price.")
    p.add_argument("--watch", nargs="?", type=float, const=30.0,
                   default=None, metavar="SECONDS",
                   help="live progress, refreshing every SECONDS (default "
                        "30). Reads files only - safe to leave running "
                        "beside the sweep. Ctrl-C to stop.")
    p.add_argument("--readiness", action="store_true",
                   help="is it safe yet to raise the sweep rate or the "
                        "Chrome budget? reads files only, never Google")
    p.add_argument("--status", action="store_true",
                   help="print progress and findings, then exit")
    p.add_argument("--log", default="sweep.log",
                   help="append the run log here as well as the terminal "
                        "(default sweep.log; empty string disables)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = build_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    # The throttle warnings are the whole early-warning system, and until now
    # they only ever went to the terminal the sweep was launched from. Close
    # that window and they are gone - on 2026-08-23 the running sweep's
    # health was unreadable for exactly this reason, while sweep.log still
    # held a previous run's output and looked current. Append, with the date
    # in the file's format, so one file covers the whole history.
    if args.log and not (args.status or args.readiness or args.coverage
                         or args.watch is not None):
        try:
            fh = logging.FileHandler(args.log, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            logging.getLogger().addHandler(fh)
        except OSError as exc:
            log.warning("could not open %s for logging: %s", args.log, exc)

    try:
        prefs = Preferences.load(args.preferences)
    except PreferencesError as exc:
        log.error("%s", exc)
        return 2
    cfg = config_mod.load(args.config)

    windows = sweep_order(generate_windows(prefs, today=Date.today()))

    # --focus overrides the config for this run; "none" turns it off.
    focus_months = list(cfg.sweep_focus_months or [])
    if args.focus is not None:
        if args.focus.strip().lower() in ("none", "off", ""):
            focus_months = []
        else:
            try:
                focus_months = [int(x) for x in args.focus.split(",") if x.strip()]
            except ValueError:
                log.error("--focus wants month numbers, e.g. --focus 1,2,3")
                return 2
    bad = [m for m in focus_months if not 1 <= m <= 12]
    if bad:
        log.error("--focus month(s) out of range: %s", bad)
        return 2
    store = SweepStore.load(args.store)

    # Prune on the way in, not only at batch boundaries. `prune` runs after
    # `sweep_batch` returns, so a run stopped mid-batch never prunes at all -
    # and on 2026-08-24, after a day of restarting to pick up fixes, the
    # store held 459 findings against a MAX_ENTRIES of 400. Harmless in
    # itself, but the cap exists to bound the file and it was not binding.
    # Every command that only *reports* must leave the store alone. The
    # sweep holds it in memory and rewrites it per window, so a second
    # process writing it is the "two sweepers" hazard this project already
    # warns about - cursors overwrite each other and coverage silently goes
    # backwards. `--coverage` was missed when `--status` and `--readiness`
    # were excluded, so asking how complete the sweep was could perturb the
    # thing being asked about.
    read_only = (args.status or args.readiness or args.coverage
                 or args.watch is not None)
    if not read_only:
        dropped = store.prune()
        if dropped:
            # Persist it. Pruning in memory only would be lost the moment
            # the process is stopped before its first batch completes, which
            # is exactly the situation that let the store drift to 459.
            store.save(args.store)
            log.info("Pruned %d finding(s) on startup; %d remembered.",
                     dropped, len(store.found))

    # A restart after a gap judges the connection fresh. Without this a
    # sweep stopped while throttled comes straight back up in 4x backoff on
    # yesterday's evidence, and needs two hours of crawling to disprove it.
    if not read_only and store.forget_stale_health():
        log.info("Idle a while; forgetting the old connection-health "
                 "samples and judging this stretch fresh.")

    if args.recheck_unverified:
        pending = unverified_windows(windows, store)
        added = queue_unverified(windows, store)
        store.save(args.store)
        print(f"{len(pending):,} walked window(s) have no fare recorded and no "
              f"healthy check behind them.")
        print(f"{added:,} added to the re-check queue "
              f"({len(store.suspect):,} now queued).")
        cycle = (args.delay + 6.1) * RECHECK_EVERY
        print(f"At one re-check every {RECHECK_EVERY} launches, that is "
              f"~{len(store.suspect) * cycle / 86400:.1f} days of work "
              f"alongside the normal rotation.")
        return 0

    if args.stop:
        return stop_and_wait(args.store, timeout_s=args.stop_timeout)

    if args.watch is not None:
        every = max(float(args.watch), 5.0)
        started = None
        try:
            while True:
                snap = SweepStore.load(args.store)
                if started is None:
                    # (when, cursor, focus backlog, re-check queue). The
                    # last two are baselines: the cold cursor is frozen
                    # during a focus *and* during a post-pass drain, so the
                    # only honest rate is the one for the work being done.
                    started = (datetime.now(timezone.utc), snap.cursor,
                               len(focus_pending(
                                   windows, snap, focus_months,
                                   max_age_hours=args.focus_max_age,
                                   max_tries=(args.focus_max_tries
                                              or FOCUS_MAX_TRIES)))
                               if focus_months else 0,
                               len(snap.suspect))
                lines = watch_lines(windows, snap,
                                    threshold=prefs.good_price_usd,
                                    delay_s=(snap.delay_s or args.delay),
                                    started=started,
                                    focus_months=focus_months,
                                    focus_max_age_hours=args.focus_max_age,
                                    focus_max_tries=args.focus_max_tries)
                # Home the cursor and clear so the block refreshes in
                # place. Harmless where it is ignored: the block just
                # repeats instead.
                sys.stdout.write('\x1b[H\x1b[J')
                clock = datetime.now(timezone.utc).astimezone()
                print(f"  SJO -> Japan sweep   {clock:%H:%M:%S}"
                      f"   (refreshing every {every:.0f}s, "
                      f"Ctrl-C to stop)")
                print()
                for line in lines:
                    print(line)
                sys.stdout.flush()
                time.sleep(every)
        except KeyboardInterrupt:
            print()
            print("Stopped watching. The sweep itself is unaffected.")
        return 0

    if args.readiness:
        from tracker import alarm as _alarm, throttle as _throttle
        ready, lines = readiness_report(
            store,
            throttle_state=_throttle.ThrottleState.load(cfg.throttle_file),
            hours_since_email=_alarm.hours_since_last_email(cfg.state_file),
            delay_s=(store.delay_s or args.delay),
            hot_list_size=getattr(cfg, "hot_list_size", None),
        )
        print('\nSafe to raise the rate or the Chrome budget?\n')
        for line in lines:
            print(line)
        return 0 if ready else 1

    if args.coverage:
        for line in coverage_report(windows, store,
                                    threshold=prefs.good_price_usd,
                                    delay_s=(store.delay_s or args.delay)):
            print(line)
        return 0

    if args.status:
        print(store.progress(len(windows)))
        print(store.health())
        # What the hand-kept allow list is costing. These are fares that
        # were refused *only* because a hub has never been researched -
        # not US or Canada, which are refused for ever. Costa Rica has
        # visa-free Schengen access and the list is per-airport, so CDG is
        # on it and Orly is not.
        lost = sorted(store.rejected_unknown.items(),
                      key=lambda kv: int(kv[1].get("min", 10 ** 9)))
        if lost:
            print()
            print("Fares refused only for want of a researched hub "
                  "(cheapest first):")
            for code, rec in lost[:10]:
                print(f"  {code:5} seen {int(rec.get('n', 0)):4}x  "
                      f"cheapest ${int(rec.get('min', 0)):,}")
            print("  Nothing is allowed or blocked by this list; it is a "
                  "measurement.")
            print("  Add a hub to airports.HUBS only with a researched "
                  "tier and a test.")
        best = store.best(limit=15, threshold=None)
        if not best:
            print("No findings yet.")
        else:
            print(f"\nCheapest {len(best)} window(s) found:")
            for d in best:
                flag = "  <-- under threshold" if d.price_usd <= prefs.good_price_usd else ""
                print(f"  {d.describe()}{flag}")
        return 0

    # Zero windows is a configuration mistake, not a state to sit in. Every
    # named month can be outside the horizon - `included_months: [6]` with
    # an 8-month horizon starting in August - and `sweep_batch` then returns
    # immediately with nothing to price. The outer loop has no sleep of its
    # own, because the pacing lives per-window, so the process would spin at
    # full speed rewriting the store forever.
    if not windows:
        log.error("No travel windows to sweep. Check `included_months` and "
                  "`excluded_months` in %s: %s", args.preferences,
                  prefs.describe())
        missed = prefs.unreachable_months()
        if missed:
            from tracker.preferences import MONTH_NAMES
            log.error("These months are outside the %d-month horizon and "
                      "search nothing at all: %s", prefs.search_months,
                      ", ".join(MONTH_NAMES[m] for m in missed))
        return 2

    if chrome_path(cfg.chrome_path) is None:
        log.error("Chrome not found. Install it, or set chrome_path in %s",
                  args.config)
        return 2

    other = another_sweeper_running()
    if other is not None:
        log.error("Another sweeper is already running (pid %d). Two of them "
                  "would overwrite each other's cursor and coverage would go "
                  "backwards. Stop it first:  python sweep_forever.py --stop",
                  other)
        return 3
    claim_instance()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # A stop file left over from the previous run must not stop this one
    # before it prices a single window.
    clear_stop()

    def wants_stop() -> bool:
        return _stop or stop_requested()

    # Measured on this machine: a Chrome launch that renders a real
    # result page takes about 6s, not the 13 this once assumed - which
    # made the banner promise 21 hours for a pass that takes 13.5.
    cycle = args.delay + 6.1
    log.info("Sweeping %d window(s) to %s, %.0fs apart "
             "(~%.1f h per full pass). Ctrl-C to stop.",
             len(windows), cfg.chrome_destination, args.delay,
             len(windows) * cycle / 3600.0)
    log.info("Resuming at %s", store.progress(len(windows)))

    alarm_cfg = alarm_mod.AlarmConfig.from_config(cfg, prefs)
    if not alarm_cfg.usable:
        log.warning("No SMTP configured, so you will NOT be emailed if Google "
                    "starts blocking. Run setup_email.py to fix that.")
    else:
        log.info("Throttle alarms will be emailed to %s", alarm_cfg.to_addr)
    if focus_months:
        from tracker.preferences import MONTH_NAMES as _NAMES
        names = ", ".join(_NAMES[m] for m in focus_months if m in _NAMES)
        # A focus month with no windows at all searches nothing, and looks
        # exactly like a focus that finished instantly. Same trap as a
        # named month the horizon cannot reach, which this project already
        # learned to shout about rather than swallow.
        have = {w.depart.month for w in windows}
        missing = [m for m in focus_months if m not in have]
        if missing:
            log.warning("FOCUS: month(s) %s are not in the searched window "
                        "list at all, so there is nothing to focus on there. "
                        "Check included_months in config.yaml.",
                        ", ".join(_NAMES.get(m, str(m)) for m in missing))
        if not set(focus_months) & have:
            log.warning("FOCUS: none of the requested months are searched; "
                        "carrying on with the ordinary rotation.")
            focus_months = []
        else:
            # Start the attempt counters fresh. They exist so a focus can
            # end - a window that keeps answering blank is given a fair
            # number of tries and then left to the ordinary queue - but
            # they must not make the *next* focus a no-op. Measured
            # 2026-08-24: without this a second focus on the same months
            # spent zero launches on them, silently, which is precisely the
            # "run it again on a sale day" case it was built for.
            #
            # Reset at startup rather than on completion: clearing them the
            # moment a focus finishes would let it immediately restart and
            # loop for ever, and restarting the sweep is how a focus gets
            # asked for again anyway.
            if store.focus_tries or store.focus_done_logged:
                log.info("FOCUS: clearing %d attempt counter(s) from a "
                         "previous focus.", len(store.focus_tries))
                store.focus_tries = {}
                store.focus_done_logged = False
                store.save(args.store)
            pend = focus_pending(windows, store, focus_months,
                                 max_age_hours=args.focus_max_age,
                                 max_tries=(args.focus_max_tries
                                            or FOCUS_MAX_TRIES))
            # Say which of the two jobs this is. Under --focus-max-age the
            # windows all *have* answers - they are stale, not missing - and
            # "1089 window(s) still without a trusted answer" read at 6am on
            # a sale day looks like the store has been wiped.
            tries = args.focus_max_tries or FOCUS_MAX_TRIES
            if args.focus_max_age is None:
                what = "still without a trusted answer"
            elif args.focus_max_age <= 0:
                what = f"to re-price, every one of them, at most {tries}x each"
            else:
                what = (f"answered more than {args.focus_max_age:g} h ago, "
                        f"at most {tries}x each")
            log.info("FOCUS: finishing %s before the rest - %d window(s) %s. "
                     "The cold rotation is paused at %d and resumes after.",
                     names, len(pend), what, store.cursor)
            # An age with the default try count does not terminate in one
            # sweep: windows go stale again while the focus runs, so it
            # keeps finding work until each has had every try. Say so, since
            # the whole point on a sale day is knowing when it is done.
            if args.focus_max_age is not None and tries > 1:
                log.warning("FOCUS: with --focus-max-age %g and %d tries this "
                            "will keep re-pricing as windows go stale again. "
                            "Add --focus-max-tries 1 for a single pass that "
                            "stops.", args.focus_max_age, tries)

    def raise_alarm(kind: str, facts: dict) -> None:
        """Best effort. A failed alarm must never stop the sweep."""
        try:
            if kind == "blocked":
                content = alarm_mod.blocked_email(**facts)
            elif kind == "recovered":
                content = alarm_mod.recovered_email(**facts)
            elif kind == "silent":
                content = alarm_mod.silent_email(**facts)
            else:
                return
            alarm_mod.send(content, alarm_cfg)
        except Exception as exc:          # noqa: BLE001
            log.warning("alarm failed (%s); sweeping on", exc)

    def announce(d: Discovery) -> None:
        if d.price_usd <= prefs.good_price_usd:
            log.info("*** FOUND %s ***", d.describe())
        else:
            log.debug("new best for window: %s", d.describe())

    # --- the rate tripwire -------------------------------------------------
    # Raising the rate is an experiment, and an experiment needs a stop
    # condition that does not depend on somebody watching. A rest is taken
    # only after the sweep has been throttled for a sustained stretch, so a
    # new one is the least ambiguous "Google is refusing" signal the store
    # carries - much stronger than a throttle *detection*, which this
    # project has had several false ones of.
    #
    # Deliberately one-way for the life of the process. Speeding back up after
    # a quiet stretch would be reading a cleared health sample as an all-clear
    # - the same mistake as the recovery email that described the wrong
    # throttle.
    current_delay, why_slower = safe_start_delay(
        store, args.delay, asked="--delay" in sys.argv)
    if why_slower:
        log.warning("Starting at --delay %.0fs rather than the %.0fs default: "
                    "%s. Pass --delay %.0f explicitly to override.",
                    current_delay, args.delay, why_slower, args.delay)
    # `rests_total`, never `consecutive_rests`: the latter is cleared on
    # recovery and by forget_stale_health, so after one rest and one recovery
    # it returns to 0 and the next rest reads as 1 - equal to what was
    # already seen, not greater. The tripwire fired once and then never
    # again, which is precisely the case it exists for.
    rests_seen = store.rests_total

    # A restart silently drops back to the default rate, and on a sale day
    # that is the difference between a focus finishing and not: measured
    # 2026-08-26, 880 stale windows take 5.8 h at --delay 5 and 16.5 h at
    # the 40s default, with nothing on screen to say which you got.
    #
    # The rate is deliberately NOT persisted. The Startup launcher passes no
    # --delay precisely so the default is what survives a reboot - a fast
    # rate written into an unattended file is what threw this address into a
    # day-long throttle on 2026-08-23. So: keep the safe default, and say
    # plainly that it differs from last time.
    last = getattr(store, "delay_s", 0.0) or 0.0
    if last and abs(last - current_delay) > 0.01:
        log.warning("NOTE: the previous run was at --delay %.0fs, this one is "
                    "at %.0fs (%s). A full pass is %.1f d instead of %.1f d. "
                    "Pass --delay %.0f to continue at the old rate.",
                    last, current_delay,
                    "you asked for it" if "--delay" in sys.argv
                    else "the default, because --delay was not given",
                    len(windows) * (current_delay + LAUNCH_SECONDS) / 86400,
                    len(windows) * (last + LAUNCH_SECONDS) / 86400,
                    last)

    _backoff = slower_rate_step(current_delay)
    if _backoff is not None:
        log.info("Rate tripwire armed: at --delay %.0fs (~%.0f req/day), "
                 "backing off to %.0fs if Google starts refusing.",
                 current_delay, 86400 / (current_delay + LAUNCH_SECONDS),
                 _backoff)

    while True:
        priced = 0
        store.delay_s = current_delay
        try:
            priced = sweep_batch(
                windows, store,
                origin=cfg.origins[0], destination=cfg.chrome_destination,
                max_stops=cfg.max_stops, batch=args.batch,
                max_total_hours=cfg.max_total_hours,
                focus_months=focus_months,
                focus_max_age_hours=args.focus_max_age,
                focus_max_tries=args.focus_max_tries,
                chrome_override=cfg.chrome_path, timeout_s=cfg.chrome_timeout_s,
                budget_ms=cfg.chrome_budget_ms, delay_s=current_delay,
                on_find=announce, history_csv=cfg.sweep_history_csv,
                lock_path=cfg.google_lock,
                hot_threshold=prefs.good_price_usd,
                save_to=args.store,
                should_stop=wants_stop,
                on_alarm=raise_alarm,
            )
        except Exception as exc:            # noqa: BLE001 - must not die
            log.warning("batch failed (%s); pausing 60s", exc)
            time.sleep(60)

        if store.rests_total > rests_seen:
            rests_seen = store.rests_total
            backed = slower_rate_step(current_delay)
            if backed is not None:
                log.warning("TRIPWIRE: rest #%d at --delay %.0fs. Backing the "
                            "rate off to %.0fs (%.0f -> %.0f req/day) for the "
                            "rest of this process. Restart with an explicit "
                            "--delay to override.",
                            store.rests_total, current_delay, backed,
                            86400 / (current_delay + LAUNCH_SECONDS),
                            86400 / (backed + LAUNCH_SECONDS))
                current_delay = backed
            else:
                log.warning("TRIPWIRE: rest #%d, but --delay %.0fs is already "
                            "the slowest rung; not backing off further.",
                            store.rests_total, current_delay)

        dropped = store.prune()

        # Watch the *other* process. A scheduled run that crashes cannot
        # report it - on 2026-08-24 one malformed CSV line killed every run
        # four hours before its email phase, while this sweep carried on
        # looking perfectly healthy. The sweep is the only thing always
        # running, so it is the only thing that can notice.
        silent = alarm_mod.hours_since_last_email(cfg.state_file)
        if silent is not None and silent > alarm_mod.SILENCE_HOURS:
            if not store.silence_alarm_sent:
                store.silence_alarm_sent = True
                log.warning("No alert email for %.0f h - the scheduled runs "
                            "have stopped delivering. Emailing a warning.",
                            silent)
                raise_alarm("silent", {"hours": silent,
                                       "threshold": alarm_mod.SILENCE_HOURS})
        elif silent is not None and store.silence_alarm_sent:
            log.info("Alert emails are flowing again (last one %.1f h ago).",
                     silent)
            store.silence_alarm_sent = False

        store.save(args.store)
        under = store.best(limit=3, threshold=prefs.good_price_usd)
        if store.suspect or store.throttled_since:
            log.info("Health: %s", store.health())
        log.info("%s%s%s", store.progress(len(windows)),
                 f", {dropped} pruned" if dropped else "",
                 f", cheapest under threshold ${under[0].price_usd:,}" if under else "")

        if args.once or wants_stop():
            clear_stop()
            release_instance()
            log.info("Stopped cleanly. %s", store.progress(len(windows)))
            return 0

        if not priced:
            # Nothing was priced and nothing raised. Do not come straight
            # back round: the delays live inside sweep_batch, so an empty
            # batch means this loop has no pacing at all.
            log.warning("A batch priced no windows; pausing 60s rather than "
                        "spinning.")
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
