"""Google saying "no price" is not the parser failing to read one.

`rows_missed_by_parser` is what raises "results are arriving in a format
we cannot read" - an alarm that means Google's markup has moved and real
fares are being lost. So what it counts has to be real losses.

Measured 2026-08-25: 39 windows each carried exactly two unreadable rows,
never one, never three. Captured live, the row was:

    Total price is unavailable. 2 stops flight with American and JAL ...
    Layover at Dallas Fort Worth ... Chicago O'Hare ...

One logical row appearing twice, because the DOM carries everything
double. It has no price by Google's own admission, and it transits the US,
so the visa rule would drop it regardless. Nothing was being lost - but
the counter had reached 20 against an alarm at 25, so the first email that
alarm ever sent would have been false.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.browser import _MASTER, _NO_PRICE, parse_options

LIVE_ROW = ("Total price is unavailable. 2 stops flight with American and "
            "JAL. Leaves Juan Santamaria International Airport at 12:35 AM "
            "on Tuesday, March 2 and arrives at Narita International Airport "
            "at 2:45 PM on Wednesday, March 3. Total duration 23 hr 10 min. "
            "Layover (1 of 2) is a 2 hr 18 min layover at Dallas Fort Worth "
            "International Airport in Dallas.")


class TestTheDiscriminator:
    def test_the_live_row_is_recognised_as_unpriced(self):
        assert _NO_PRICE.search(LIVE_ROW)

    def test_a_real_fare_is_not(self):
        row = ("From 1,347 US dollars round trip total. 1 stop flight with "
               "Edelweiss Air and SWISS. Layover at Zurich.")
        assert not _NO_PRICE.search(row)
        assert _MASTER.search(row), "a real fare must still parse"

    def test_page_furniture_is_not_unpriced_either(self):
        for junk in ("View more flights", "Number of adult passengers",
                     "Remove infant on lap"):
            assert not _NO_PRICE.search(junk), junk

    def test_it_is_case_insensitive(self):
        assert _NO_PRICE.search("TOTAL PRICE IS UNAVAILABLE. 1 stop flight")


class TestTheCountsAreSeparated:
    def _dom(self, rows):
        body = "".join(f'<li><div aria-label="{r}"></div></li>' for r in rows)
        return f"<html><body><ul>{body}</ul></body></html>"

    def _parse(self, rows):
        stats: dict = {}
        parse_options(self._dom(rows), origin="SJO", destination="TYO",
                      depart_date=date(2027, 3, 2), return_date=date(2027, 3, 30),
                      stats=stats)
        return stats

    def test_an_unpriced_row_is_not_a_parse_failure(self):
        stats = self._parse([LIVE_ROW])
        assert stats.get("unpriced") == 1
        assert stats.get("unmatched", 0) == 0, (
            "an unpriced row was counted as unreadable markup")

    def test_genuinely_unreadable_markup_still_counts(self):
        """The alarm must still fire on a real change."""
        stats = self._parse(["something entirely unlike a flight row"])
        assert stats.get("unmatched") == 1
        assert stats.get("unpriced", 0) == 0

    def test_both_kinds_together(self):
        stats = self._parse([LIVE_ROW, "gibberish", LIVE_ROW])
        assert stats.get("unpriced") == 2 and stats.get("unmatched") == 1


class TestTheStoreKeepsThemApart:
    def test_the_field_exists_and_starts_at_zero(self):
        from tracker.sweeper import SweepStore
        s = SweepStore()
        assert s.rows_unpriced == 0 and s.rows_missed_by_parser == 0

    def test_the_alarm_reads_only_real_failures(self):
        import pathlib
        import re
        src = re.sub(r"\s+", " ", (pathlib.Path(__file__).resolve().parent.parent
                                   / "tracker" / "cli.py").read_text(encoding="utf-8"))
        i = src.find("PARSER_ALARM_ROWS")
        assert i > 0
        assert "rows_missed_by_parser" in src[i:i + 1200], (
            "the parser alarm no longer reads the failure counter")
        assert "rows_unpriced" not in src[i:i + 1200], (
            "the alarm would fire on rows Google simply cannot price")
