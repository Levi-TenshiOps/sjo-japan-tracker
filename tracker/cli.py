"""Entry point: plan a small slice, search, filter, classify, log, maybe email."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import (
    alerts, config as config_mod, email_render, history, monthly, notify,
    pricing, ranking, schedule, sweeper, throttle, verify as verify_mod,
)
from .itinerary import Itinerary, dedupe, format_price, partition
from .preferences import Preferences, PreferencesError
from .search import Searcher, fetch_text_query, plan_broad, plan_hub_sweep

CR_TZ = ZoneInfo("America/Costa_Rica")
log = logging.getLogger("tracker")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _read_rows(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


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


def _rotate(items: list[str], by: int) -> list[str]:
    """items rotated left by `by`, so a truncated sweep still covers them all."""
    if not items:
        return []
    i = by % len(items)
    return [*items[i:], *items[:i]]


def collect(cfg, plan, searcher: Searcher, probe_destinations=()):
    """This slice's searches: (accepted, rejected, errors, empties, bands)."""
    pairs = plan.date_pairs()
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

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

    rows = [] if args.no_history else _read_rows(cfg.history_csv)
    hot = schedule.hot_keys_from_history(rows, limit=cfg.hot_list_size)

    # The wide net. One text query per month asks Google for its own cheapest
    # dates, which finds in ~8 requests what the date grid needs a fortnight
    # to reach. The hints are unverified candidates: they carry no routing,
    # so they are pushed onto the *front* of the hot list and priced by the
    # ordinary search, where itinerary.validate() still rules on the visa.
    month_hints: list = []
    if cfg.monthly_scan and not args.status:
        early, late = prefs.window_on(None)
        nights = prefs.nights_options
        month_hints = monthly.scan_months(
            fetch_text_query,
            monthly.months_in_window(early, late),
            destination=cfg.monthly_scan_destination,
            origin=cfg.origins[0],
            min_nights=min(nights) if nights else None,
            max_nights=max(nights) if nights else None,
            halves=cfg.monthly_scan_halves,
        )
        asked = len(monthly.months_in_window(early, late))
        for h in month_hints:
            log.info("Month hint  %s", h.describe())
        if month_hints:
            keys = monthly.hint_window_keys(month_hints)
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
        return 0

    log.info("Alert under %s, standout under %s",
             format_price(cfg.good_price_usd), format_price(cfg.great_price_usd))

    searcher = Searcher(delay=cfg.request_delay_seconds, max_requests=budget)
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

    if not accepted:
        log.warning("Nothing usable this run.")
        return 0

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
        hist_days = history.distinct_days(cfg.history_csv, origin=cfg.origins[0])
    bands = pricing.resolve_bands(
        google_bands=google_bands,
        history_prices=hist_prices,
        history_days=hist_days,
    )
    log.info("Bands (%s): cheap < %s, expensive > %s", bands.source,
             format_price(bands.low), format_price(bands.high))

    best = accepted[0]
    log.info("Cheapest: %s %s %s -> %s", format_price(best.price_usd),
             best.route_label, best.outbound_date, bands.classify(best.price_usd))

    # Re-price the windows that matter through Chrome. The HTTP grid above
    # cannot see the Zurich routings where the sub-threshold fares live: on
    # 2027-01-29 it called $1,635 the cheapest while the real answer was
    # $1,347 on Edelweiss/SWISS. Whatever Chrome finds is the truth for
    # these windows, so it is logged loudly and drives the alert price.
    verified: list = []
    if cfg.chrome_verify:
        targets = verify_mod.choose_targets(
            hint_keys=monthly.hint_window_keys(month_hints),
            hot_keys=hot,
            grid_keys=[f"{i.outbound_date.isoformat()}_{i.return_date.isoformat()}"
                       for i in accepted[:20] if i.return_date],
            limit=cfg.chrome_max_per_run,
            today=datetime.now(CR_TZ).date(),
            min_lead_days=prefs.min_lead_days,
        )
        verified = verify_mod.verify(
            targets,
            origin=cfg.origins[0], destination=cfg.chrome_destination,
            max_stops=cfg.max_stops, chrome_override=cfg.chrome_path,
            timeout_s=cfg.chrome_timeout_s, budget_ms=cfg.chrome_budget_ms,
            sleep=time.sleep, delay_s=cfg.request_delay_seconds,
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
        known = {(o.depart_date, o.return_date, o.price_usd) for o in verified}
        fresh = [o for o in swept
                 if (o.depart_date, o.return_date, o.price_usd) not in known]
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
        log.info("Logged %d row(s) (%d from Chrome)",
                 history.append(cfg.history_csv, rows), len(verified))

    qualifying = [i for i in accepted if i.price_usd <= cfg.good_price_usd]
    # The fare the email is *about*. Normally the cheapest one clearing the
    # threshold; in digest mode nothing may clear it, and then the email is
    # about the cheapest fare found. Never index qualifying[0] directly:
    # under daily_digest that list is empty on an expensive day.
    headline_pick = qualifying[0] if qualifying else best
    # Chrome sees fares the grid cannot. When it found something cheaper,
    # that is the day's real best price and the alert must be about it,
    # otherwise the email reports $1,635 on a day a $1,347 seat existed.
    alert_price = headline_pick.price_usd
    alert_signature = headline_pick.signature
    if verified and verified[0].price_usd < alert_price:
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
        log.info("Push: %s", notify.send_push(
            cfg.ntfy_topic, title=content.subject,
            body=f"{format_price(headline_pick.price_usd)} "
                 f"{headline_pick.route_label} {headline_pick.outbound_date}",
            url=headline_pick.deep_link, dry_run=args.dry_run).detail)

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
    sys.exit(run())


if __name__ == "__main__":
    main()
