"""Top-N selection with a guaranteed priority-month share."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
import pytest

from tracker.itinerary import build_itinerary
from tracker.ranking import Selection, priority_checker, select_top
from tests import fixtures as fx

PRIORITY = [1, 2, 3]          # January, February, March


def make(price, month, day=10, year=2027):
    i = build_itinerary(fx.MEX_OPTION, origin="SJO", destination="NRT",
                        outbound_date=date(year, month, day),
                        return_date=date(year, month, day) + timedelta(days=14),
                        deep_link="https://x.test")
    i.price_usd = price
    return i


def pick(items, count=20, share=0.5):
    return select_top(items, count=count,
                      is_priority=priority_checker(PRIORITY),
                      priority_share=share)


class TestQuota:
    def test_half_the_slots_are_priority_when_needed(self):
        """30 cheap non-priority fares must not crowd January out entirely."""
        cheap_other = [make(900 + n, 6, day=1 + n % 28) for n in range(30)]
        pricey_priority = [make(1500 + n, 1, day=1 + n % 28) for n in range(30)]
        sel = pick(cheap_other + pricey_priority)
        assert len(sel.items) == 20
        assert sel.priority_count >= 10
        assert sel.quota_was_binding

    def test_quota_does_nothing_when_already_satisfied(self):
        """If the cheapest 20 are mostly priority, do not distort anything."""
        items = [make(900 + n, 1, day=1 + n % 28) for n in range(15)]
        items += [make(1800 + n, 7, day=1 + n % 28) for n in range(15)]
        sel = pick(items)
        assert sel.priority_count == 15
        assert not sel.quota_was_binding
        assert [i.price_usd for i in sel.items] == sorted(
            i.price_usd for i in items)[:20]

    def test_output_is_still_strictly_cheapest_first(self):
        """The quota shapes membership, never the displayed order."""
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        prices = [i.price_usd for i in pick(items).items]
        assert prices == sorted(prices)

    def test_cheapest_overall_is_always_included(self):
        items = [make(1500 + n, 1) for n in range(30)]
        items.append(make(400, 9))          # a bargain outside priority months
        sel = pick(items)
        assert min(i.price_usd for i in sel.items) == 400

    def test_share_is_configurable(self):
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        assert pick(items, share=0.25).priority_count >= 5
        assert pick(items, share=0.75).priority_count >= 15

    def test_share_of_zero_disables_the_quota(self):
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        sel = pick(items, share=0.0)
        assert sel.priority_count == 0

    def test_share_of_one_fills_with_priority_first(self):
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        assert pick(items, share=1.0).priority_count == 20


class TestEdgeCases:
    def test_too_few_priority_options_releases_the_reservation(self):
        """Three January fares must not leave seven slots empty."""
        items = [make(1500 + n, 1) for n in range(3)]
        items += [make(900 + n, 8) for n in range(30)]
        sel = pick(items)
        assert len(sel.items) == 20
        assert sel.priority_count == 3
        assert sel.quota_met       # met, given only 3 were available

    def test_no_priority_options_at_all(self):
        items = [make(900 + n, 8) for n in range(30)]
        sel = pick(items)
        assert len(sel.items) == 20 and sel.priority_count == 0

    def test_fewer_candidates_than_slots(self):
        sel = pick([make(1000, 1), make(1100, 7)])
        assert len(sel.items) == 2

    def test_empty_input(self):
        sel = pick([])
        assert sel.items == [] and sel.priority_share == 0.0

    def test_exactly_the_slot_count(self):
        items = [make(1000 + n, 1 if n % 2 else 7) for n in range(20)]
        assert len(pick(items).items) == 20

    def test_no_priority_predicate_is_plain_cheapest_first(self):
        items = [make(2000 - n, 6) for n in range(30)]
        sel = select_top(items, count=20)
        assert len(sel.items) == 20
        assert sel.items[0].price_usd == 1971

    def test_no_duplicates(self):
        items = [make(1000 + n, 1 if n % 3 else 9) for n in range(40)]
        sel = pick(items)
        assert len({id(i) for i in sel.items}) == len(sel.items)

    def test_ties_do_not_break_selection(self):
        items = [make(1200, 1) for _ in range(15)]
        items += [make(1200, 8) for _ in range(15)]
        assert len(pick(items).items) == 20


class TestReporting:
    def test_describe_mentions_the_split(self):
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        text = pick(items).describe()
        assert "priority months" in text and "20 of 60" in text

    def test_share_is_computed(self):
        items = [make(900 + n, 6) for n in range(30)]
        items += [make(1500 + n, 2) for n in range(30)]
        assert pick(items).priority_share >= 0.5

    def test_priority_checker_matches_departure_month(self):
        is_pri = priority_checker([1, 2, 3])
        assert is_pri(make(1000, 2))
        assert not is_pri(make(1000, 4))

    def test_empty_month_list_matches_nothing(self):
        assert not priority_checker([])(make(1000, 1))
