"""Regressions for bugs found by simulation and by real Google traffic.

Each class names the failure it locks out. All offline: the Google payload is
a real capture trimmed to a fixture, and every search injects a fake.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
from pathlib import Path

import pytest

from tracker import schedule
from tracker.airports import describe_destination, destination_codes, is_metro
from tracker.itinerary import (
    Itinerary, Leg, build_itinerary, dedupe, split_outbound, validate,
)
from tracker.preferences import Preferences
from tracker.pricing import PriceBands, median_bands, resolve_bands
from tracker.search import RouteQuery, Searcher, extract_google_bands
from tests import fixtures as fx

FIXTURE_DIR = Path(__file__).parent


def prefs(**kw):
    base = dict(
        alert_email="a@b.com", search_months=8, min_lead_days=21,
        departure_step_days=4, priority_months=[1, 2, 3], priority_share=0.5,
        trip_weeks=[2, 3, 4, 5], destinations=["TYO"],
    )
    base.update(kw)
    p = Preferences(**base)
    p.validate()
    return p


# --------------------------------------------------------------------------
class TestGridDoesNotDriftWithToday:
    """The departure grid used to be anchored to `today`.

    It moved forward one day every day, so with a 4-day step only one day in
    four sampled the same dates. The hot list is keyed by (depart, return),
    so it matched nothing on the other three days and quietly emptied, and
    the rotation cursors indexed a different pool on every run.
    """

    def test_same_departure_dates_on_consecutive_days(self):
        p = prefs()
        d0 = date(2026, 8, 22)
        first = sorted({w.depart for w in schedule.generate_windows(p, today=d0)})
        for k in range(1, 8):
            later = sorted({w.depart for w in
                            schedule.generate_windows(p, today=d0 + timedelta(days=k))})
            # Dates may fall off the front as they get too close to book, and
            # new ones appear at the far end, but the grid itself must not move.
            overlap = set(first) & set(later)
            assert overlap, f"day +{k} shares no departure date with day 0"
            assert overlap == {d for d in later if d in set(first)}
            for d in later:
                assert (d - schedule.GRID_EPOCH).days % p.departure_step_days == 0

    def test_hot_list_survives_the_next_day(self):
        """A window found cheap today must still be re-priceable tomorrow."""
        p = prefs()
        d0 = date(2026, 8, 22)
        hot = [w.key for w in schedule.generate_windows(p, today=d0)][:6]
        for k in range(0, 6):
            plan = schedule.build_plan(
                p, hot_keys=hot, rotation=schedule.RotationState(),
                request_budget=24, hot_share=0.4, today=d0 + timedelta(days=k))
            assert plan.hot, f"hot list empty on day +{k}"

    def test_rotation_covers_every_window(self):
        """Over one full cycle every window must get priced at least once."""
        p = prefs(destinations=["TYO"])
        today = date(2026, 8, 22)
        rot = schedule.RotationState()
        universe = {w.key for w in schedule.generate_windows(p, today=today)}
        seen: set[str] = set()
        for _ in range(200):
            plan = schedule.build_plan(
                p, hot_keys=[], rotation=rot, request_budget=24,
                hot_share=0.4, today=today)
            seen.update(w.key for w in plan.windows)
            rot.advance(plan.slices_total, plan.priority_slices_total)
            if universe <= seen:
                break
        assert universe <= seen, f"{len(universe - seen)} window(s) never searched"


# --------------------------------------------------------------------------
class TestBudgetIsSpentOnCoverageFirst:
    """Retries used to run depth-first and starve the tail of the plan.

    Three attempts on query 1 before query 2 was touched meant a handful of
    dead searches ate the request budget, and the windows at the end of the
    plan were never priced at all - while the rotation cursor advanced past
    them regardless.
    """

    def make(self, dead_hubs, **kw):
        def fetch(query):
            raw = query.to_bytes()
            if any(h.encode() in raw for h in dead_hubs):
                return []
            return [fx.MEX_OPTION]
        return Searcher(fetch=fetch, delay=0, jitter=0, sleep=lambda _: None, **kw)

    def queries(self, hubs):
        return [RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN, hub=h) for h in hubs]

    def test_every_query_is_attempted_before_any_retry(self):
        hubs = ["MEX", "ZRH", "MAD", "AMS", "DOH", "IST"]
        s = self.make({"MEX"}, max_requests=len(hubs))
        outs = s.run_all(self.queries(hubs))
        starved = [o for o in outs if o.error == "request budget exhausted"]
        assert not starved, "a planned search was never attempted"
        assert s.requests_made == len(hubs)

    def test_dead_searches_do_not_eat_the_tail(self):
        hubs = ["MEX", "ZRH", "MAD", "AMS", "DOH", "IST", "DXB", "ICN"]
        s = self.make({"MEX", "ZRH", "MAD"}, max_requests=len(hubs))
        outs = s.run_all(self.queries(hubs))
        priced = [o for o in outs if o.ok]
        assert len(priced) == 5, "live searches lost budget to dead ones"

    def test_empty_response_is_retried_once_not_twice(self):
        """An empty page is usually the truth; three tries buys nothing."""
        calls = []

        def fetch(query):
            calls.append(query)
            return []
        s = Searcher(fetch=fetch, delay=0, jitter=0, sleep=lambda _: None)
        out = s.run(RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN))
        assert out.error == "no results"
        assert len(calls) == 2

    def test_exceptions_still_get_the_full_retry_allowance(self):
        fetcher = fx.FakeFetcher(fail_times=99)
        s = Searcher(fetch=fetcher, delay=0, max_retries=3, sleep=lambda _: None)
        s.run(RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN))
        assert len(fetcher.calls) == 3

    def test_empty_rate_counts_requests_not_queries(self):
        """Reporting per query hid retries and understated throttling."""
        s = Searcher(fetch=lambda q: [], delay=0, jitter=0, sleep=lambda _: None)
        s.run(RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN))
        assert s.requests_made == 2
        assert s.barren_requests == 2
        assert s.empty_rate == 1.0


# --------------------------------------------------------------------------
class TestMetroDestinations:
    """One TYO search covers Narita and Haneda for the price of one request.

    Without metro awareness `split_outbound` never found the destination, so
    the whole round trip counted as the outbound, every itinerary blew past
    max_total_hours, and a TYO search returned nothing usable at all.
    """

    def hnd_option(self):
        return fx.FakeFlights(
            price=1975,
            airlines=["Lufthansa"],
            flights=[
                fx.leg("SJO", "FRA", (2027, 1, 15, 20, 0), (2027, 1, 16, 14, 0), 660),
                fx.leg("FRA", "HND", (2027, 1, 16, 17, 0), (2027, 1, 17, 12, 0), 680),
                fx.leg("HND", "FRA", (2027, 1, 24, 11, 0), (2027, 1, 24, 17, 0), 800),
                fx.leg("FRA", "SJO", (2027, 1, 25, 10, 0), (2027, 1, 25, 16, 0), 660),
            ],
        )

    def build(self, raw, dest):
        return build_itinerary(raw, origin="SJO", destination=dest,
                               outbound_date=fx.DEPART, return_date=fx.RETURN)

    def test_metro_code_expands(self):
        assert destination_codes("TYO") == {"NRT", "HND"}
        assert destination_codes("KIX") == {"KIX"}
        assert is_metro("TYO") and not is_metro("NRT")

    def test_landing_at_hnd_satisfies_a_tyo_search(self):
        itin = self.build(self.hnd_option(), "TYO")
        assert validate(itin, max_total_hours=60, min_layover_min=75) is None

    def test_outbound_split_finds_the_metro_arrival(self):
        itin = self.build(self.hnd_option(), "TYO")
        assert itin.outbound_leg_count == 2
        assert itin.stops_outbound == 1
        # 660 + 180 layover + 680 = 1520 min, not the whole round trip.
        assert itin.outbound_duration_min == 1520

    def test_arrival_airport_is_the_real_one(self):
        itin = self.build(self.hnd_option(), "TYO")
        assert itin.arrival_airport == "HND"
        assert itin.route_label == "SJO–HND"

    def test_destination_airports_are_not_listed_as_hubs(self):
        itin = self.build(self.hnd_option(), "TYO")
        assert itin.hubs == ["FRA"]
        assert "HND" not in itin.hubs

    def test_narita_and_haneda_stay_separate_rows(self):
        """A metro search returns both airports; dedupe must not merge them."""
        hnd = self.build(self.hnd_option(), "TYO")
        nrt_raw = fx.FakeFlights(
            price=1975, airlines=["Lufthansa"],
            flights=[
                fx.leg("SJO", "FRA", (2027, 1, 15, 20, 0), (2027, 1, 16, 14, 0), 660),
                fx.leg("FRA", "NRT", (2027, 1, 16, 17, 0), (2027, 1, 17, 12, 0), 680),
                fx.leg("NRT", "FRA", (2027, 1, 24, 11, 0), (2027, 1, 24, 17, 0), 800),
                fx.leg("FRA", "SJO", (2027, 1, 25, 10, 0), (2027, 1, 25, 16, 0), 660),
            ])
        nrt = self.build(nrt_raw, "TYO")
        assert hnd.signature != nrt.signature
        assert len(dedupe([hnd, nrt])) == 2

    def test_banned_routing_still_rejected_under_a_metro_search(self):
        """Metro awareness must not widen the visa check."""
        itin = self.build(fx.DFW_OPTION, "TYO")
        assert "DFW" in (validate(itin) or "")

    def test_describe_destination(self):
        assert describe_destination("TYO") == "Tokyo"
        assert describe_destination("HND") == "Tokyo Haneda (HND)"

    def test_email_names_the_city_not_the_metro_code(self):
        """The subject line read 'San José to TYO' before this."""
        from tracker import email_render
        from tracker.pricing import SEED_BANDS
        itin = self.build(self.hnd_option(), "TYO")
        content = email_render.render([itin], SEED_BANDS, threshold=2600,
                                      is_great=False, generated_at="now")
        assert "TYO" not in content.html
        assert "Tokyo" in content.html
        assert "Tokyo" in content.subject or "Tokyo" in content.text



# --------------------------------------------------------------------------
class TestGoogleBandsFromARealPayload:
    """`extract_google_bands` was written blind. This pins it to a capture."""

    def html(self):
        return (FIXTURE_DIR / "payload_ds1.html").read_text(encoding="utf-8")

    def test_reads_the_real_capture(self):
        bands = extract_google_bands(self.html())
        assert bands is not None
        assert (bands.low, bands.high, bands.usual) == (1066, 4035, 1545)
        assert bands.source == "GOOGLE"

    def test_still_refuses_nonsense(self):
        for bad in ("", "<html></html>", 'script class="ds:1" data:[[1,2]],'):
            assert extract_google_bands(bad) is None

    def test_usual_outside_the_range_is_dropped_not_reported(self):
        html = self.html().replace(",1545]", ",99999]")
        bands = extract_google_bands(html)
        assert bands is None or bands.usual != 99999

    def test_bands_reach_the_email_path(self):
        """resolve_bands used to be called without google_bands at all."""
        google = PriceBands(low=1000, high=3000, usual=1500, source="GOOGLE")
        chosen = resolve_bands(google_bands=google,
                               history_prices=[1200] * 40, history_days=9)
        assert chosen.source == "GOOGLE"

    def test_median_bands_ignores_one_odd_route(self):
        readings = [
            PriceBands(1000, 3000, 1500, "GOOGLE"),
            PriceBands(1100, 3100, 1600, "GOOGLE"),
            PriceBands(50_000, 90_000, 60_000, "GOOGLE"),
        ]
        out = median_bands(readings)
        assert out.low == 1100 and out.high == 3100
        assert median_bands([]) is None


# --------------------------------------------------------------------------
class TestUnparseablePayloadIsNotAnException:
    """Google omits the itinerary block entirely when a search finds nothing.

    fast-flights raises TypeError on that shape. Left alone it burned the
    full retry allowance on every genuinely-empty search.
    """

    def test_parse_failure_reads_as_no_results(self, monkeypatch):
        import tracker.search as search_mod

        def boom(_html):
            raise TypeError("'NoneType' object is not subscriptable")
        monkeypatch.setattr(search_mod, "fetch_flights_html", lambda q: "<html>")
        monkeypatch.setattr(search_mod, "parse_flights_html", boom)

        s = Searcher(delay=0, jitter=0, sleep=lambda _: None)
        out = s.run(RouteQuery("SJO", "TYO", fx.DEPART, fx.RETURN))
        assert out.error == "no results"
        assert s.requests_made == 2      # one retry, not the full allowance


# --------------------------------------------------------------------------
class TestWindowsDateFormatting:
    """`%-d` is glibc-only and raises ValueError on Windows."""

    def test_renders_on_any_platform(self):
        from tracker.email_render import _fmt_date
        assert _fmt_date(date(2027, 1, 5)) == "Tue, Jan 5"
        assert _fmt_date(date(2027, 1, 15)) == "Fri, Jan 15"


# --------------------------------------------------------------------------
class TestUnproductiveDestinationsStepAside:
    """SJO-Osaka returns nothing from Google, at any stop count.

    Searching it every run spent half the request budget to learn that again,
    which halved how much of the 8-month window got priced. It now drops to an
    occasional probe and comes straight back if it ever returns a fare.
    """

    def test_healthy_destinations_are_all_searched(self):
        rot = schedule.RotationState()
        for _ in range(30):
            active = rot.active_destinations(["TYO", "OSA"])
            assert active == ["TYO", "OSA"]
            rot.record_destinations(active, {"TYO", "OSA"})

    def run_for(self, runs, produced, destinations=("TYO", "OSA")):
        """Drive the state the way the CLI does and report what got searched."""
        rot = schedule.RotationState()
        searched = []
        for _ in range(runs):
            active = rot.active_destinations(destinations)
            probe = rot.destinations_due_for_probe(destinations)
            searched.append((tuple(active), tuple(probe)))
            rot.record_destinations([*active, *probe], set(produced))
        return rot, searched

    def test_a_dead_destination_is_demoted_not_dropped(self):
        rot, searched = self.run_for(60, {"TYO"})
        in_plan = sum("OSA" in a for a, _ in searched)
        probed = sum("OSA" in pr for _, pr in searched)
        assert all("TYO" in a for a, _ in searched), "a paying destination was demoted"
        assert in_plan == schedule.DEST_PROBATION_AFTER
        assert 0 < probed < 12, "probe cadence wrong"

    def test_the_probe_stays_out_of_the_window_plan(self):
        """Folding the probe back in would halve the windows priced that run
        and re-map the rotation cursor, turning the sweep into a random walk.
        """
        rot, searched = self.run_for(60, {"TYO"})
        for active, probe in searched[schedule.DEST_PROBATION_AFTER:]:
            assert active == ("TYO",), "probe leaked into the window plan"
            assert set(probe) <= {"OSA"}

    def test_it_promotes_itself_the_moment_it_pays_out(self):
        rot, _ = self.run_for(40, {"TYO"})
        assert rot.dest_misses["OSA"] >= schedule.DEST_PROBATION_AFTER
        rot.record_destinations(["TYO", "OSA"], {"TYO", "OSA"})
        assert rot.dest_misses["OSA"] == 0
        assert rot.active_destinations(["TYO", "OSA"]) == ["TYO", "OSA"]
        assert rot.destinations_due_for_probe(["TYO", "OSA"]) == []

    def test_never_leaves_a_run_with_nothing_to_search(self):
        rot, searched = self.run_for(40, set())
        assert all(a for a, _ in searched)
        assert rot.active_destinations(["TYO", "OSA"]) == ["TYO", "OSA"]

    def test_budget_follows_the_active_destinations(self):
        """Demoting a destination must buy more windows, not idle requests."""
        p = prefs(destinations=["TYO", "OSA"])
        today = date(2026, 8, 22)
        both = schedule.build_plan(p, rotation=schedule.RotationState(),
                                   request_budget=24, today=today,
                                   destinations=["TYO", "OSA"])
        one = schedule.build_plan(p, rotation=schedule.RotationState(),
                                  request_budget=24, today=today,
                                  destinations=["TYO"])
        assert len(one.windows) == 2 * len(both.windows)
        assert one.request_estimate <= 24

    def test_state_survives_a_round_trip_to_disk(self, tmp_path):
        rot = schedule.RotationState()
        rot.record_destinations(["TYO", "OSA"], {"TYO"})
        path = tmp_path / "rotation.json"
        rot.save(path)
        assert schedule.RotationState.load(path).dest_misses == {"TYO": 0, "OSA": 1}


# --------------------------------------------------------------------------
class TestThrottleJudgesOnlyWhereResultsArePossible:
    """A dead destination looks exactly like a throttled connection.

    Counting its empties as evidence of blocking collapsed the budget from 24
    to 14 on the first real run, purely because SJO-Osaka returns nothing.
    """

    class FakeSearcher:
        def __init__(self, per_dest):
            self.requests_by_destination = {d: r for d, (r, _) in per_dest.items()}
            self.barren_by_destination = {d: b for d, (_, b) in per_dest.items()}
            self.requests_made = sum(self.requests_by_destination.values())
            self.barren_requests = sum(self.barren_by_destination.values())

    def found(self, *dests):
        out = []
        for d in dests:
            itin = Itinerary(
                price_usd=1000, legs=[Leg("SJO", "NRT",
                                          __import__("datetime").datetime(2027, 1, 1),
                                          __import__("datetime").datetime(2027, 1, 2), 600)],
                airlines=[], outbound_date=date(2027, 1, 1),
                return_date=date(2027, 1, 15), origin="SJO", destination=d)
            out.append(itin)
        return out

    def test_a_dead_destination_does_not_shrink_the_budget(self):
        from tracker.cli import _throttle_sample
        s = self.FakeSearcher({"TYO": (6, 0), "OSA": (6, 6)})
        empty, judged = _throttle_sample(s, self.found("TYO"))
        assert (empty, judged) == (0, 6), "Osaka's emptiness counted as throttling"

    def test_a_real_block_still_shrinks_it(self):
        from tracker.cli import _throttle_sample
        s = self.FakeSearcher({"TYO": (6, 6), "OSA": (6, 6)})
        empty, judged = _throttle_sample(s, [])
        assert (empty, judged) == (12, 12)

    def test_a_partial_block_is_still_visible(self):
        from tracker.cli import _throttle_sample
        s = self.FakeSearcher({"TYO": (8, 5), "OSA": (4, 4)})
        empty, judged = _throttle_sample(s, self.found("TYO"))
        assert empty / judged > 0.5
