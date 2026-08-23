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
