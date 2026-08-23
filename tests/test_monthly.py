"""The monthly wide net: one text query per month, parsed offline.

Fixtures below are the real shapes seen on 2026-08-22. Both renderings
occur: one carries the year, one omits it and omits the second month too.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pytest

from tracker.monthly import (
    MonthHint, hint_window_keys, months_in_window, parse_hint, scan_months,
    visible_text,
)

# Real page text, trimmed. Note the en dash and the missing year.
WITH_YEAR = ("Best Close dialog Looking for stops Travel Jan 29 – Feb 25, 2027 "
             "for $1,347 Change dates Date grid Price graph Other departing flights")
NO_YEAR = ("Track prices Any dates Travel Sep 30 – Oct 30 for $1,604 "
           "Change dates Date grid Price graph")
NO_HINT = ("Travel update Air traffic disruptions may affect flights. Best "
           "Close dialog Looking for stops")
HTML = ('<html><body><script>var x = "Travel Nov 1 - Nov 2 for $99";</script>'
        '<div>Travel Nov 30 – Dec 29 for $1,432</div></body></html>')


class TestParseHint:
    def test_reads_a_hint_that_carries_the_year(self):
        h = parse_hint(WITH_YEAR, month="February 2027", anchor_year=2027)
        assert h == MonthHint("February 2027", date(2027, 1, 29), date(2027, 2, 25), 1347)
        assert h.nights == 27

    def test_fills_in_a_missing_year_from_the_anchor(self):
        h = parse_hint(NO_YEAR, month="October 2026", anchor_year=2026)
        assert h.depart == date(2026, 9, 30) and h.ret == date(2026, 10, 30)
        assert h.nights == 30 and h.price_usd == 1604

    def test_no_recommendation_is_none_not_an_error(self):
        assert parse_hint(NO_HINT, month="March 2027", anchor_year=2027) is None

    def test_strips_markup_and_ignores_script_contents(self):
        """A price inside a <script> must not be mistaken for the hint."""
        h = parse_hint(HTML, month="December 2026", anchor_year=2026)
        assert h.price_usd == 1432
        assert h.depart == date(2026, 11, 30)

    def test_year_rolls_forward_across_new_year(self):
        h = parse_hint("Travel Dec 28 – Jan 20 for $1,500",
                       month="December 2026", anchor_year=2026)
        assert h.depart == date(2026, 12, 28) and h.ret == date(2027, 1, 20)
        assert h.nights == 23

    def test_impossible_date_is_rejected(self):
        assert parse_hint("Travel Feb 30 – Mar 20, 2027 for $900",
                          month="February 2027", anchor_year=2027) is None

    def test_backwards_range_is_rejected(self):
        assert parse_hint("Travel Mar 20, 2027 – Mar 2, 2027 for $900",
                          month="March 2027", anchor_year=2027) is None

    @pytest.mark.parametrize("dash", ["–", "—", "-"])
    def test_accepts_every_dash_google_uses(self, dash):
        h = parse_hint(f"Travel Sep 3 {dash} Sep 30, 2026 for $1,663",
                       month="September 2026", anchor_year=2026)
        assert h is not None and h.price_usd == 1663

    def test_visible_text_drops_tags(self):
        assert "<div>" not in visible_text(HTML)


class TestMonthsInWindow:
    def test_covers_every_month_the_window_touches(self):
        got = months_in_window(date(2026, 9, 12), date(2027, 4, 22))
        assert len(got) == 8
        assert got[0] == ("September 2026", 2026)
        assert got[-1] == ("April 2027", 2027)

    def test_single_month_window(self):
        assert months_in_window(date(2027, 2, 1), date(2027, 2, 28)) == [
            ("February 2027", 2027)]


class FakeFetch:
    """Injected in place of the network, per the project's test rule."""
    def __init__(self, pages):
        self.pages, self.queries = pages, []

    def __call__(self, query):
        self.queries.append(query)
        for needle, page in self.pages.items():
            if needle in query:
                if isinstance(page, Exception):
                    raise page
                return page
        return ""


class TestScanMonths:
    def test_one_request_per_month_and_hints_come_back(self):
        f = FakeFetch({"February 2027": WITH_YEAR, "October 2026": NO_YEAR})
        hints = scan_months(f, [("October 2026", 2026), ("February 2027", 2027)])
        assert len(f.queries) == 2
        assert [h.price_usd for h in hints] == [1604, 1347]

    def test_a_month_without_a_hint_is_skipped_quietly(self):
        f = FakeFetch({"March 2027": NO_HINT, "February 2027": WITH_YEAR})
        hints = scan_months(f, [("March 2027", 2027), ("February 2027", 2027)])
        assert [h.month for h in hints] == ["February 2027"]

    def test_a_failing_month_does_not_abort_the_sweep(self):
        """Five of eight months returned nothing on the first real pass."""
        f = FakeFetch({"January 2027": RuntimeError("boom"),
                       "February 2027": WITH_YEAR})
        hints = scan_months(f, [("January 2027", 2027), ("February 2027", 2027)])
        assert len(hints) == 1

    def test_nights_outside_the_trip_range_are_filtered(self):
        f = FakeFetch({"October 2026": NO_YEAR})       # 30 nights
        assert scan_months(f, [("October 2026", 2026)], max_nights=28) == []
        assert scan_months(f, [("October 2026", 2026)], min_nights=31) == []
        assert len(scan_months(f, [("October 2026", 2026)],
                               min_nights=21, max_nights=31)) == 1

    def test_query_names_origin_and_destination(self):
        f = FakeFetch({})
        scan_months(f, [("May 2027", 2027)], origin="SJO", destination="HND")
        assert f.queries == ["Flights from SJO to HND in May 2027"]

    def test_keys_are_ordered_cheapest_first(self):
        a = MonthHint("a", date(2027, 1, 1), date(2027, 1, 22), 1800)
        b = MonthHint("b", date(2027, 2, 1), date(2027, 2, 22), 1200)
        assert hint_window_keys([a, b]) == [b.key, a.key]

    def test_key_matches_the_hot_list_shape(self):
        """A mismatched separator silently makes every hint a hot-list miss."""
        from tracker.schedule import Window
        h = MonthHint("x", date(2027, 1, 29), date(2027, 2, 25), 1347)
        assert h.key == "2027-01-29_2027-02-25"
        assert h.key == Window(h.depart, h.ret).key
