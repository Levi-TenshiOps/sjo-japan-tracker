"""The email must contain price, duration and a working link for every row."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re
from datetime import date
import pytest

from tracker.email_render import (
    DEFAULT_ROWS, build_subject, rank_for_email, render, render_html, render_text,
)
from tracker.itinerary import build_itinerary
from tracker.pricing import SEED_BANDS, PriceBands
from tests import fixtures as fx


def itin(raw, dest="NRT", dep=None, ret=None, link="https://www.google.com/travel/flights?tfs=ABC"):
    return build_itinerary(raw, origin="SJO", destination=dest,
                           outbound_date=dep or fx.DEPART,
                           return_date=ret or fx.RETURN, deep_link=link)


@pytest.fixture
def items():
    return [
        itin(fx.MEX_OPTION, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB,
             link="https://www.google.com/travel/flights?tfs=MEX1"),
        itin(fx.ZRH_OPTION, link="https://www.google.com/travel/flights?tfs=ZRH1"),
    ]


@pytest.fixture
def content(items):
    return render(items, SEED_BANDS, threshold=1380, is_great=False,
                  generated_at="Aug 22, 2026 at 07:00 Costa Rica time")


class TestRequiredFields:
    """Requirement 2: exact USD price, duration, and a link, for every row."""

    def test_prices_present(self, content):
        assert "$1,290" in content.html and "$1,658" in content.html

    def test_prices_are_usd_not_crc(self, content):
        assert "CRC" not in content.html
        assert re.search(r"\$1,290", content.html)

    def test_durations_present(self, content):
        assert "46 hr 20 min" in content.html   # ZRH, matches Google exactly
        assert "29 hr 30 min" in content.html   # MEX: 195+700+875

    def test_links_present_and_absolute(self, content):
        links = re.findall(r'href="(https://[^"]+)"', content.html)
        assert "https://www.google.com/travel/flights?tfs=MEX1" in links
        assert "https://www.google.com/travel/flights?tfs=ZRH1" in links

    def test_every_row_has_a_view_button(self, content):
        assert content.html.count(">View</a>") == 2

    def test_airlines_and_stops(self, content):
        assert "Aeromexico" in content.html and "SWISS" in content.html
        assert "1 stop" in content.html

    def test_route_and_hub_named(self, content):
        assert "Mexico City (MEX)" in content.html
        assert "Zurich (ZRH)" in content.html

    def test_dates_readable(self, content):
        assert "Feb 10" in content.html and "Jan 15" in content.html


class TestPriceVerdict:
    """Requirement 5: the email says cheap / typical / expensive."""

    def test_typical_shown(self, content):
        assert "typical" in content.html.lower()

    def test_cheap_shown(self, items):
        cheap = [itin(fx.MEX_GREAT, dep=fx.DEPART_FEB, ret=fx.RETURN_FEB)]
        html = render_html(cheap, SEED_BANDS, threshold=1380, is_great=True,
                           generated_at="now")
        assert "cheap" in html.lower()

    def test_expensive_shown(self):
        bands = PriceBands(low=500, high=800, usual=650, source="SEED")
        html = render_html([itin(fx.ZRH_OPTION)], bands, threshold=2000,
                           is_great=False, generated_at="now")
        assert "expensive" in html.lower()

    def test_band_boundaries_displayed(self, content):
        assert "$1,188" in content.html and "$2,269" in content.html

    def test_usual_price_displayed(self, content):
        assert "$1,329" in content.html

    def test_savings_line_when_below_usual(self, content):
        assert "below the $1,329" in content.html

    def test_no_savings_line_when_above_usual(self):
        html = render_html([itin(fx.ZRH_OPTION)], SEED_BANDS, threshold=2000,
                           is_great=False, generated_at="now")
        assert "below the" not in html

    def test_source_attribution(self, content):
        assert "typical range Google publishes" in content.html


class TestSubject:
    def test_has_price_and_verdict(self, items):
        s = build_subject(items, SEED_BANDS, is_great=False)
        assert "$1,290" in s and "typical" in s

    def test_great_says_book_now(self, items):
        s = build_subject(items, SEED_BANDS, is_great=True)
        assert "book now" in s

    def test_uses_cheapest_not_first(self):
        pair = [itin(fx.ZRH_OPTION), itin(fx.MEX_OPTION, dep=fx.DEPART_FEB)]
        assert "$1,290" in build_subject(pair, SEED_BANDS, is_great=False)


class TestStructure:
    def test_is_html_document(self, content):
        assert content.html.startswith("<!DOCTYPE html>")
        assert content.html.rstrip().endswith("</html>")

    def test_table_based_layout(self, content):
        """Outlook cannot do flexbox or grid."""
        assert "<table" in content.html
        assert "display:flex" not in content.html
        assert "display:grid" not in content.html

    def test_dark_mode_supported(self, content):
        assert "prefers-color-scheme" in content.html

    def test_no_external_images(self, content):
        """Blocked images must not break the layout, so there are none."""
        assert "<img" not in content.html

    def test_html_escaped(self):
        bad = itin(fx.MEX_OPTION, dep=fx.DEPART_FEB)
        bad.airlines = ['<script>alert("x")</script>']
        html = render_html([bad], SEED_BANDS, threshold=1380,
                           is_great=False, generated_at="now")
        assert "<script>alert" not in html and "&lt;script&gt;" in html

    def test_row_cap(self):
        many = [itin(fx.MEX_OPTION, dep=fx.DEPART_FEB) for _ in range(30)]
        for n, i in enumerate(many):
            i.price_usd = 1000 + n
        html = render_html(many, SEED_BANDS, threshold=1380,
                           is_great=False, generated_at="now")
        assert html.count(">View</a>") == DEFAULT_ROWS
        assert "10 more under" in html

    def test_threshold_and_cap_explained(self, content):
        assert "$1,380" in content.html
        assert "two per day" in content.html


class TestPlainText:
    def test_has_prices_durations_links(self, content):
        t = content.text
        assert "$1,290" in t and "46 hr 20 min" in t
        assert "https://www.google.com/travel/flights?tfs=MEX1" in t

    def test_no_html_tags(self, content):
        assert "<table" not in content.text and "<div" not in content.text

    def test_verdict_present(self, content):
        assert "typical" in content.text


class TestGuards:
    def test_refuses_empty(self):
        with pytest.raises(ValueError, match="no itineraries"):
            render([], SEED_BANDS, threshold=1380, is_great=False, generated_at="now")

    def test_missing_link_omits_button(self):
        i = itin(fx.MEX_OPTION, dep=fx.DEPART_FEB, link="")
        html = render_html([i], SEED_BANDS, threshold=1380,
                           is_great=False, generated_at="now")
        assert ">View</a>" not in html
        assert "$1,290" in html


class TestRankedList:
    """Requirement: always show at least 10, cheapest first."""

    def _spread(self, n, start=1200, step=40):
        out = []
        for k in range(n):
            i = itin(fx.MEX_OPTION, dep=fx.DEPART_FEB,
                     link=f"https://www.google.com/travel/flights?tfs=P{k}")
            i.price_usd = start + k * step
            i.outbound_date = date(2027, 2, 1 + (k % 27))
            out.append(i)
        return out

    def test_pads_to_ten_when_few_qualify(self):
        """Only 2 under $1,380, but the email still shows 10 ranked."""
        sel, n_under = rank_for_email(self._spread(20), threshold=1380)
        shown = sel.items
        assert n_under == 5
        assert len(shown) == DEFAULT_ROWS

    def test_padding_rows_are_marked(self):
        html = render_html(self._spread(20), SEED_BANDS, threshold=1250,
                           is_great=False, generated_at="now")
        assert "over budget" in html
        assert "above your $1,250 budget" in html

    def test_no_padding_when_plenty_qualify(self):
        sel, n_under = rank_for_email(self._spread(20), threshold=9999)
        shown = sel.items
        assert n_under == 20
        assert all(i.price_usd <= 9999 for i in shown)
        assert "over budget" not in render_html(
            shown, SEED_BANDS, threshold=9999, is_great=False, generated_at="n")

    def test_sorted_cheapest_first(self):
        shown = rank_for_email(self._spread(20), threshold=1380)[0].items
        prices = [i.price_usd for i in shown]
        assert prices == sorted(prices)
        assert prices[0] == 1200

    def test_rows_are_numbered(self):
        html = render_html(self._spread(12), SEED_BANDS, threshold=1380,
                           is_great=False, generated_at="now")
        for n in range(1, 11):
            assert f">{n}.</span>" in html

    def test_headline_counts_only_qualifying(self):
        html = render_html(self._spread(20), SEED_BANDS, threshold=1380,
                           is_great=False, generated_at="now")
        assert "Found 5 visa-free options" in html

    def test_single_qualifying_still_gives_context(self):
        html = render_html(self._spread(20), SEED_BANDS, threshold=1210,
                           is_great=False, generated_at="now")
        assert "Found 1 visa-free option" in html
        assert html.count(">View</a>") == DEFAULT_ROWS

    def test_text_version_ranked_and_marked(self):
        text = render_text(self._spread(20), SEED_BANDS, threshold=1250,
                           is_great=False, generated_at="now")
        assert "1. " in text and "[over budget]" in text

    def test_fewer_than_ten_available_shows_all(self):
        shown = rank_for_email(self._spread(4), threshold=1380)[0].items
        assert len(shown) == 4
