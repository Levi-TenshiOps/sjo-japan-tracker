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
import signal
import sys
import time
from datetime import date as Date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import config as config_mod          # noqa: E402
from tracker.browser import chrome_path           # noqa: E402
from tracker.preferences import Preferences, PreferencesError  # noqa: E402
from tracker.schedule import generate_windows     # noqa: E402
from tracker.sweeper import (                     # noqa: E402
    DEFAULT_STORE, Discovery, SweepStore, sweep_batch, sweep_order,
)

log = logging.getLogger("sweep")
_stop = False


def _handle_signal(signum, frame):      # noqa: ARG001
    global _stop
    _stop = True
    log.info("Stop requested; finishing the current window then saving.")


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep every window, forever.")
    p.add_argument("-c", "--config", default=config_mod.DEFAULT_CONFIG_PATH)
    p.add_argument("-p", "--preferences", default="preferences.json")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--delay", type=float, default=90.0,
                   help="seconds between launches (default 90). At 90s
the sweep makes ~900 requests a day; the 6s it used before
made ~5,800, which is what got the address throttled.")
    p.add_argument("--batch", type=int, default=10,
                   help="windows priced before each save (default 10)")
    p.add_argument("--once", action="store_true", help="one batch, then exit")
    p.add_argument("--status", action="store_true",
                   help="print progress and findings, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = build_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    try:
        prefs = Preferences.load(args.preferences)
    except PreferencesError as exc:
        log.error("%s", exc)
        return 2
    cfg = config_mod.load(args.config)

    windows = sweep_order(generate_windows(prefs, today=Date.today()))
    store = SweepStore.load(args.store)

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

    if chrome_path(cfg.chrome_path) is None:
        log.error("Chrome not found. Install it, or set chrome_path in %s",
                  args.config)
        return 2

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Measured on this machine: a Chrome launch that renders a real
    # result page takes about 6s, not the 13 this once assumed - which
    # made the banner promise 21 hours for a pass that takes 13.5.
    cycle = args.delay + 6.1
    log.info("Sweeping %d window(s) to %s, %.0fs apart "
             "(~%.1f h per full pass). Ctrl-C to stop.",
             len(windows), cfg.chrome_destination, args.delay,
             len(windows) * cycle / 3600.0)
    log.info("Resuming at %s", store.progress(len(windows)))

    def announce(d: Discovery) -> None:
        if d.price_usd <= prefs.good_price_usd:
            log.info("*** FOUND %s ***", d.describe())
        else:
            log.debug("new best for window: %s", d.describe())

    while True:
        try:
            sweep_batch(
                windows, store,
                origin=cfg.origins[0], destination=cfg.chrome_destination,
                max_stops=cfg.max_stops, batch=args.batch,
                chrome_override=cfg.chrome_path, timeout_s=cfg.chrome_timeout_s,
                budget_ms=cfg.chrome_budget_ms, delay_s=args.delay,
                on_find=announce, history_csv=cfg.sweep_history_csv,
                lock_path=cfg.google_lock,
                hot_threshold=prefs.good_price_usd,
            )
        except Exception as exc:            # noqa: BLE001 - must not die
            log.warning("batch failed (%s); pausing 60s", exc)
            time.sleep(60)

        dropped = store.prune()
        store.save(args.store)
        under = store.best(limit=3, threshold=prefs.good_price_usd)
        if store.suspect or store.throttled_since:
            log.info("Health: %s", store.health())
        log.info("%s%s%s", store.progress(len(windows)),
                 f", {dropped} pruned" if dropped else "",
                 f", cheapest under threshold ${under[0].price_usd:,}" if under else "")

        if args.once or _stop:
            log.info("Stopped. %s", store.progress(len(windows)))
            return 0


if __name__ == "__main__":
    sys.exit(main())
