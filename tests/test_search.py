"""Query building, deep links, retries, budget."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date
import base64
import pytest

from tracker.search import (
    RouteQuery, Searcher, build_query, deep_link, extract_google_bands,
    plan_broad, plan_hub_sweep,
)
from tests import fixtures as fx

RQ = RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN, hub="MEX")


class TestQueryBuilding:
    def test_round_trip_has_two_legs(self):
        q = build_query(RQ)
        assert len(q.flight_data) == 2

    def test_one_way_has_one_leg(self):
        q = build_query(RouteQuery("SJO", "NRT", fx.DEPART, None))
        assert len(q.flight_data) == 1

    def test_currency_forced_to_usd(self):
        """Google defaults to CRC from a Costa Rican IP; we must override."""
        assert build_query(RQ).currency == "USD"
        assert "curr=USD" in deep_link(RQ)

    def test_language_english(self):
        assert "hl=en" in deep_link(RQ)

    def test_hub_encoded_in_payload(self):
        raw = build_query(RQ).to_bytes()
        assert b"MEX" in raw

    def test_dates_encoded(self):
        raw = build_query(RQ).to_bytes()
        assert b"2027-01-15" in raw and b"2027-01-24" in raw


class TestDeepLink:
    def test_is_google_flights_url(self):
        link = deep_link(RQ)
        assert link.startswith("https://www.google.com/travel/flights")
        assert "tfs=" in link

    def test_tfs_is_valid_base64(self):
        tfs = deep_link(RQ).split("tfs=")[1].split("&")[0]
        assert base64.b64decode(tfs + "==")

    def test_distinct_queries_distinct_links(self):
        a = deep_link(RQ)
        b = deep_link(RouteQuery("SJO", "HND", fx.DEPART, fx.RETURN, hub="MEX"))
        assert a != b

    def test_link_attached_to_results(self):
        s = Searcher(fetch=fx.FakeFetcher(), delay=0, sleep=lambda _: None)
        out = s.run(RQ)
        assert out.itineraries[0].deep_link.startswith("https://www.google.com")


class TestPlanning:
    def test_broad_is_product_of_inputs(self):
        qs = plan_broad(origins=["SJO"], destinations=["NRT", "HND", "KIX"],
                        date_pairs=[(fx.DEPART, fx.RETURN), (fx.DEPART_FEB, fx.RETURN_FEB)])
        assert len(qs) == 6
        assert all(q.hub is None for q in qs)

    def test_hub_sweep_covers_each_hub(self):
        qs = plan_hub_sweep(origins=["SJO"], destinations=["NRT"],
                            date_pairs=[(fx.DEPART, fx.RETURN)],
                            hubs=["MEX", "ZRH", "MAD"])
        assert {q.hub for q in qs} == {"MEX", "ZRH", "MAD"}

    def test_banned_hubs_never_planned(self):
        """Even if someone puts a US hub in config, no request is made for it."""
        qs = plan_hub_sweep(origins=["SJO"], destinations=["NRT"],
                            date_pairs=[(fx.DEPART, fx.RETURN)],
                            hubs=["MEX", "DFW", "YYZ", "IAH", "PEK"])
        assert {q.hub for q in qs} == {"MEX"}


class TestSearcher:
    def test_happy_path(self):
        s = Searcher(fetch=fx.FakeFetcher([fx.MEX_OPTION]), delay=0, sleep=lambda _: None)
        out = s.run(RQ)
        assert out.ok and len(out.itineraries) == 1

    def test_caches_repeat_queries(self):
        fetcher = fx.FakeFetcher()
        s = Searcher(fetch=fetcher, delay=0, sleep=lambda _: None)
        s.run(RQ); s.run(RQ); s.run(RQ)
        assert len(fetcher.calls) == 1

    def test_retries_then_succeeds(self):
        fetcher = fx.FakeFetcher(fail_times=2)
        s = Searcher(fetch=fetcher, delay=0, sleep=lambda _: None)
        out = s.run(RQ)
        assert out.ok and len(fetcher.calls) == 3

    def test_gives_up_after_max_retries(self):
        fetcher = fx.FakeFetcher(fail_times=99)
        s = Searcher(fetch=fetcher, delay=0, max_retries=3, sleep=lambda _: None)
        out = s.run(RQ)
        assert not out.ok and "throttled" in out.error
        assert len(fetcher.calls) == 3

    def test_empty_results_recorded_as_error(self):
        s = Searcher(fetch=fx.FakeFetcher([]), delay=0, sleep=lambda _: None)
        assert s.run(RQ).error == "no results"

    def test_request_budget_enforced(self):
        fetcher = fx.FakeFetcher()
        s = Searcher(fetch=fetcher, delay=0, max_requests=2, sleep=lambda _: None)
        qs = [RouteQuery("SJO", "NRT", fx.DEPART, fx.RETURN, hub=h)
              for h in ("MEX", "ZRH", "MAD", "AMS", "DOH")]
        outs = s.run_all(qs)
        assert len(fetcher.calls) == 2
        assert sum(1 for o in outs if o.error == "request budget exhausted") == 3

    def test_backoff_sleeps_grow(self):
        slept = []
        s = Searcher(fetch=fx.FakeFetcher(fail_times=99), delay=2,
                     max_retries=3, sleep=slept.append)
        s.run(RQ)
        waits = [x for x in slept if x >= 2]
        assert waits[0] < waits[1]

    def test_malformed_results_skipped_not_crashed(self):
        s = Searcher(fetch=fx.FakeFetcher([fx.EMPTY_OPTION, fx.MEX_OPTION]),
                     delay=0, sleep=lambda _: None)
        assert len(s.run(RQ).itineraries) == 1


class TestGoogleBands:
    def test_garbage_returns_none(self):
        for bad in ("", "<html></html>", "not html at all", None):
            assert extract_google_bands(bad) is None

    def test_absurd_numbers_rejected(self):
        html = 'script class="ds:1" data:[[1,2],[3,4]],'
        assert extract_google_bands(html) is None

    def test_never_raises(self):
        """Undocumented structure: it may fail, but must fail quietly."""
        for junk in ("data:[[[", 'ds:1 data:[{"a":1}],', "ds:1", "data:"):
            extract_google_bands(junk)  # no exception
