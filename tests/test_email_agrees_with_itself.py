"""The email must not contradict its own verified block.

Round 8 of the email audit, 2026-08-25. The HTML half had already been
fixed for this once - its comment describes announcing "Nothing under
$1,400 today" above a block listing five fares at $1,347. The plain-text
half was never given the same fix, so it still counted and quoted the
HTTP grid alone:

    $1,347  SJO - ZRH - TYO  <-- under your threshold
    0 visa-free option(s) at or under $1,400.
    Cheapest: $2,509 (cheap for this route).

Three wrong statements from one cause. The band was right - it comes
from `cheapest_seen` - which made "cheap" read as a label for $2,509.
"""

from datetime import date, datetime

import pytest

from tracker.browser import BrowserOption
from tracker.email_render import render, render_text
from tracker.itinerary import Itinerary, Leg
from tracker.pricing import PriceBands

BANDS = PriceBands(low=2213, high=3202, usual=2866, source="SEED",
                   seen_low=1347, seen_high=13127)
DEPART, RETURN = date(2027, 1, 29), date(2027, 2, 25)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def cheap_verified(price: int = 1347) -> BrowserOption:
    return BrowserOption(
        price_usd=price, origin="SJO", destination="TYO",
        depart_date=DEPART, return_date=RETURN, stops=("ZRH",),
        airlines=("SWISS",), total_minutes=2780,
        deep_link="https://example.invalid/verified",
    )


def dear_grid(price: int = 2509) -> Itinerary:
    return Itinerary(
        price_usd=price,
        legs=[
            Leg("SJO", "FRA", _dt("2027-01-29T10:00"), _dt("2027-01-29T20:00"), 600),
            Leg("FRA", "NRT", _dt("2027-01-29T22:00"), _dt("2027-01-30T09:40"), 700),
            Leg("NRT", "FRA", _dt("2027-02-25T10:00"), _dt("2027-02-25T20:00"), 700),
            Leg("FRA", "SJO", _dt("2027-02-25T22:00"), _dt("2027-02-26T04:00"), 600),
        ],
        airlines=["Lufthansa"], outbound_date=DEPART, return_date=RETURN,
        origin="SJO", destination="NRT",
        deep_link="https://example.invalid/grid", outbound_leg_count=2,
    )


class TestTheSummaryCountsBothChannels:
    def test_text_does_not_say_zero_above_a_cheap_verified_fare(self):
        text = render_text([dear_grid()], BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=[cheap_verified()])
        assert "0 visa-free option(s) at or under $1,400." not in text
        assert "1 visa-free option(s) at or under $1,400." in text

    def test_text_quotes_the_cheapest_fare_it_actually_shows(self):
        text = render_text([dear_grid()], BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=[cheap_verified()])
        assert "Cheapest: $1,347" in text
        assert "Cheapest: $2,509" not in text

    def test_the_band_label_describes_the_number_beside_it(self):
        """$2,509 sits in TYPICAL; $1,347 in CHEAP. Whichever price the
        line quotes, the parenthesis must agree with that price."""
        text = render_text([dear_grid()], BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=[cheap_verified()])
        line = next(ln for ln in text.splitlines() if ln.startswith("Cheapest:"))
        quoted = int(line.split("$")[1].split()[0].replace(",", ""))
        label = line.split("(")[1].split()[0].lower()
        assert BANDS.classify(quoted).lower().startswith(label[:5])

    @pytest.mark.parametrize("grid", [[], [dear_grid()]])
    def test_the_standout_line_names_the_standout_fare(self, grid):
        msg = render(grid, BANDS, threshold=1400, is_great=True,
                     generated_at="now", verified=[cheap_verified()])
        assert "$1,347 is a standout price" in msg.html
        assert "$2,509 is a standout price" not in msg.html

    def test_grid_only_email_is_unchanged(self):
        """With no verified fares the old behaviour must still hold."""
        text = render_text([dear_grid()], BANDS, threshold=1400,
                           is_great=False, generated_at="now", verified=[])
        assert "0 visa-free option(s) at or under $1,400." in text
        assert "Cheapest: $2,509" in text
