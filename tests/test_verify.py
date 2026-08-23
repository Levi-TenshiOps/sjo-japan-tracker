"""Chrome verification: which windows get a launch, and what survives.

No browser is launched and no network is touched - `fetch` is injected.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
from datetime import date

import pytest

from tracker.verify import (
    VerifyTarget, choose_targets, cheapest, under, verify,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")
TODAY = date(2026, 8, 22)


def key(a, b):
    return f"{a}_{b}"


class TestChoosingTargets:
    def test_hints_come_before_hot_before_grid(self):
        got = choose_targets(
            hint_keys=[key("2027-01-29", "2027-02-25")],
            hot_keys=[key("2027-02-01", "2027-02-22")],
            grid_keys=[key("2027-03-01", "2027-03-22")],
            today=TODAY)
        assert [t.source for t in got] == ["hint", "hot", "grid"]

    def test_limit_is_respected(self):
        hints = [key(f"2027-01-{d:02d}", f"2027-02-{d:02d}") for d in range(1, 20)]
        assert len(choose_targets(hint_keys=hints, limit=6, today=TODAY)) == 6

    def test_a_window_is_never_launched_twice(self):
        k = key("2027-01-29", "2027-02-25")
        got = choose_targets(hint_keys=[k], hot_keys=[k], grid_keys=[k], today=TODAY)
        assert len(got) == 1 and got[0].source == "hint"

    def test_past_departures_are_dropped(self):
        got = choose_targets(hint_keys=[key("2026-01-01", "2026-01-28")], today=TODAY)
        assert got == []

    def test_min_lead_days_is_honoured(self):
        soon = key("2026-08-25", "2026-09-21")      # 3 days out
        assert choose_targets(hint_keys=[soon], today=TODAY, min_lead_days=21) == []
        assert len(choose_targets(hint_keys=[soon], today=TODAY, min_lead_days=1)) == 1

    def test_a_backwards_window_is_dropped(self):
        assert choose_targets(hint_keys=[key("2027-03-01", "2027-02-01")],
                              today=TODAY) == []

    def test_malformed_keys_are_skipped_not_fatal(self):
        got = choose_targets(
            hint_keys=["nonsense", "", key("2027-01-29", "2027-02-25")], today=TODAY)
        assert len(got) == 1

    def test_key_round_trips(self):
        t = VerifyTarget(date(2027, 1, 29), date(2027, 2, 25), "hint")
        assert t.key == "2027-01-29_2027-02-25"


class FakeChrome:
    """Returns the captured DOM for every URL, and counts launches."""
    def __init__(self, dom, fail_after=None):
        self.dom, self.calls, self.fail_after = dom, 0, fail_after

    def __call__(self, url):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            return ""
        return self.dom


@pytest.fixture
def dom():
    return io.open(FIXTURE, encoding="utf-8").read()


class TestVerify:
    def test_one_launch_per_target(self, dom):
        f = FakeChrome(dom)
        targets = choose_targets(
            hint_keys=[key("2027-01-29", "2027-02-25"),
                       key("2027-02-01", "2027-02-22")], today=TODAY)
        verify(targets, fetch=f)
        assert f.calls == 2

    def test_only_visa_clean_options_survive(self, dom):
        """The fixture holds four US/Canada routings and one via Mexico."""
        f = FakeChrome(dom)
        got = verify(choose_targets(hint_keys=[key("2027-01-29", "2027-02-25")],
                                    today=TODAY), fetch=f)
        assert [o.price_usd for o in got] == [2652]
        assert all(o.visa_ok for o in got)

    def test_the_cheapest_row_is_rejected_not_reported(self, dom):
        """$1,863 via Toronto is the cheapest and must never be offered."""
        f = FakeChrome(dom)
        got = verify(choose_targets(hint_keys=[key("2027-01-29", "2027-02-25")],
                                    today=TODAY), fetch=f)
        assert all(o.price_usd != 1863 for o in got)

    def test_results_are_cheapest_first_across_targets(self, dom):
        f = FakeChrome(dom)
        targets = choose_targets(
            hint_keys=[key("2027-01-29", "2027-02-25"),
                       key("2027-02-01", "2027-02-22")], today=TODAY)
        got = verify(targets, fetch=f)
        assert [o.price_usd for o in got] == sorted(o.price_usd for o in got)

    def test_an_empty_dom_yields_nothing_and_does_not_raise(self):
        got = verify(choose_targets(hint_keys=[key("2027-01-29", "2027-02-25")],
                                    today=TODAY), fetch=FakeChrome(""))
        assert got == []

    def test_a_failing_launch_does_not_abort_the_rest(self, dom):
        f = FakeChrome(dom, fail_after=1)
        targets = choose_targets(
            hint_keys=[key("2027-01-29", "2027-02-25"),
                       key("2027-02-01", "2027-02-22")], today=TODAY)
        got = verify(targets, fetch=f)
        assert f.calls == 2 and len(got) == 1

    def test_no_targets_means_no_launches(self, dom):
        f = FakeChrome(dom)
        assert verify([], fetch=f) == [] and f.calls == 0

    def test_delay_is_applied_between_launches(self, dom):
        naps = []
        targets = choose_targets(
            hint_keys=[key("2027-01-29", "2027-02-25"),
                       key("2027-02-01", "2027-02-22")], today=TODAY)
        verify(targets, fetch=FakeChrome(dom), sleep=naps.append, delay_s=2.0)
        assert naps == [2.0, 2.0]


class TestThresholdHelpers:
    def test_under_filters_and_sorts(self, dom):
        got = verify(choose_targets(hint_keys=[key("2027-01-29", "2027-02-25")],
                                    today=TODAY), fetch=FakeChrome(dom))
        assert under(got, 1400) == []          # the only clean row is $2,652
        assert [o.price_usd for o in under(got, 3000)] == [2652]

    def test_cheapest_of_nothing_is_none(self):
        assert cheapest([]) is None


class TestVerifiedFaresReachTheEmail:
    """The whole point: a fare Chrome found must be visible in the inbox.

    Regression: the run logged "CHROME FOUND 1 fare at or under $1,400"
    and then emailed "$1,635", because the email was rendered from the HTTP
    grid alone and the alert price came from it too.
    """

    def _verified(self):
        from tracker.browser import BrowserOption
        return [BrowserOption(
            price_usd=1347, origin="SJO", destination="NRT",
            depart_date=date(2027, 1, 29), return_date=date(2027, 2, 25),
            stops=("ZRH",), airlines=("Edelweiss Air", "SWISS"),
            total_minutes=46 * 60 + 20,
            deep_link="https://www.google.com/travel/flights?tfs=ZRH1")]

    def _items(self):
        from tracker.itinerary import build_itinerary
        from tests import fixtures as fx
        return [build_itinerary(fx.MEX_OPTION, origin="SJO", destination="NRT",
                                outbound_date=fx.DEPART, return_date=fx.RETURN,
                                deep_link="https://example.com/a")]

    def test_price_appears_in_html(self):
        from tracker.email_render import render_html
        from tracker.pricing import SEED_BANDS
        html = render_html(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "$1,347" in html

    def test_route_and_airlines_appear(self):
        from tracker.email_render import render_html
        from tracker.pricing import SEED_BANDS
        html = render_html(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "SJO - ZRH - NRT" in html
        assert "Edelweiss Air" in html and "SWISS" in html

    def test_duration_matches_the_booking_page(self):
        """46 hr 20 min is what Google shows for this itinerary."""
        from tracker.email_render import render_html
        from tracker.pricing import SEED_BANDS
        html = render_html(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "46 hr 20 min" in html

    def test_price_appears_in_plain_text_too(self):
        from tracker.email_render import render_text
        from tracker.pricing import SEED_BANDS
        text = render_text(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "$1,347" in text and "ZRH" in text

    def test_under_threshold_is_flagged_in_text(self):
        from tracker.email_render import render_text
        from tracker.pricing import SEED_BANDS
        text = render_text(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "under your threshold" in text

    def test_no_verified_fares_renders_nothing_extra(self):
        from tracker.email_render import render_html
        from tracker.pricing import SEED_BANDS
        html = render_html(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now", verified=[])
        assert "Verified in a real browser" not in html

    def test_deep_link_is_clickable(self):
        from tracker.email_render import render_html
        from tracker.pricing import SEED_BANDS
        html = render_html(self._items(), SEED_BANDS, threshold=1400,
                           is_great=False, generated_at="now",
                           verified=self._verified())
        assert "tfs=ZRH1" in html
