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
    BrowserOption, chrome_path, claimed_result_count, fetch_dom, parse_options,
    unreadable_count, visa_free,
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


class TestWeCanTellWhenAWindowWasUnderCollected:
    """Two ways to miss a fare on a window we *did* check, both invisible.

    The trip owner asked for 100% of applicable flights, not 99%. Coverage
    of *windows* is now guaranteed by the check ledger, but that says
    nothing about completeness *within* a window:

    * Google states its own count ("16 results returned") and the page has a
      "View more flights" control `--dump-dom` cannot click. Measured
      2026-08-23 a live page claimed 16 while the parser found 13, and
      nothing recorded the gap.
    * An option whose routing cannot be read is dropped, because
      `banned_reason` fails closed. Right for safety, wrong to do silently -
      it may have been a bookable fare.

    Neither is fixed by detecting it. Both stop being unknowable.
    """

    @pytest.mark.parametrize("text,expected", [
        ("16 results returned", 16),
        ("About 42 results found", 42),
        ("1 result returned", 1),
        ("no count anywhere here", None),
        ("9999 results returned", None),      # implausible, ignored
        ("0 results returned", None),
        ("", None),
    ])
    def test_the_claimed_count_is_read_when_present(self, text, expected):
        assert claimed_result_count(text) == expected

    def test_a_shortfall_is_detectable(self):
        dom = "<html><body>16 results returned</body></html>"
        assert claimed_result_count(dom) == 16       # vs however many parse

    def test_unreadable_routings_are_counted_apart_from_visa_rejections(self):
        readable_banned = BrowserOption(
            price_usd=900, origin="SJO", destination="TYO",
            depart_date=date(2027, 1, 1), return_date=date(2027, 1, 28),
            stops=("DFW",), airlines=("X",), total_minutes=1000, deep_link="")
        unreadable = BrowserOption(
            price_usd=900, origin="SJO", destination="TYO",
            depart_date=date(2027, 1, 1), return_date=date(2027, 1, 28),
            stops=("??",), airlines=("X",), total_minutes=1000, deep_link="")
        assert not readable_banned.visa_ok and not unreadable.visa_ok
        assert unreadable_count([readable_banned, unreadable]) == 1, (
            "a visa rejection is a correct decision; an unreadable routing "
            "is a fare we could not judge, and they must not be conflated")

    def test_a_clean_page_reports_nothing_to_worry_about(self, options):
        """The real captured DOM: every routing readable, nothing dropped
        for being unverifiable."""
        assert unreadable_count(options) == 0
