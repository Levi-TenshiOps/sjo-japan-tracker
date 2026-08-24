#!/usr/bin/env python3
"""Run the full-coverage sweep, indefinitely.

The scheduled tracker cannot cover the search space: Chrome prices ~120
windows a day against ~4,000, so a bargain on an unwatched date can sit
there for weeks. This process closes that hole by walking every window in
order, forever, and writing what it finds to `discoveries.json`, which the
scheduled run folds into the email.

    python sweep_forever.py                 # run until stopped
    python sweep_forever.py --delay 12      # gentler on the IP
    python sweep_forever.py --status        # what has it found so far?
    python sweep_forever.py --once          # a single batch, then exit

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import alarm as alarm_mod            # noqa: E402
from tracker import gate                          # noqa: E402
from tracker import config as config_mod          # noqa: E402
from tracker.browser import chrome_path           # noqa: E402
from tracker.preferences import Preferences, PreferencesError  # noqa: E402
from tracker.schedule import generate_windows     # noqa: E402
from tracker.sweeper import (                     # noqa: E402
    DEFAULT_STORE, RECHECK_EVERY, Discovery, SweepStore, coverage_report,
    queue_unverified, sweep_batch, sweep_order, unverified_windows,
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


def _handle_signal(signum, frame):      # noqa: ARG001
    global _stop
    _stop = True
    log.info("Stop requested; finishing the current window then saving.")


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep every window, forever.")
    p.add_argument("-c", "--config", default=config_mod.DEFAULT_CONFIG_PATH)
    p.add_argument("-p", "--preferences", default="preferences.json")
    p.add_argument("--store", default=DEFAULT_STORE)
    # 90s is ~900 requests a day. The 6s this used before was ~5,800, which
    # is what got the address throttled.
    p.add_argument("--delay", type=float, default=90.0,
                   help="seconds between launches (default 90, ~900 req/day)")
    p.add_argument("--batch", type=int, default=10,
                   help="windows priced before each save (default 10)")
    p.add_argument("--once", action="store_true", help="one batch, then exit")
    p.add_argument("--stop", action="store_true",
                   help="ask a running sweep to finish its window and exit "
                        "cleanly, then exit. Better than killing it: a killed "
                        "sweep leaves the Google lock behind.")
    p.add_argument("--recheck-unverified", action="store_true",
                   help="queue every walked window that produced no fare and "
                        "was not checked on a healthy connection, then exit")
    p.add_argument("--coverage", action="store_true",
                   help="how often each kind of window is revisited, and what "
                        "length of price drop that catches")
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
    if args.log and not args.status:
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
    store = SweepStore.load(args.store)

    # Prune on the way in, not only at batch boundaries. `prune` runs after
    # `sweep_batch` returns, so a run stopped mid-batch never prunes at all -
    # and on 2026-08-24, after a day of restarting to pick up fixes, the
    # store held 459 findings against a MAX_ENTRIES of 400. Harmless in
    # itself, but the cap exists to bound the file and it was not binding.
    if not args.status:
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
    if not args.status and store.forget_stale_health():
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
        request_stop()
        print("Stop requested. The running sweep will finish its current "
              "window, save, release the Google lock and exit - usually "
              "within a couple of minutes.")
        print("It is safe to start a new one after that; the cursor resumes.")
        return 0

    if args.coverage:
        for line in coverage_report(windows, store,
                                    threshold=prefs.good_price_usd,
                                    delay_s=args.delay):
            print(line)
        return 0

    if args.status:
        print(store.progress(len(windows)))
        print(store.health())
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

    while True:
        priced = 0
        try:
            priced = sweep_batch(
                windows, store,
                origin=cfg.origins[0], destination=cfg.chrome_destination,
                max_stops=cfg.max_stops, batch=args.batch,
                max_total_hours=cfg.max_total_hours,
                chrome_override=cfg.chrome_path, timeout_s=cfg.chrome_timeout_s,
                budget_ms=cfg.chrome_budget_ms, delay_s=args.delay,
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
