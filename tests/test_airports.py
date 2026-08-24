"""The deny list is the safety-critical asset. Guard it."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

from tracker.airports import (
    BANNED_AIRPORTS, CANADA_AIRPORTS, HUBS, HUBS_BY_CODE, US_AIRPORTS,
    ban_reason, describe_hub, is_banned, usable_hubs,
)


class TestDenyList:
    @pytest.mark.parametrize("code", [
        "MIA", "IAH", "DFW", "LAX", "JFK", "ATL", "ORD", "EWR", "SFO",
        "PHX", "CLT", "MCO", "FLL", "IAD", "SEA", "LAS", "DEN", "HNL", "SJU",
    ])
    def test_us_hubs_banned(self, code):
        assert is_banned(code) and "C-1" in ban_reason(code)

    @pytest.mark.parametrize("code", ["YYZ", "YUL", "YVR", "YYC", "YOW", "YEG"])
    def test_canadian_hubs_banned(self, code):
        assert is_banned(code) and "Canadian" in ban_reason(code)

    @pytest.mark.parametrize("code", ["PEK", "PVG", "CAN", "PKX"])
    def test_mainland_china_banned(self, code):
        assert is_banned(code)

    def test_case_insensitive(self):
        assert is_banned("dfw") and is_banned("Dfw") and is_banned("DFW")

    def test_every_us_gateway_from_sjo_is_covered(self):
        """The routes Google actually offers from SJO must all be blocked."""
        for code in ("MIA", "IAH", "DFW", "LAX", "JFK", "ATL", "EWR", "ORD",
                     "CLT", "PHX", "MCO", "FLL", "IAD", "DEN", "SFO", "LAS"):
            assert code in US_AIRPORTS, f"{code} missing from US list"


class TestAllowList:
    @pytest.mark.parametrize("code", [
        "MEX", "ZRH", "MAD", "AMS", "CDG", "FRA", "IST", "DOH", "DXB",
        "ICN", "SIN", "HKG", "PTY", "BOG", "GRU", "LHR",
    ])
    def test_usable_hubs_not_banned(self, code):
        assert not is_banned(code)
        assert code in HUBS_BY_CODE

    def test_no_hub_is_also_banned(self):
        """A contradiction here would silently produce zero results."""
        overlap = {h.code for h in HUBS} & BANNED_AIRPORTS
        assert not overlap, f"hub also on deny list: {overlap}"

    def test_ban_reason_none_for_clean(self):
        assert ban_reason("MEX") is None

    def test_tiers(self):
        assert HUBS_BY_CODE["MEX"].tier == "FREE"
        assert HUBS_BY_CODE["LHR"].tier == "LIGHT"   # UK ETA
        assert HUBS_BY_CODE["ICN"].tier == "LIGHT"   # K-ETA

    def test_free_tier_excludes_light(self):
        codes = {h.code for h in usable_hubs("FREE")}
        assert "MEX" in codes and "LHR" not in codes and "ICN" not in codes

    def test_light_tier_includes_both(self):
        codes = {h.code for h in usable_hubs("LIGHT")}
        assert {"MEX", "LHR", "ICN"} <= codes

    def test_all_hubs_usable(self):
        assert all(h.usable for h in HUBS)

    def test_describe(self):
        assert describe_hub("ZRH") == "Zurich (ZRH)"
        assert describe_hub("XXX") == "XXX"

    def test_hub_codes_unique(self):
        codes = [h.code for h in HUBS]
        assert len(codes) == len(set(codes))


class TestUnknownAirportsFailClosed:
    """Silence must not read as approval. Safety-critical.

    Audited 2026-08-23 against a list of real US and Canadian airports: 50
    of them - Anchorage and Fairbanks among them - were not on the deny list
    and came back clean, because `ban_reason` was a pure deny list and
    returned None for anything nobody had thought to add. A Costa Rican
    passport needs a C-1 for every one of them, and the traveller would
    discover that at the SJO gate.
    """

    @pytest.mark.parametrize("code", [
        "ANC", "FAI", "JNU", "KTN", "SIT", "BET", "OTZ", "OME", "ADQ",
        "ABQ", "TUL", "BHM", "GEG", "ROC", "SYR", "TYS", "ALB", "HSV",
        "ICT", "ISP", "LEX", "MHT", "MYR", "PNS", "PSP", "SBA", "SRQ",
        "TLH", "TVC", "XNA"])
    def test_the_us_airports_that_were_missing(self, code):
        assert ban_reason(code), f"{code} is in the United States"

    @pytest.mark.parametrize("code", [
        "YQT", "YZF", "YXY", "YFB", "YQM", "YSJ", "YYT", "YDF", "YQX",
        "YZV", "YBG", "YQY", "YAM", "YTS", "YXU", "YQG", "YXC", "YXS",
        "YPR", "YZP"])
    def test_the_canadian_airports_that_were_missing(self, code):
        assert ban_reason(code), f"{code} is in Canada"

    def test_a_code_nobody_has_researched_is_rejected(self):
        reason = ban_reason("XQZ")
        assert reason and "unverified" in reason

    def test_the_rejection_says_what_to_do_about_it(self):
        """A rejection nobody can action is a rejection nobody will fix."""
        assert "HUBS" in ban_reason("XQZ")

    def test_every_researched_hub_is_still_allowed(self):
        for hub in HUBS:
            assert ban_reason(hub.code) is None, hub.code

    def test_the_route_endpoints_are_allowed(self):
        """Rejecting SJO or Tokyo would reject literally every itinerary."""
        for code in ("SJO", "NRT", "HND", "KIX", "ITM", "TYO", "OSA"):
            assert ban_reason(code) is None, code

    @pytest.mark.parametrize("code", [
        "MEX", "MTY", "FRA", "CDG", "ZRH", "PVR", "PTY", "MAD", "IST",
        "LIR", "KUL", "SAL", "AMS", "ICN", "DOH"])
    def test_every_hub_ever_actually_observed_is_allowed(self, code):
        """The 15 connecting airports that appear in the real history.

        Four of them - MTY, PVR, LIR and SAL - were passing only because
        nothing had banned them. LIR is Costa Rica's own second airport.
        """
        assert ban_reason(code) is None, code

    def test_it_reaches_the_browser_path(self):
        """Both visa call sites reject on any non-None reason."""
        from tracker.browser import BrowserOption
        from datetime import date
        o = BrowserOption(price_usd=900, origin="SJO", destination="TYO",
                          depart_date=date(2027, 1, 1),
                          return_date=date(2027, 1, 28),
                          stops=("ANC",), airlines=("X",), total_minutes=1000,
                          deep_link="")
        assert not o.visa_ok
        assert "ANC" in o.banned_reason
