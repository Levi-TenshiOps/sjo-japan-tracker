"""What the hand-kept allow list costs, measured rather than guessed.

`sweep_history.csv` records what we accept, so the cost of the visa filter
has been invisible: a fare refused for a US transit and a fare refused
because nobody has researched Orly left the same trace, which is none.

The first is refused for ever. The second is a gap in a hand-kept list -
Costa Rica has visa-free Schengen access, and CDG is on the list while ORY
is not, FRA and MUC while BER and HAM are not. Immigration is national;
the list is per-airport.

Nothing in this feature changes what is allowed.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.airports import ban_reason, is_unresearched
from tracker.browser import BrowserOption
from tracker.sweeper import MAX_REJECTED_HUBS, SweepStore, _note_unresearched


def opt(price, stops):
    """`visa_ok` is derived from `stops`, so these exercise the real rule."""
    return BrowserOption(
        price_usd=price, origin="SJO", destination="TYO",
        depart_date=date(2027, 1, 29), return_date=date(2027, 2, 25),
        stops=tuple(stops), airlines=("X",), total_minutes=2780,
        deep_link="")


class TestItSeparatesForeverFromNotYet:
    def test_a_us_transit_is_refused_for_ever(self):
        assert ban_reason("LAX") and not is_unresearched("LAX")

    def test_a_canadian_transit_is_refused_for_ever(self):
        assert ban_reason("YYZ") and not is_unresearched("YYZ")

    def test_an_unlisted_schengen_airport_is_merely_unresearched(self):
        for code in ("ORY", "BER", "HAM", "KEF", "BUD"):
            assert is_unresearched(code), code

    def test_a_listed_hub_is_neither(self):
        for code in ("CDG", "FRA", "ZRH", "AMS"):
            assert not ban_reason(code) and not is_unresearched(code), code


class TestRecording:
    def test_an_unresearched_hub_is_recorded_with_its_price(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY"])])
        assert s.rejected_unknown["ORY"] == {"n": 1, "min": 1400}

    def test_it_keeps_the_cheapest_and_counts_every_sighting(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY"]), opt(1200, ["ORY"]),
                               opt(1900, ["ORY"])])
        assert s.rejected_unknown["ORY"] == {"n": 3, "min": 1200}

    def test_an_accepted_fare_is_not_recorded(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["CDG"])])
        assert s.rejected_unknown == {}

    def test_a_fare_also_touching_the_us_is_not_recoverable(self):
        """Researching Orly would not rescue an itinerary via Miami."""
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY", "MIA"])])
        assert s.rejected_unknown == {}

    def test_every_unresearched_hub_on_one_itinerary_is_counted(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY", "BER"])])
        assert set(s.rejected_unknown) == {"ORY", "BER"}

    def test_a_listed_hub_beside_an_unlisted_one_is_still_recorded(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["CDG", "ORY"])])
        assert set(s.rejected_unknown) == {"ORY"}

    def test_it_does_not_grow_without_bound(self):
        s = SweepStore()
        for i in range(MAX_REJECTED_HUBS + 20):
            _note_unresearched(s, [opt(1400, [f"Z{i:02d}"])])
        assert len(s.rejected_unknown) <= MAX_REJECTED_HUBS

    def test_a_known_hub_still_gets_counted_after_the_cap(self):
        """The cap must not stop an existing entry being updated."""
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY"])])
        for i in range(MAX_REJECTED_HUBS + 20):
            _note_unresearched(s, [opt(1400, [f"Z{i:02d}"])])
        _note_unresearched(s, [opt(900, ["ORY"])])
        assert s.rejected_unknown["ORY"]["min"] == 900


class TestItChangesNothingAboutWhatIsAllowed:
    def test_recording_does_not_make_a_hub_legal(self):
        s = SweepStore()
        _note_unresearched(s, [opt(1400, ["ORY"])])
        assert ban_reason("ORY"), "recording a hub must not permit it"

    def test_the_us_deny_list_is_untouched(self):
        from tracker.airports import BANNED_AIRPORTS
        for code in ("LAX", "JFK", "MIA", "IAH", "DFW", "ANC", "YYZ", "YVR"):
            assert code in BANNED_AIRPORTS, code
