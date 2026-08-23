"""Duration maths and the visa safety net."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime

from tracker.itinerary import (
    Leg, build_itinerary, dedupe, format_duration, layover_minutes,
    partition, segment_duration, split_outbound, validate,
)
from tests import fixtures as fx


def make(raw, dest="NRT", dep=None, ret=None):
    return build_itinerary(
        raw, origin="SJO", destination=dest,
        outbound_date=dep or fx.DEPART, return_date=ret or fx.RETURN,
        deep_link="https://example.test/x",
    )


class TestDuration:
    def test_matches_google_46hr20(self):
        """The real ZRH itinerary from the screenshot: Google says 46 hr 20 min."""
        itin = make(fx.ZRH_OPTION)
        assert itin.outbound_duration_min == 2780
        assert format_duration(itin.outbound_duration_min) == "46 hr 20 min"

    def test_layover_counted(self):
        legs = make(fx.ZRH_OPTION).outbound_legs
        assert layover_minutes(legs[0], legs[1]) == 1350  # 22 hr 30 min

    def test_sums_legs_plus_layovers(self):
        legs = make(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB).outbound_legs
        # 195 flown + 700 layover + 875 flown
        assert segment_duration(legs) == 195 + 700 + 875

    def test_single_leg_has_no_layover(self):
        legs = [Leg("SJO", "NRT", datetime(2027, 1, 1, 9), datetime(2027, 1, 2, 5), 900)]
        assert segment_duration(legs) == 900

    def test_empty(self):
        assert segment_duration([]) == 0

    def test_negative_layover_ignored(self):
        a = Leg("SJO", "MEX", datetime(2027, 1, 1, 9), datetime(2027, 1, 1, 14), 300)
        b = Leg("MEX", "NRT", datetime(2027, 1, 1, 11), datetime(2027, 1, 2, 5), 900)
        assert layover_minutes(a, b) == 0

    def test_absurd_layover_ignored(self):
        a = Leg("SJO", "MEX", datetime(2027, 1, 1, 9), datetime(2027, 1, 1, 14), 300)
        b = Leg("MEX", "NRT", datetime(2027, 3, 1, 11), datetime(2027, 3, 2, 5), 900)
        assert layover_minutes(a, b) == 0

    @pytest.mark.parametrize("mins,text", [
        (0, "\u2014"), (45, "45 min"), (60, "1 hr"),
        (2780, "46 hr 20 min"), (1200, "20 hr"),
    ])
    def test_formatting(self, mins, text):
        assert format_duration(mins) == text


class TestVisaSafety:
    def test_canada_rejected(self):
        reason = validate(make(fx.YYZ_OPTION))
        assert reason and "YYZ" in reason and "Canadian" in reason

    def test_us_rejected(self):
        reason = validate(make(fx.DFW_OPTION, dest="HND"))
        assert reason and "DFW" in reason and "C-1" in reason

    def test_mexico_allowed(self):
        assert validate(make(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB)) is None

    def test_zurich_allowed(self):
        assert validate(make(fx.ZRH_OPTION)) is None

    def test_partition_splits_correctly(self):
        raws = [fx.ZRH_OPTION, fx.YYZ_OPTION, fx.DFW_OPTION, fx.MEX_OPTION]
        itins = [make(r) for r in raws]
        good, bad = partition(itins)
        assert len(good) == 2 and len(bad) == 2
        assert all("YYZ" not in i.all_airports for i in good)
        assert all("DFW" not in i.all_airports for i in good)

    def test_banned_hub_never_survives_even_if_cheapest(self):
        """A banned routing must not win on price."""
        cheap_banned = fx.FakeFlights(
            price=1, airlines=["X"], flights=list(fx.DFW_OPTION.flights)
        )
        good, bad = partition([make(cheap_banned, dest="HND"), make(fx.ZRH_OPTION)])
        assert len(good) == 1
        assert good[0].price_usd == 1658

    def test_duration_cap(self):
        reason = validate(make(fx.ZRH_OPTION), max_total_hours=30)
        assert reason and "over the 30 hr cap" in reason

    def test_duration_cap_generous_passes(self):
        assert validate(make(fx.ZRH_OPTION), max_total_hours=60) is None

    def test_tight_layover_rejected(self):
        itin = make(fx.TIGHT_OPTION, dep=fx.DEPART_FEB, ret=None)
        reason = validate(itin, min_layover_min=75)
        assert reason and "too tight" in reason

    def test_tight_layover_allowed_when_guard_off(self):
        itin = make(fx.TIGHT_OPTION, dep=fx.DEPART_FEB, ret=None)
        assert validate(itin, min_layover_min=0) is None


class TestParsing:
    def test_hubs_exclude_endpoints(self):
        itin = make(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB)
        assert itin.hubs == ["MEX"]

    def test_outbound_split(self):
        itin = make(fx.ZRH_OPTION)
        assert itin.outbound_leg_count == 2
        assert len(itin.return_legs) == 2
        assert itin.stops_outbound == 1

    def test_split_outbound_helper(self):
        legs = make(fx.ZRH_OPTION).legs
        assert split_outbound(legs, "NRT") == 2

    def test_empty_flights_returns_none(self):
        assert make(fx.EMPTY_OPTION) is None

    def test_zero_price_returns_none(self):
        assert make(fx.NO_PRICE_OPTION) is None

    def test_labels(self):
        itin = make(fx.ZRH_OPTION)
        assert itin.stops_label == "1 stop"
        assert "Zurich (ZRH)" in itin.via_label
        assert itin.route_label == "SJO\u2013NRT"
        assert "SWISS" in itin.airlines_label

    def test_signature_stable_and_distinct(self):
        a = make(fx.ZRH_OPTION)
        b = make(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB)
        assert a.signature == make(fx.ZRH_OPTION).signature
        assert a.signature != b.signature


class TestDedupe:
    def test_keeps_cheapest_per_signature(self):
        a = make(fx.ZRH_OPTION)
        b = make(fx.ZRH_OPTION); b.price_usd = 1400
        assert [i.price_usd for i in dedupe([a, b])] == [1400]

    def test_sorted_by_price(self):
        out = dedupe([
            make(fx.ZRH_OPTION),
            make(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB),
        ])
        assert [i.price_usd for i in out] == [1290, 1658]


class TestDestinationSanity:
    """Regression: an itinerary that never reaches the requested airport was
    silently treated as one giant outbound leg."""

    def test_never_reaches_destination_rejected(self):
        itin = make(fx.ZRH_OPTION, dest="HND")   # legs only ever touch NRT
        reason = validate(itin)
        assert reason == "never reaches HND"

    def test_rejected_before_duration_is_trusted(self):
        itin = make(fx.ZRH_OPTION, dest="HND")
        assert validate(itin, max_total_hours=500) is not None

    def test_wrong_origin_rejected(self):
        itin = make(fx.ZRH_OPTION)
        itin.origin = "LIR"
        assert validate(itin) == "does not start at LIR"

    def test_correct_destination_passes(self):
        assert validate(make(fx.ZRH_OPTION, dest="NRT")) is None
