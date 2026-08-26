"""Entry point: plan a small slice, search, filter, classify, log, maybe email."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import (
    alarm as alarm_mod, alerts, config as config_mod, email_render, gate,
    history, monthly, notify, pricing, ranking, schedule, sweeper, throttle,
    verify as verify_mod,
)
from .itinerary import Itinerary, dedupe, format_price, partition
from .preferences import Preferences, PreferencesError
from .search import Searcher, fetch_text_query, plan_broad, plan_hub_sweep

CR_TZ = ZoneInfo("America/Costa_Rica")

# How long the background sweep may go without pricing a window
# before a scheduled run says so. It walks one window per ~96s, so an
# hour of silence already means something is wrong; three is well
# past any throttle rest, which tops out at one hour.
SWEEP_IDLE_HOURS = 3.0
log = logging.getLogger("tracker")


def _setup_logging(verbose: bool, log_file: str = "") -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # The scheduled task runs headless, so without this the only record of a
    # run is its exit code. Checked 2026-08-23: six tasks installed, one had
    # fired, and there was no way to see what it did or whether the email it
    # should have sent was actually sent. Append with the date, so one file
    # is the whole history rather than the last run only.
    if not log_file:
        return
    try:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        log.warning("could not open %s for logging: %s", log_file, exc)
        return
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)


def _read_rows(path: str) -> list[dict]:
    """The same tolerance `history.read_prices` needs, for the same reason.

    `price_history.csv` is written by the run itself, so it is not in the
    race that killed the 09:03 run on 2026-08-24 - but a run killed
    mid-write leaves a torn tail *permanently*, and this project has
    already lost a machine to a power cut. A torn tail here would crash
    every subsequent run for ever, which is strictly worse than the race.
    """
    p = Path(path)
    if not p.exists():
        return []
    return list(history._rows(p))


def _throttle_sample(searcher: Searcher, found) -> tuple[int, int]:
    """(empty requests, total requests) the throttle should judge on.

    A destination Google simply will not build routes for looks exactly like
    a throttled connection: every request comes back empty. The difference is
    that a block takes *everything* down with it. So when any destination is
    still returning fares, only those destinations are used as evidence -
    they are the ones whose emptiness would actually mean something. When
    nothing at all came back, every request counts, which is the case a real
    block produces.
    """
    producing = {i.destination for i in found}
    if not producing:
        return searcher.barren_requests, searcher.requests_made
    empty = sum(searcher.barren_by_destination.get(d, 0) for d in producing)
    total = sum(searcher.requests_by_destination.get(d, 0) for d in producing)
    return empty, total or searcher.requests_made


def wide_net_months(prefs, today=None) -> list[tuple[str, int]]:
    """(label, year) the wide net should ask about: what is actually searched.

    Driving this from the raw horizon instead was a real bug, found
    2026-08-23. After September and April were excluded the net kept
    querying them - six wasted requests a run - and worse, a hint for an
    excluded month went onto the *front* of the hot list, spent Chrome
    budget verifying it, and could have put a fare in the email for a month
    the trip owner had explicitly ruled out.
    """
    return [(f"{monthly.MONTH_FULL[m]} {y}", y)
            for y, m in prefs.searched_months(today)]


def _rotate(items: list[str], by: int) -> list[str]:
    """items rotated left by `by`, so a truncated sweep still covers them all."""
    if not items:
        return []
    i = by % len(items)
    return [*items[i:], *items[:i]]


def collect(cfg, plan, searcher: Searcher, probe_destinations=()):
    """This slice's searches: (accepted, rejected, errors, empties, bands)."""
    pairs = plan.date_pairs()
    # The HTTP grid cannot see a stay longer than ~30 nights. That is not a
    # throttle and not a fare rule: the server-rendered HTML this path reads
    # simply carries no prices past that point, while the same URL in a
    # browser shows them - which is how a $1,390 32-night fare was nearly
    # excluded for good in 2026-08.
    #
    # Measured across all of `price_history.csv`: 509 grid fares at 30
    # nights or fewer, and **zero** at 31 or more. So every such request is
    # spent to be told nothing, and it is worse than merely wasted - those
    # empties feed `throttle.py`, which cuts the grid's budget, which is
    # already floored at 8. A structural blind spot was being read as a bad
    # connection and answered by making the grid smaller.
    #
    # On 2026-08-24 the rotation walked onto 31-36 night stays and the empty
    # rate stepped from a stable 25% to 75% for three consecutive runs.
    # Every empty window was 31 nights or longer; not one was under.
    #
    # Chrome still prices these windows, and the sweep still walks them.
    if cfg.http_max_nights:
        usable = [(a, b) for a, b in pairs
                  if b is None or (b - a).days <= cfg.http_max_nights]
        skipped = len(pairs) - len(usable)
        if skipped and usable:
            log.info("Grid: skipping %d window(s) over %d nights - the HTTP "
                     "path cannot see them; Chrome covers those",
                     skipped, cfg.http_max_nights)
            pairs = usable
    if not pairs:
        return [], [], ["no travel windows left to search"], (0, 0), None

    found: list[Itinerary] = []
    errors: list[str] = []
    bands_seen: list = []

    queries = plan_broad(
        origins=cfg.origins,
        destinations=plan.destinations,
        date_pairs=pairs,
        max_stops=cfg.max_stops,
    )
    log.info("Scanning: %s", plan.describe())

    for outcome in searcher.run_all(queries):
        if outcome.error:
            errors.append(f"{outcome.query.label}: {outcome.error}")
        if outcome.google_bands is not None:
            bands_seen.append(outcome.google_bands)
        found.extend(outcome.itineraries)

    # Destinations on probation get one request on the nearest window, just
    # enough to notice if Google starts building routes there again.
    if probe_destinations and not searcher.budget_exhausted:
        probe_queries = plan_broad(
            origins=cfg.origins[:1],
            destinations=list(probe_destinations),
            date_pairs=pairs[:1],
            max_stops=cfg.max_stops,
        )
        for outcome in searcher.run_all(probe_queries):
            if outcome.google_bands is not None:
                bands_seen.append(outcome.google_bands)
            found.extend(outcome.itineraries)

    # Whatever the window plan deliberately left unspent goes on forcing a
    # price for each visa-free hub on the single best window, so routings
    # Google buries still surface. The hub list is rotated so that over a
    # few runs every hub gets swept rather than only the first handful.
    if cfg.deep_hub_sweep and found and not searcher.budget_exhausted:
        cheapest = min(found, key=lambda i: i.price_usd)
        hubs = _rotate(cfg.hubs, plan.slice_index * max(cfg.hub_sweep_requests, 1))
        hub_queries = plan_hub_sweep(
            origins=cfg.origins,
            destinations=[cheapest.destination],
            date_pairs=[(cheapest.outbound_date, cheapest.return_date)],
            hubs=hubs,
            max_stops=cfg.max_stops,
        )
        for outcome in searcher.run_all(hub_queries):
            if outcome.google_bands is not None:
                bands_seen.append(outcome.google_bands)
            found.extend(outcome.itineraries)

    empties, judged = _throttle_sample(searcher, found)

    accepted, rejected = partition(
        found,
        max_total_hours=cfg.max_total_hours,
        min_layover_min=cfg.min_layover_min,
    )
    return (dedupe(accepted), rejected, errors, (judged, empties),
            pricing.median_bands(bands_seen))


# How dark a run has to go before the trip owner is emailed about it.
# Deliberately conservative: they were woken by a false alarm on
# 2026-08-24, and an alarm nobody trusts is worse than no alarm.
BLOCKED_MIN_CHROME = 3       # too few launches to conclude anything
BLOCKED_GRID_RATE = 0.5      # half the grid empty as well


def _raise_block_alarm(cfg, prefs, throttle_state, *, chrome_stats: dict,
                       grid_requests: int, grid_empty: int, verified) -> None:
    """Email the trip owner when a run gets nothing, and when it recovers."""
    blocked_now = run_looks_blocked(
        chrome_attempts=chrome_stats.get("attempts", 0),
        chrome_blank=chrome_stats.get("blank", 0),
        grid_requests=grid_requests, grid_empty=grid_empty,
    )
    if blocked_now == bool(throttle_state.blocked_alarm_sent):
        return                              # nothing has changed; stay quiet
    alarm_cfg = alarm_mod.AlarmConfig.from_config(cfg, prefs)
    when = datetime.now(CR_TZ).strftime("%H:%M")
    if blocked_now:
        log.warning("Every channel came back empty; emailing the alarm")
        rate = 100.0 * grid_empty / grid_requests if grid_requests else 0.0
        _send_alarm(alarm_mod.run_blocked_email(
            chrome_blank=chrome_stats.get("blank", 0),
            chrome_attempts=chrome_stats.get("attempts", 0),
            grid_rate=rate, when=when), alarm_cfg)
    else:
        log.info("Google is answering again; emailing the all-clear")
        _send_alarm(alarm_mod.run_recovered_email(
            when=when,
            cheapest=(format_price(verified[0].price_usd) if verified
                      else "none yet")), alarm_cfg)
    throttle_state.blocked_alarm_sent = blocked_now
    throttle_state.save(cfg.throttle_file)


def run_looks_blocked(*, chrome_attempts: int, chrome_blank: int,
                      grid_requests: int, grid_empty: int) -> bool:
    """True when every channel this run tried came back with nothing.

    The test is deliberately "both channels, same run" rather than either
    one alone. A single channel going quiet is ordinary - the HTTP grid is
    empty on most windows by design, and a date really can have no fares.
    Both going quiet at once, on windows already known to hold fares, is
    Google refusing to answer.

    Chrome blankness is counted on the *page*, not on the visa filter. A
    window that returned fourteen US-transit fares and kept none of them is
    Google answering perfectly well; counting that as a blank is exactly
    the bug that produced a 70% false alarm on 2026-08-24.
    """
    if chrome_attempts < BLOCKED_MIN_CHROME or chrome_blank < chrome_attempts:
        return False
    if grid_requests <= 0:
        # Chrome is the better witness, but on its own it is one process
        # and one profile; a corrupt Chrome profile would look identical.
        return False
    return grid_empty / grid_requests >= BLOCKED_GRID_RATE


def _recently_swept(cfg) -> set[str]:
    """Window keys the background sweep has priced recently enough to trust.

    A missing or unreadable store simply means the sweep is not running, in
    which case nothing is skipped and the run behaves exactly as before.
    """
    if cfg.chrome_skip_if_swept_hours <= 0:
        return set()
    try:
        store = sweeper.SweepStore.load(cfg.sweep_store)
        return {d.key for d in store.best(
            limit=10 ** 6, max_age_hours=cfg.chrome_skip_if_swept_hours)}
    except Exception as exc:                # noqa: BLE001
        log.debug("sweep store unreadable (%s); verifying everything", exc)
        return set()


# Result rows the parser could not read. Normally zero; anything material
# means Google's markup has moved and fares are being dropped unread.
PARSER_ALARM_ROWS = 25


def _watch_the_sweep(cfg, prefs, throttle_state, *, store, idle) -> None:
    """Email when the sweep stops, or when results stop being readable.

    The scheduled runs are the only thing that can report either. The sweep
    cannot announce its own death, and a parser that has stopped
    understanding the page produces no error at all - just fewer fares,
    which is indistinguishable from a quiet market.

    Both are once-only and clear themselves, so six runs a day cannot send
    six copies.
    """
    alarm_cfg = alarm_mod.AlarmConfig.from_config(cfg, prefs)
    stopped = idle is not None and idle > SWEEP_IDLE_HOURS
    if stopped and not throttle_state.sweep_idle_alarm_sent:
        log.warning("Emailing: the background sweep has stopped")
        _send_alarm(alarm_mod.sweep_stopped_email(
            hours=idle, cursor=store.progress(0).split(",")[0],
            pending=len(store.suspect)), alarm_cfg)
        throttle_state.sweep_idle_alarm_sent = True
        throttle_state.save(cfg.throttle_file)
    elif not stopped and throttle_state.sweep_idle_alarm_sent:
        log.info("The background sweep is running again")
        throttle_state.sweep_idle_alarm_sent = False
        throttle_state.save(cfg.throttle_file)

    missed = int(getattr(store, "rows_missed_by_parser", 0) or 0)
    if missed >= PARSER_ALARM_ROWS and not throttle_state.parser_alarm_sent:
        log.warning("Emailing: %d result row(s) could not be parsed", missed)
        _send_alarm(alarm_mod.parser_broken_email(
            missed=missed, windows=store.windows_priced), alarm_cfg)
        throttle_state.parser_alarm_sent = True
        throttle_state.save(cfg.throttle_file)


def _send_alarm(content, alarm_cfg) -> None:
    """Best effort. A failed alarm must never cost the trip owner an email."""
    if not alarm_cfg.usable:
        log.warning("No SMTP configured, so the block alarm could not be "
                    "sent. Run setup_email.py to fix that.")
        return
    try:
        alarm_mod.send(content, alarm_cfg)
    except Exception as exc:                # noqa: BLE001
        log.warning("alarm failed (%s); continuing with the run", exc)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="track_prices",
        description="Track visa-free San Jose to Japan fares.",
    )
    parser.add_argument("-c", "--config", default=config_mod.DEFAULT_CONFIG_PATH)
    parser.add_argument("-p", "--preferences", default="preferences.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except actually send")
    parser.add_argument("--save-preview", metavar="PATH",
                        help="write the rendered HTML email to a file")
    parser.add_argument("--budget", type=int, default=None,
                        help="override this run's request budget")
    parser.add_argument("--runs-per-day", type=int, default=4,
                        help="used for the throttle and coverage report")
    parser.add_argument("--status", action="store_true",
                        help="print settings and coverage, then exit")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--log", default="tracker.log",
                        help="append the run log here as well as the terminal "
                             "(default tracker.log; empty string disables)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose, "" if args.status else args.log)

    try:
        prefs = Preferences.load(args.preferences)
    except PreferencesError as exc:
        log.error("%s", exc)
        return 2
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        log.error("Config problem: %s", exc)
        return 2

    # Preferences win over config for anything the wizard asked about.
    cfg.alert_email = cfg.alert_email or prefs.alert_email
    cfg.destinations = prefs.destinations
    cfg.good_price_usd = prefs.good_price_usd
    cfg.great_price_usd = prefs.great_price_usd
    cfg.hub_tier = prefs.hub_tier

    throttle_state = throttle.ThrottleState.load(cfg.throttle_file)
    rotation = schedule.RotationState.load(cfg.rotation_file)
    budget = args.budget or throttle_state.budget
    # `max_requests_per_run` described itself in config.yaml as the ceiling
    # and was read by nothing at all - the real ceiling was throttle.py's
    # hardcoded MAX_BUDGET of 40. Lowering it in the config to calm a
    # throttle would have had no effect whatsoever, which is the worst kind
    # of setting to ship: one that looks like a lever and is painted on.
    if cfg.max_requests_per_run:
        budget = min(budget, cfg.max_requests_per_run)

    # Read the sweep store before planning: if it is feeding us, the grid's
    # coverage role is redundant and its budget can be cut to what the email
    # table needs. If the sweep is down, the grid is the only thing standing
    # between the trip owner and no email at all, so it keeps its full budget.
    try:
        _store = sweeper.SweepStore.load(cfg.sweep_store)
        _swept = _store.best(limit=200, max_age_hours=cfg.sweep_max_age_hours)
    except Exception:                       # noqa: BLE001
        _swept = []
    sweep_is_healthy = len(_swept) >= cfg.swept_enough
    if sweep_is_healthy:
        budget = min(budget, cfg.grid_budget_when_swept)
        log.info("Sweep has %d fresh finding(s); trimming the grid to %d request(s)",
                 len(_swept), budget)
    else:
        log.info("Sweep has only %d fresh finding(s); running the full grid",
                 len(_swept))

    # Watch the sweep, because the sweep watches these runs and nothing was
    # watching *it*. A reboot leaves it down - it is started by hand, unlike
    # these tasks - and then its silence watchdog is down with it, so a
    # stopped sweeper is invisible from both sides. The 2026-08-23 power cut
    # did exactly this and it went unnoticed until somebody asked.
    #
    # Only warn; never fail the run. The email matters more than the warning,
    # and the grid alone still produces one.
    try:
        idle = sweeper._age_hours(_store.last_active) if _store.last_active else None
        if idle is not None and idle > SWEEP_IDLE_HOURS:
            log.warning("The background sweep has not priced a window in "
                        "%.1f h. It does not restart itself - check whether "
                        "it is still running: python sweep_forever.py --status",
                        idle)
        _watch_the_sweep(cfg, prefs, throttle_state, store=_store, idle=idle)
    except Exception as exc:                # noqa: BLE001
        log.debug("sweep watchdog failed (%s); continuing", exc)

    rows = [] if args.no_history else _read_rows(cfg.history_csv)
    hot = schedule.hot_keys_from_history(rows, limit=cfg.hot_list_size)

    # The wide net. One text query per month asks Google for its own cheapest
    # dates, which finds in ~8 requests what the date grid needs a fortnight
    # to reach. The hints are unverified candidates: they carry no routing,
    # so they are pushed onto the *front* of the hot list and priced by the
    # ordinary search, where itinerary.validate() still rules on the visa.
    month_hints: list = []
    if cfg.monthly_scan and not args.status:
        nights = prefs.nights_options
        # Ask only about months actually being searched. Driving this from
        # the raw horizon meant the net kept querying September and April
        # after the trip owner excluded them - six wasted requests a run,
        # and worse: a hint for an excluded month went onto the front of the
        # hot list, spent Chrome budget, and could reach the email as a fare
        # in a month they had ruled out.
        months_asked = wide_net_months(prefs)
        # Every departure date the tracker would actually search. The wide
        # net uses it twice: to skip asking about a half-month that holds
        # none of them, and to refuse a hint that lands outside them.
        searchable = schedule.generate_windows(prefs)
        departures = {w.depart for w in searchable}
        searchable_keys = {w.key for w in searchable}
        with gate.google("run:wide-net", path=cfg.google_lock):
            month_hints = monthly.scan_months(
                fetch_text_query,
                months_asked,
                destination=cfg.monthly_scan_destination,
                origin=cfg.origins[0],
                min_nights=min(nights) if nights else None,
                max_nights=max(nights) if nights else None,
                halves=cfg.monthly_scan_halves,
                departures=departures,
                delay_s=cfg.monthly_scan_delay_seconds,
            )
        # The real probe count, not the month count. With `halves` on the
        # net sends 8 whole-month queries plus 16 half-month ones, and this
        # line reported "8 requests" for all 24 of them - which quietly
        # understated the project's daily footprint by 96 requests.
        asked = len(monthly.probe_count(months_asked,
                                        halves=cfg.monthly_scan_halves,
                                        departures=departures))
        for h in month_hints:
            log.info("Month hint  %s", h.describe())
        # Keep them. The hints are the only price data this project has for
        # the months the sweep has not reached yet, and until now every run
        # logged them and threw them away.
        try:
            monthly.record_hints(cfg.month_ledger, month_hints,
                                 asked=[label for label, _year in months_asked])
        except OSError as exc:
            log.debug("could not record month hints: %s", exc)
        if month_hints:
            # Second pass, deliberately, in the same spirit as
            # `itinerary.validate()` re-checking the visa after Google's
            # `connecting_airports` filter: what we asked for and what came
            # back are not the same thing. Google answers a month query
            # with whatever window it likes, and that window may be one
            # `whole_trip_in_searched_months` excluded - a late-March
            # departure coming home in April, say. Such a hint goes to the
            # *front* of the hot list and buys a Chrome launch, so letting
            # it through spends the scarcest budget in the project on a
            # trip nobody would take.
            keys = [k for k in monthly.hint_window_keys(month_hints)
                    if k in searchable_keys]
            skipped = len(month_hints) - len(keys)
            if skipped:
                log.info("Ignoring %d hint(s) for window(s) outside the "
                         "searched months", skipped)
            hot = keys + [k for k in hot if k not in set(keys)]
            log.info("Wide net: %d hint(s) from %d request(s); cheapest $%s",
                     len(month_hints), asked,
                     f"{min(h.price_usd for h in month_hints):,}")
        else:
            # Say so out loud. A silent wide net looks identical to one that
            # is switched off, and this is the part of the run most likely
            # to break when Google changes its wording.
            log.info("Wide net: %d request(s), no usable hint this run "
                     "(months return one only some of the time)", asked)

    # The window plan must not claim the whole budget when the hub sweep is
    # enabled, or the sweep never gets a single request and the config flag
    # is a lie. Cap the reserve at a quarter of the run so coverage still
    # dominates.
    hub_reserve = (
        min(max(cfg.hub_sweep_requests, 0), max(budget // 4, 1))
        if cfg.deep_hub_sweep else 0
    )
    active = rotation.active_destinations(prefs.destinations)
    probe = rotation.destinations_due_for_probe(prefs.destinations)
    demoted = [d for d in prefs.destinations if d not in active]
    if demoted:
        log.info("%s returned nothing in %d searches; %s",
                 ", ".join(demoted), schedule.DEST_PROBATION_AFTER,
                 f"probing {', '.join(probe)} this run" if probe
                 else "on probation, not searched this run")
    plan = schedule.build_plan(
        prefs, hot_keys=hot, rotation=rotation,
        request_budget=max(budget - hub_reserve - len(probe), 1),
        hot_share=cfg.hot_share, destinations=active,
    )

    if args.status:
        combos, full = schedule.estimate_requests(prefs, destinations=active)
        gen_days = schedule.coverage_days(plan.slices_total, args.runs_per_day)
        pri_days = schedule.coverage_days(
            plan.priority_slices_total, args.runs_per_day)
        print(prefs.describe())
        searching = ", ".join(active)
        print(f"  Searching    : {searching}"
              + (f"  (on probation: {', '.join(demoted)})" if demoted else ""))
        print(f"  Search space : {combos} windows -> {full} searches "
              f"for a complete pass")
        print(f"  This run     : {plan.describe()}")
        print(f"  Coverage     : priority months every {pri_days:.1f} day(s), "
              f"everything else every {gen_days:.1f} day(s)")
        print(f"  Throttle     : {throttle_state.advice(args.runs_per_day)}")
        print(f"  Hot list     : {len(hot)} window(s) re-priced every run")
        # The only 8-month-wide price picture the project has. The sweep
        # walks the priority months first, so on 2026-08-23 it had priced
        # January to March and nothing else - while the wide net had been
        # asking about every month, six times a day, since the start.
        ledger = monthly.load_ledger(cfg.month_ledger)
        searched = [label for label, _year in wide_net_months(prefs)]
        print("\nWide net, cheapest seen per month:")
        for line in monthly.format_ledger(ledger,
                                          threshold=prefs.good_price_usd,
                                          only=searched):
            print(line)
        return 0

    log.info("Alert under %s, standout under %s",
             format_price(cfg.good_price_usd), format_price(cfg.great_price_usd))

    searcher = Searcher(delay=cfg.request_delay_seconds, max_requests=budget)
    with gate.google("run:grid", path=cfg.google_lock):
        accepted, rejected, errors, sample, google_bands = collect(
            cfg, plan, searcher, probe_destinations=probe)
    judged_requests, empties = sample

    log.info("Throttle: %s",
             throttle_state.record(requests=judged_requests, empty=empties))
    if throttle_state.looks_blocked:
        log.warning("%s", throttle_state.advice(args.runs_per_day))
    throttle_state.save(cfg.throttle_file)

    rotation.record_destinations(
        [*plan.destinations, *probe], {i.destination for i in accepted})
    rotation.advance(plan.slices_total, plan.priority_slices_total)
    rotation.save(cfg.rotation_file)

    log.info("%d usable option(s); %d rejected; %d request(s)",
             len(accepted), len(rejected), searcher.requests_made)
    for rej in rejected[:5]:
        log.debug("rejected %s: %s", rej.itinerary.signature, rej.reason)

    # Deliberately *not* an early return any more. The HTTP grid is the
    # weakest source in the project - floored at 8 requests, ~74% of what
    # it returns is visa-rejected, and it cannot see stays over 30 nights
    # at all - and aborting here handed it a veto over the whole product.
    # A run where it happened to find nothing threw away a Chrome-verified
    # $1,347 and 400 sweep findings and sent no email at all.
    #
    # The run now continues and decides at the end, when it knows what
    # Chrome and the sweep found. Nothing is sent only when there is
    # genuinely nothing to say.
    if not accepted:
        log.warning("The grid found nothing usable; continuing on Chrome "
                    "and the background sweep.")

    hist_prices, hist_days = [], 0
    if not args.no_history:
        # Verified rows only. The HTTP rows in this file are systematically
        # dearer (median $2,866 against Chrome's $2,346 on the same day)
        # because HTTP cannot see the cheap European routings, so mixing
        # the two populations describes neither.
        hist_prices = history.read_prices(
            cfg.history_csv, origin=cfg.origins[0], band_source="CHROME")
        # The sweep sees far more of the market than the scheduled runs do,
        # so its observations are most of the baseline.
        hist_prices += history.read_prices(
            cfg.sweep_history_csv, origin=cfg.origins[0], band_source="CHROME")
        # Count the days across *both* logs, because the prices come from
        # both. The sweep contributes the large majority - 2,210 of 2,811
        # Chrome observations on 2026-08-25 - and it is the only thing that
        # prices the whole calendar, so judging "enough days" on the
        # scheduled runs' own file alone could hold the real bands back on
        # evidence the tracker already has.
        hist_days = history.distinct_days_across(
            [cfg.history_csv, cfg.sweep_history_csv], origin=cfg.origins[0])
    bands = pricing.resolve_bands(
        google_bands=google_bands,
        history_prices=hist_prices,
        history_days=hist_days,
    )
    # Close the bar's ends with what has actually been seen. The cut-offs
    # are percentiles and say nothing about what cheap *reaches*, which is
    # how the email came to show "$1,641 is cheap" above a green zone
    # starting below any fare that exists. `hist_prices` is every Chrome
    # observation from both logs - and most of them come from the
    # background sweep, the only thing that prices the whole calendar.
    bands = bands.with_observed(hist_prices)
    log.info("Bands (%s): cheap < %s, expensive > %s", bands.source,
             format_price(bands.low), format_price(bands.high))
    # Google's own range is still worth recording even though it no longer
    # labels anything: a shift in it is real news about the market. It is
    # not used for classification because it describes every routing Google
    # sells, including the US and Canada transits that are not bookable
    # here - measured 2026-08-23, it called 0 of 1,249 visa-free fares cheap.
    if google_bands is not None:
        log.info("Google's own range for comparison: %s-%s (usual %s); "
                 "includes routings this passport cannot use",
                 format_price(google_bands.low), format_price(google_bands.high),
                 format_price(google_bands.usual) if google_bands.usual else "n/a")

    if accepted:
        best = accepted[0]
        log.info("Cheapest: %s %s %s -> %s", format_price(best.price_usd),
                 best.route_label, best.outbound_date,
                 bands.classify(best.price_usd))

    # Re-price the windows that matter through Chrome. The HTTP grid above
    # cannot see the Zurich routings where the sub-threshold fares live: on
    # 2027-01-29 it called $1,635 the cheapest while the real answer was
    # $1,347 on Edelweiss/SWISS. Whatever Chrome finds is the truth for
    # these windows, so it is logged loudly and drives the alert price.
    verified: list = []
    chrome_stats: dict = {}
    if cfg.chrome_verify:
        # Do not spend a launch on a window the sweep has just priced.
        # Measured 2026-08-24: 53 Chrome launches went to 18 distinct
        # windows, and nine of them took 44 - four or five checks each,
        # while the background sweep was re-pricing those same nine every
        # ~10 hours on its hot tier. Two systems doing the same work, and
        # a Chrome launch is the most expensive request this project makes.
        #
        # The swept price is already folded into `verified` below and
        # carries its own "checked N hr ago" label, so nothing is lost by
        # letting it stand. When the sweep is stopped its findings age out
        # and every window becomes eligible again, which is the right
        # fallback rather than a special case.
        fresh_swept = _recently_swept(cfg)
        skipped_fresh = 0
        targets = verify_mod.choose_targets(
            hint_keys=monthly.hint_window_keys(month_hints),
            hot_keys=hot,
            grid_keys=[f"{i.outbound_date.isoformat()}_{i.return_date.isoformat()}"
                       for i in accepted[:20] if i.return_date],
            limit=cfg.chrome_max_per_run,
            today=datetime.now(CR_TZ).date(),
            min_lead_days=prefs.min_lead_days,
        )
        if fresh_swept:
            kept = [t for t in targets if t.key not in fresh_swept]
            # Never thin the sample below what `run_looks_blocked` needs to
            # tell a blackout from a quiet run; three launches is its floor.
            if len(kept) >= BLOCKED_MIN_CHROME:
                skipped_fresh = len(targets) - len(kept)
                targets = kept
            if skipped_fresh:
                log.info("Skipping %d window(s) the sweep priced within "
                         "%.0fh; %d launch(es) left to spend",
                         skipped_fresh, cfg.chrome_skip_if_swept_hours,
                         len(targets))
        with gate.google("run:chrome", path=cfg.google_lock):
            verified = verify_mod.verify(
                targets,
                origin=cfg.origins[0], destination=cfg.chrome_destination,
                max_stops=cfg.max_stops, chrome_override=cfg.chrome_path,
                max_total_hours=cfg.max_total_hours,
                timeout_s=cfg.chrome_timeout_s, budget_ms=cfg.chrome_budget_ms,
                sleep=time.sleep, delay_s=cfg.request_delay_seconds,
                stats=chrome_stats,
            )
    # Fold in whatever the background sweep has found since the last run.
    # It walks every window in the space, so it reaches dates this run's
    # twenty Chrome launches never touch. A missing store just means the
    # sweep is not running, which must never break the email.
    try:
        store = sweeper.SweepStore.load(cfg.sweep_store)
        swept = [d.to_option() for d in
                 store.best(limit=12, max_age_hours=cfg.sweep_max_age_hours)]
    except Exception as exc:                # noqa: BLE001
        log.debug("sweep store unreadable (%s); continuing without it", exc)
        swept = []
    if swept:
        # De-duplicate by *window*, not by (window, price). Including the
        # price meant that when Chrome re-priced a window the sweep already
        # held at a slightly different number, the same flight appeared
        # twice - $1,347 live beside $1,349 from three hours ago - burning
        # two of the ten visible rows on one itinerary and inviting the
        # reader to wonder which is true.
        #
        # Chrome's row wins because it is the live one; the sweep's is
        # dropped rather than shown alongside.
        known = {(o.depart_date, o.return_date) for o in verified}
        fresh = [o for o in swept
                 if (o.depart_date, o.return_date) not in known]
        log.info("Background sweep contributed %d window(s); cheapest %s",
                 len(fresh), format_price(min(o.price_usd for o in swept)))
        verified = sorted(verified + fresh, key=lambda o: o.price_usd)

        cheap = verify_mod.under(verified, cfg.good_price_usd)
        if cheap:
            log.info("CHROME FOUND %d fare(s) at or under %s:",
                     len(cheap), format_price(cfg.good_price_usd))
            for o in cheap[:5]:
                log.info("   %s", o.describe())
        elif verified:
            log.info("Chrome: %d visa-free option(s), cheapest %s (none under %s)",
                     len(verified), format_price(verified[0].price_usd),
                     format_price(cfg.good_price_usd))
        else:
            log.info("Chrome: %d window(s) checked, no visa-free option found",
                     len(targets))

    # Tell the trip owner when a run gets nothing at all. Until now only the
    # sweep could raise a block alarm, so whenever the sweep was stopped -
    # which it was for hours on 2026-08-23 - nothing watched these six runs.
    # The 12:26 run on 2026-08-24 went dark on every channel and told nobody.
    # The whole block is best-effort. It sits between the search and the
    # email, which is precisely where a crash costs the trip owner the
    # thing they actually receive - the 09:03 run on 2026-08-24 died two
    # lines from here and sent nothing. A warning that fails to send is a
    # nuisance; a warning that takes the email down with it is the bug it
    # was written to catch.
    try:
        _raise_block_alarm(cfg, prefs, throttle_state,
                           chrome_stats=chrome_stats,
                           grid_requests=judged_requests, grid_empty=empties,
                           verified=verified)
    except Exception as exc:                # noqa: BLE001
        log.warning("block alarm failed (%s); continuing with the run", exc)

    preview = ranking.select_top(
        accepted, count=prefs.result_count,
        is_priority=ranking.priority_checker(prefs.priority_months),
        priority_share=prefs.priority_share,
    )
    log.info("Ranking: %s", preview.describe())

    if not args.no_history:
        rows = history.rows_from(accepted, band_of=bands.classify,
                                 band_source=bands.source)
        # Chrome's prices are the accurate ones, so they belong in the
        # history too - otherwise the hot list only ever learns the
        # inflated HTTP numbers and keeps re-pricing the wrong windows.
        rows += history.rows_from_verified(verified, band_of=bands.classify)
        # Best effort, like the sweep's identical call. This runs *before*
        # the email, so an unguarded write here means a locked or full disk
        # costs the trip owner the thing they actually receive - and on
        # Windows the file is read by other processes constantly, so a
        # transient PermissionError is not exotic. Losing a few history
        # rows is a rounding error against losing the email.
        try:
            written = history.append(cfg.history_csv, rows)
            log.info("Logged %d row(s) (%d from Chrome)", written, len(verified))
        except OSError as exc:
            log.warning("could not write %s (%s); the email is unaffected",
                        cfg.history_csv, exc)

    qualifying = [i for i in accepted if i.price_usd <= cfg.good_price_usd]
    # The fare the email is *about*. Normally the cheapest one clearing the
    # threshold; in digest mode nothing may clear it, and then the email is
    # about the cheapest fare found. Never index qualifying[0] directly:
    # under daily_digest that list is empty on an expensive day.
    # With an empty grid there is no `best` at all, and the email is then
    # entirely Chrome's and the sweep's. Fall through to `verified`.
    headline_pick = (qualifying[0] if qualifying
                     else (accepted[0] if accepted else None))
    if headline_pick is None and not verified:
        log.warning("Nothing usable from any source this run.")
        return 0
    if headline_pick is None:
        alert_price = verified[0].price_usd
        alert_signature = f"chrome|{verified[0].deep_link[:80]}"
        log.info("Alert price comes from Chrome: %s (the grid found nothing)",
                 format_price(alert_price))
    # Chrome sees fares the grid cannot. When it found something cheaper,
    # that is the day's real best price and the alert must be about it,
    # otherwise the email reports $1,635 on a day a $1,347 seat existed.
    if headline_pick is not None:
        alert_price = headline_pick.price_usd
        alert_signature = headline_pick.signature
    if headline_pick is not None and verified and verified[0].price_usd < alert_price:
        alert_price = verified[0].price_usd
        alert_signature = f"chrome|{verified[0].deep_link[:80]}"
        log.info("Alert price comes from Chrome: %s (grid said %s)",
                 format_price(alert_price), format_price(headline_pick.price_usd))
    state = alerts.AlertState.load(cfg.state_file)
    now = datetime.now(CR_TZ)
    decision = alerts.decide(
        state,
        best_price=alert_price,
        best_signature=alert_signature,
        good_threshold=cfg.good_price_usd,
        great_threshold=cfg.great_price_usd,
        now=now,
        min_drop_usd=cfg.min_drop_usd,
        min_drop_pct=cfg.min_drop_pct,
        reserve_last_slot=cfg.reserve_last_slot,
        last_call_hour=cfg.last_call_hour,
        always_send=cfg.daily_digest,
    )

    if not decision.should_send:
        log.info("No email: %s", decision.reason)
        state.roll_day(now)
        state.save(cfg.state_file)
        return 0

    log.info("Emailing (slot %d/2): %s", decision.slot, decision.reason)

    # The renderer gets the whole ranked set and decides how many rows to
    # show, marking anything above the threshold.
    content = email_render.render(
        accepted, bands,
        threshold=cfg.good_price_usd,
        is_great=decision.is_great,
        generated_at=now.strftime("%b %d, %Y at %H:%M") + " Costa Rica time",
        dashboard_url=cfg.dashboard_url or None,
        count=prefs.result_count,
        priority_months=prefs.priority_months,
        priority_share=prefs.priority_share,
        priority_label=prefs.priority_label,
        verified=verified,
    )

    if args.save_preview:
        Path(args.save_preview).write_text(content.html, encoding="utf-8")
        log.info("Preview written to %s", args.save_preview)

    result = notify.send_email(
        content, to_addr=cfg.alert_email, smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port, smtp_user=cfg.smtp_user,
        smtp_password=cfg.smtp_password, from_name=cfg.from_name,
        dry_run=args.dry_run,
    )
    log.info("Email: %s", result.detail)

    if cfg.ntfy_topic:
        # `headline_pick` is None when the grid found nothing and the email
        # is carried entirely by Chrome and the sweep - a case that only
        # became possible on 2026-08-25, when the grid lost its veto. The
        # push then describes the verified fare instead.
        if headline_pick is not None:
            body = (f"{format_price(headline_pick.price_usd)} "
                    f"{headline_pick.route_label} {headline_pick.outbound_date}")
            url = headline_pick.deep_link
        else:
            top = verified[0]
            body = (f"{format_price(top.price_usd)} "
                    f"{top.origin}-{top.destination} {top.depart_date}")
            url = top.deep_link
        log.info("Push: %s", notify.send_push(
            cfg.ntfy_topic, title=content.subject, body=body,
            url=url, dry_run=args.dry_run).detail)

    if result.ok and not args.dry_run:
        alerts.record_sent(
            state, best_price=alert_price,
            best_signature=alert_signature,
            is_great=decision.is_great, now=now)
    else:
        state.roll_day(now)
    state.save(cfg.state_file)
    return 0 if result.ok else 1


def main() -> None:
    """Run, and make sure a crash says so in the log.

    The scheduled task discards stderr, so before this an unhandled
    exception left exit code 1 and a log file that simply stopped
    mid-sentence. That is what the 09:03 run on 2026-08-24 looked like:
    the grid finished, and then nothing, with no way to tell a crash from
    a machine that went to sleep. `tracker.log` exists precisely so a run
    leaves a trace, and the one run that most needs to leave one was the
    one that could not.
    """
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except BaseException:                   # noqa: BLE001 - then re-raised
        log.exception("The run failed and did not finish")
        raise


if __name__ == "__main__":
    main()
