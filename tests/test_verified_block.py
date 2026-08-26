"""The block carrying live prices must not hide the priority months.

It is the part of the email the trip owner acts on - browser-verified,
current, one click from booking - and it was sorted purely by price. A
month that happens to be cheap can therefore fill every visible row. On
2026-08-25 November already held two of six, on a month barely explored,
while December had not been touched at all.

Same contract `ranking.select_top` gives the grid table, which this could
not reuse: that works on `Itinerary` and these are `BrowserOption`.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.browser import BrowserOption
from tracker.email_render import VERIFIED_ROWS, select_verified

PRIORITY = [1, 2, 3]


def opt(price, month, day=15, minutes=2780):
    y = 2027 if month <= 6 else 2026
    return BrowserOption(
        price_usd=price, origin="SJO", destination="TYO",
        depart_date=date(y, month, day), return_date=date(y, month, 28),
        stops=("ZRH",), airlines=("SWISS",), total_minutes=minutes,
        deep_link="https://example/x")


class TestThePriorityMonthsAreProtected:
    def test_a_cheap_other_month_cannot_take_every_row(self):
        """The failure this exists to prevent."""
        flood = [opt(1000 + i, 11, day=(i % 28) + 1) for i in range(20)]
        pri = [opt(1500 + i, 1, day=(i % 28) + 1) for i in range(10)]
        sel = select_verified(flood + pri, priority_months=PRIORITY, share=0.5)
        n_pri = sum(1 for o in sel if o.depart_date.month in {1, 2, 3})
        assert n_pri >= 5, f"only {n_pri} priority rows survived a cheap November"

    def test_it_does_not_bind_when_it_need_not(self):
        """If the cheapest already are priority months, nothing changes."""
        pri = [opt(1000 + i, 1, day=(i % 28) + 1) for i in range(12)]
        other = [opt(2000 + i, 11, day=(i % 28) + 1) for i in range(12)]
        sel = select_verified(pri + other, priority_months=PRIORITY)
        assert all(o.depart_date.month == 1 for o in sel)

    def test_fewer_priority_options_than_slots_releases_them(self):
        pri = [opt(1500, 1), opt(1600, 2)]
        other = [opt(1000 + i, 11, day=(i % 28) + 1) for i in range(20)]
        sel = select_verified(pri + other, priority_months=PRIORITY)
        assert len(sel) == VERIFIED_ROWS
        assert sum(1 for o in sel if o.depart_date.month in {1, 2, 3}) == 2


class TestTheContractHolds:
    def test_the_list_is_always_cheapest_first(self):
        opts = [opt(1500, 11), opt(1200, 1), opt(1800, 2), opt(1100, 12)]
        sel = select_verified(opts, priority_months=PRIORITY)
        assert [o.price_usd for o in sel] == sorted(o.price_usd for o in sel)

    def test_the_single_cheapest_always_appears(self):
        """Even when it is in no priority month at all."""
        bargain = opt(900, 12)
        pri = [opt(1000 + i, 1, day=(i % 28) + 1) for i in range(30)]
        sel = select_verified([bargain] + pri, priority_months=PRIORITY)
        assert bargain in sel and sel[0] is bargain

    def test_no_priority_months_is_plain_cheapest_first(self):
        opts = [opt(1500, 11), opt(1200, 1), opt(1800, 2)]
        sel = select_verified(opts, priority_months=())
        assert [o.price_usd for o in sel] == [1200, 1500, 1800]

    def test_it_never_returns_more_than_asked(self):
        opts = [opt(1000 + i, 1, day=(i % 28) + 1) for i in range(40)]
        assert len(select_verified(opts, priority_months=PRIORITY)) == VERIFIED_ROWS

    def test_no_duplicates_are_introduced(self):
        opts = [opt(1000 + i, 1 if i % 2 else 11, day=(i % 28) + 1)
                for i in range(30)]
        sel = select_verified(opts, priority_months=PRIORITY)
        assert len(sel) == len({id(o) for o in sel})

    def test_an_empty_list_is_empty(self):
        assert select_verified([], priority_months=PRIORITY) == []

    def test_fewer_options_than_slots_shows_them_all(self):
        opts = [opt(1200, 1), opt(1300, 11)]
        assert len(select_verified(opts, priority_months=PRIORITY)) == 2


class TestTheEmailUsesIt:
    def test_both_renderers_apply_the_quota(self):
        import pathlib
        import re
        src = re.sub(r"\s+", " ", (pathlib.Path(__file__).resolve().parent.parent
                                   / "tracker" / "email_render.py").read_text(encoding="utf-8"))
        assert src.count("select_verified(verified, priority_months=") == 2, (
            "the HTML and text blocks must both use the quota")

    def test_the_row_count_is_named_not_magic(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "tracker" / "email_render.py").read_text(encoding="utf-8")
        assert "verified[:6]" not in src, "a magic 6 is back"
        assert "verified[:VERIFIED_ROWS]" in src
