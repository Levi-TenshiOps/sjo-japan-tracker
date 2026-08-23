"""Whole pipeline, offline: search -> filter -> classify -> log -> decide."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime
import pytest

from tracker import history, pricing
from tracker.alerts import CR_TZ, AlertState, decide, record_sent
from tracker.email_render import render
from tracker.itinerary import dedupe, partition
from tracker.search import RouteQuery, Searcher
from tests import fixtures as fx

ALL_RAW = [fx.MEX_OPTION, fx.ZRH_OPTION, fx.YYZ_OPTION, fx.DFW_OPTION]


def pipeline(raws, *, threshold=1380):
    """Search through to a ranked, validated list."""
    s = Searcher(fetch=fx.FakeFetcher(raws), delay=0, sleep=lambda _: None)
    out = s.run(RouteQuery("SJO", "NRT", fx.DEPART_FEB, fx.RETURN_FEB))
    good, bad = partition(out.itineraries, max_total_hours=60, min_layover_min=75)
    return dedupe(good), bad


class TestEndToEnd:
    def test_banned_routes_never_reach_the_email(self):
        """The single most important guarantee in the project."""
        good, bad = pipeline(ALL_RAW)
        codes = {c for i in good for c in i.all_airports}
        assert not (codes & {"DFW", "YYZ", "IAH", "LAX", "PEK"})
        assert len(bad) == 2

    def test_cheapest_first(self):
        good, _ = pipeline(ALL_RAW)
        assert good[0].price_usd == 1290
        assert [i.price_usd for i in good] == sorted(i.price_usd for i in good)

    def test_full_run_produces_sendable_email(self):
        good, _ = pipeline(ALL_RAW)
        qualifying = [i for i in good if i.price_usd <= 1380]
        assert qualifying
        bands = pricing.resolve_bands()
        content = render(qualifying, bands, threshold=1380, is_great=False,
                         generated_at="Aug 22, 2026 at 07:00 Costa Rica time")
        assert "$1,290" in content.html
        assert "https://www.google.com/travel/flights" in content.html
        assert content.subject.startswith("\u2708")

    def test_nothing_qualifies_means_no_email(self):
        """Requirement 1: receiving nothing is a correct outcome."""
        good, _ = pipeline([fx.ZRH_OPTION])   # $1,658 only
        qualifying = [i for i in good if i.price_usd <= 1380]
        assert not qualifying
        d = decide(AlertState(), best_price=good[0].price_usd,
                   best_signature=good[0].signature,
                   good_threshold=1380, great_threshold=1150,
                   now=datetime(2026, 8, 22, 7, tzinfo=CR_TZ))
        assert not d.should_send

    def test_only_banned_results_yields_nothing(self):
        good, bad = pipeline([fx.YYZ_OPTION, fx.DFW_OPTION])
        assert good == [] and len(bad) == 2

    def test_three_runs_one_day_max_two_emails(self):
        """Requirement 1 end to end, across a simulated day."""
        state = AlertState()
        sent = []
        for hour, raws in [(6, [fx.ZRH_OPTION]),          # nothing qualifies
                           (13, [fx.MEX_OPTION]),          # $1,290 -> email 1
                           (19, [fx.MEX_GREAT])]:          # $1,040 -> email 2
            good, _ = pipeline(raws)
            q = [i for i in good if i.price_usd <= 1380]
            now = datetime(2026, 8, 22, hour, tzinfo=CR_TZ)
            d = decide(state, best_price=q[0].price_usd if q else good[0].price_usd,
                       best_signature=q[0].signature if q else None,
                       good_threshold=1380, great_threshold=1150, now=now)
            if d.should_send and q:
                render(q, pricing.resolve_bands(), threshold=1380,
                       is_great=d.is_great, generated_at=str(now))
                record_sent(state, best_price=q[0].price_usd,
                            best_signature=q[0].signature,
                            is_great=d.is_great, now=now)
                sent.append(q[0].price_usd)
        assert sent == [1290, 1040]
        assert state.emails_sent_today == 2

    def test_history_then_bands_switch_source(self, tmp_path):
        csv_path = tmp_path / "history.csv"
        good, _ = pipeline(ALL_RAW)
        bands = pricing.resolve_bands()
        assert bands.source == "SEED"

        for n in range(20):
            for i in good:
                i.price_usd = 1200 + n * 7
            history.append(csv_path, history.rows_from(
                good, band_of=bands.classify, band_source=bands.source))

        prices = history.read_prices(csv_path, origin="SJO")
        assert len(prices) >= pricing.MIN_HISTORY_POINTS
        assert pricing.resolve_bands(history_prices=prices).source == "HISTORY"

    def test_deep_link_survives_the_whole_pipeline(self):
        good, _ = pipeline(ALL_RAW)
        for i in good:
            assert i.deep_link.startswith("https://www.google.com/travel/flights")
            assert "tfs=" in i.deep_link and "curr=USD" in i.deep_link
