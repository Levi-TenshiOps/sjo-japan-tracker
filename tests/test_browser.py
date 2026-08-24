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
    BrowserOption, chrome_path, claimed_result_count, dom_price_order,
    dom_row_count,
    fetch_dom, parse_options,
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


class TestGooglesOwnRowOrderDecidesWhetherTruncationMatters:
    """Whether the unreachable rows are the dear ones is an ordering question.

    `parse_options` sorts by price, which destroys the only evidence that
    answers it. Read from the DOM instead - it is already in hand, so this
    costs no requests against an IP that has only just recovered from a
    throttle. The alternative, re-querying with `max_price` caps, does.

    If Google's list is price-ascending, the rows behind the un-clickable
    "View more flights" control are the most expensive and a shortfall
    cannot cost a cheap fare. If it is "Best" order - a blend of price and
    duration - a cheap slow fare could sit below the fold.
    """

    def test_the_captured_page_is_price_ascending(self):
        html = io.open(FIXTURE, encoding="utf-8").read()
        order = dom_price_order(html)
        assert order == [1863, 1948, 2526, 2652, 6512]
        assert order == sorted(order)

    def test_it_reads_the_dom_order_not_the_sorted_output(self):
        """The whole point: parse_options would have hidden this."""
        html = io.open(FIXTURE, encoding="utf-8").read()
        assert dom_price_order(html) == [
            o.price_usd for o in parse_options(
                html, origin="SJO", destination="NRT",
                depart_date=DEPART, return_date=RETURN)], (
            "on this page the two agree, which is exactly why a page where "
            "they disagree is the interesting one to catch")

    def test_empty_and_junk_are_empty_lists_not_crashes(self):
        assert dom_price_order("") == []
        assert dom_price_order("<html><body>nothing</body></html>") == []


class TestTheDetectorCannotManufactureGoodNews:
    """A detector that reports "fine" when it is blind is worse than none.

    Found reviewing my own code an hour after writing it, 2026-08-24.
    `dom_price_order` used a single selector where `parse_options` has a
    fallback chain, so a Google restyle would return [] while the parser
    carried on - and `[] == sorted([])` is True, so the log would have said
    "row order ascending" for a page it could not read at all. That is the
    reassuring answer, invented out of a parsing failure, about the one
    question the instrumentation exists to answer.
    """

    FALLBACK = ('<html><body><ul class="Rk10dc"><li>'
                '<div aria-label="From 1500 US dollars round trip total. 1 stop">a</div>'
                '</li><li>'
                '<div aria-label="From 1200 US dollars round trip total. 1 stop">b</div>'
                '</li></ul></body></html>')

    def test_it_follows_the_same_fallbacks_as_the_parser(self):
        assert dom_price_order(self.FALLBACK) == [1500, 1200]

    def test_it_agrees_with_the_parser_on_the_real_page(self):
        """On synthetic markup they legitimately differ - `parse_options`
        needs a price *and* an airline to build an option, this needs only a
        price. On a real page every row has both, so they must agree."""
        html = io.open(FIXTURE, encoding="utf-8").read()
        parsed = parse_options(html, origin="SJO", destination="NRT",
                               depart_date=DEPART, return_date=RETURN)
        assert len(dom_price_order(html)) == len(parsed)

    def test_reading_more_rows_than_the_parser_is_safe(self):
        """It answers a question about Google's ordering, not about which
        options are usable, so a row the parser rejected still counts."""
        assert len(dom_price_order(self.FALLBACK)) == 2

    def test_a_page_it_cannot_read_is_empty_not_ascending(self):
        order = dom_price_order("<html><body>nothing here</body></html>")
        assert order == []
        assert order == sorted(order), (
            "this is the trap: an empty list IS sorted, so the caller must "
            "check for emptiness before calling it ascending")

    def test_the_real_page_still_reads(self):
        html = io.open(FIXTURE, encoding="utf-8").read()
        assert len(dom_price_order(html)) == 5


class TestWhyARowIsSkipped:
    """The truncation gap has two halves and only one of them is a problem.

    "Google claims 16, we parsed 13" was being read as three fares lost
    behind the un-clickable "View more flights" control. There are two other
    possibilities, and they are not the same thing at all:

    * the row is in the DOM and its aria-label does not match - a parser
      bug, fixable for free, and genuinely costing fares;
    * the row is a duplicate of one already taken - same price, routing,
      airline and duration - which for the purpose of finding the cheapest
      fare is the same deal twice, and costs nothing.

    Counting them apart is what turns a worrying number into a decidable
    one.
    """

    def test_stats_are_optional(self):
        html = io.open(FIXTURE, encoding="utf-8").read()
        parse_options(html, origin="SJO", destination="NRT",
                      depart_date=DEPART, return_date=RETURN)   # no stats

    def test_duplicates_are_counted_not_silent(self):
        """Two rows identical in every field the fingerprint uses."""
        row = ('<li class="pIav2d"><div aria-label="From 1500 US dollars '
               'round trip total. 1 stop flight with SWISS. '
               'Total duration 12 hr 30 min.">1 stop in ZRH</div></li>')
        html = f"<html><body><ul class='Rk10dc'>{row}{row}</ul></body></html>"
        stats = {}
        opts = parse_options(html, origin="SJO", destination="NRT",
                             depart_date=DEPART, return_date=RETURN,
                             stats=stats)
        assert len(opts) == 1
        assert stats.get("duplicate") == 1
        assert dom_row_count(html) == 2

    def test_an_unreadable_row_is_counted_apart_from_a_duplicate(self):
        """This is the half that would be a real loss."""
        good = ('<li class="pIav2d"><div aria-label="From 1500 US dollars '
                'round trip total. 1 stop flight with SWISS. '
                'Total duration 12 hr 30 min.">1 stop in ZRH</div></li>')
        junk = '<li class="pIav2d"><div aria-label="something else">x</div></li>'
        html = f"<html><body><ul class='Rk10dc'>{good}{junk}</ul></body></html>"
        stats = {}
        parse_options(html, origin="SJO", destination="NRT",
                      depart_date=DEPART, return_date=RETURN, stats=stats)
        assert stats.get("unmatched") == 1
        assert stats.get("duplicate") is None

    def test_the_real_page_loses_nothing(self):
        """Five rows in, five options out, nothing skipped for either reason."""
        html = io.open(FIXTURE, encoding="utf-8").read()
        stats = {}
        opts = parse_options(html, origin="SJO", destination="NRT",
                             depart_date=DEPART, return_date=RETURN,
                             stats=stats)
        assert len(opts) == dom_row_count(html) == 5
        assert stats == {}
