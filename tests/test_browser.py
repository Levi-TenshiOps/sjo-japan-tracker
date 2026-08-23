"""The Chrome fallback for stays plain HTTP cannot see.

`tests/chrome_dom_32n.html` is a real rendered DOM, trimmed to five rows:
SJO-NRT departing 2027-02-05, returning 2027-03-09 (32 nights), a window
that returns a completely empty page over plain HTTP. Rows were chosen to
cover a Canadian hub, three US hubs and one Mexican two-stop routing, so
the visa rule is exercised in both directions.

Nothing here touches the network or launches Chrome.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
from datetime import date

import pytest

from tracker.browser import (
    BrowserOption, chrome_path, fetch_dom, parse_options, visa_free,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")
DEPART, RETURN = date(2027, 2, 5), date(2027, 3, 9)


@pytest.fixture
def options():
    html = io.open(FIXTURE, encoding="utf-8").read()
    return parse_options(html, origin="SJO", destination="NRT",
                         depart_date=DEPART, return_date=RETURN)


class TestParsingRealDom:
    def test_every_row_is_read(self, options):
        assert len(options) == 5

    def test_cheapest_first(self, options):
        assert [o.price_usd for o in options] == sorted(o.price_usd for o in options)

    def test_prices_match_the_page(self, options):
        assert [o.price_usd for o in options] == [1863, 1948, 2526, 2652, 6512]

    def test_single_stop_airport_is_read_as_iata(self, options):
        assert options[0].stops == ("YYZ",)

    def test_two_stop_routing_keeps_both_airports_in_order(self, options):
        mex = next(o for o in options if o.price_usd == 2652)
        assert mex.stops == ("MEX", "MTY")
        assert mex.stop_count == 2

    def test_airline_is_a_display_name_not_a_code(self, options):
        assert options[0].airlines == ("Air Canada",)

    def test_codeshare_splits_into_both_carriers(self, options):
        alaska = next(o for o in options if o.price_usd == 6512)
        assert alaska.airlines == ("Alaska", "ANA")

    def test_total_duration_is_minutes(self, options):
        assert options[0].total_minutes == 40 * 60 + 45

    def test_nights_come_from_the_requested_window(self, options):
        assert all(o.nights == 32 for o in options)

    def test_route_label_reads_end_to_end(self, options):
        mex = next(o for o in options if o.price_usd == 2652)
        assert mex.route_label == "SJO - MEX - MTY - NRT"

    def test_empty_html_is_no_options_not_a_crash(self):
        assert parse_options("", origin="SJO", destination="NRT",
                             depart_date=DEPART, return_date=RETURN) == []

    def test_unrelated_html_yields_nothing(self):
        assert parse_options("<html><body><p>hello</p></body></html>",
                             origin="SJO", destination="NRT",
                             depart_date=DEPART, return_date=RETURN) == []


class TestVisaRuleIsEnforcedHereToo:
    """Non-negotiable #1 applies to this path exactly as to the HTTP one."""

    def test_canadian_transit_is_rejected(self, options):
        yyz = next(o for o in options if o.stops == ("YYZ",))
        assert not yyz.visa_ok
        assert "YYZ" in yyz.banned_reason and "Canadian" in yyz.banned_reason

    @pytest.mark.parametrize("code", ["DFW", "IAH", "LAX"])
    def test_us_transit_is_rejected(self, options, code):
        opt = next(o for o in options if code in o.stops)
        assert not opt.visa_ok
        assert "C-1" in opt.banned_reason

    def test_mexico_routing_is_allowed(self, options):
        mex = next(o for o in options if o.stops == ("MEX", "MTY"))
        assert mex.visa_ok and mex.banned_reason is None

    def test_only_one_of_five_survives(self, options):
        assert [o.price_usd for o in visa_free(options)] == [2652]

    def test_cheapest_option_does_not_survive(self, options):
        """The $1,863 headline fare routes through Toronto and is unflyable."""
        assert options[0].price_usd == 1863
        assert options[0] not in visa_free(options)

    def test_unknown_routing_is_never_treated_as_clean(self):
        """A parse that loses the airports must fail closed, not open."""
        opt = BrowserOption(price_usd=999, origin="SJO", destination="NRT",
                            depart_date=DEPART, return_date=RETURN,
                            stops=("???",), airlines=("X",), total_minutes=100)
        assert not opt.visa_ok

    def test_nonstop_has_no_stops_and_is_allowed(self):
        opt = BrowserOption(price_usd=999, origin="SJO", destination="NRT",
                            depart_date=DEPART, return_date=RETURN,
                            stops=(), airlines=("X",), total_minutes=100)
        assert opt.visa_ok


class TestChromeDiscovery:
    def test_explicit_override_must_exist(self):
        assert chrome_path("/definitely/not/here/chrome") is None

    def test_override_is_returned_when_present(self, tmp_path):
        fake = tmp_path / "chrome.exe"
        fake.write_text("")
        assert chrome_path(str(fake)) == str(fake)

    def test_fetch_returns_empty_when_chrome_is_missing(self):
        """A broken Chrome must degrade to "no options", never raise."""
        assert fetch_dom("https://example.com",
                         chrome="/definitely/not/here/chrome", timeout=5) == ""
