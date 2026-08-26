"""The price bar must always contain the fare the email is about.

Found by the trip owner reading a live email on 2026-08-26: the headline
said $1,336 and the bar beneath it said

    cheap   $1,343 - $2,213

so the fare being advertised sat *below the floor of its own gauge*.

The cause is ordering, not arithmetic. `cli.run` builds the bands from the
two CSVs and only afterwards launches Chrome - and Chrome is the one thing
that finds the record fares, precisely the ones the HTTP grid cannot see.
So the gauge was drawn from a population that excluded the number printed
on top of it.

Third appearance of "a band the data cannot reach is a broken gauge":
GOOGLE bands whose green zone no fare could enter, a cheap band with no
floor, and now a floor above the headline.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.pricing import PriceBands


def bands(low=2213, high=3202, seen_low=1343, seen_high=13127):
    return PriceBands(low=low, high=high, usual=2866, source="SEED",
                      seen_low=seen_low, seen_high=seen_high)


class TestExtendObserved:
    def test_a_new_low_moves_the_floor(self):
        b = bands().extend_observed([1336, 1500])
        assert b.seen_low == 1336

    def test_a_new_high_moves_the_ceiling(self):
        b = bands().extend_observed([20000])
        assert b.seen_high == 20000

    def test_it_never_narrows_the_range(self):
        """The difference from `with_observed`, and the whole point."""
        b = bands().extend_observed([2000, 2500])
        assert b.seen_low == 1343, "a later, dearer sample shrank the floor"
        assert b.seen_high == 13127, "a later, cheaper sample shrank the ceiling"

    def test_with_observed_does_narrow_it(self):
        """Pinning the contrast, so the two are not confused again."""
        b = bands().with_observed([2000, 2500])
        assert b.seen_low == 2000

    def test_empty_prices_change_nothing(self):
        b = bands()
        assert b.extend_observed([]) == b
        assert b.extend_observed([0, None]) == b

    def test_it_works_from_an_unset_range(self):
        b = bands(seen_low=0, seen_high=0).extend_observed([1336, 4000])
        assert (b.seen_low, b.seen_high) == (1336, 4000)

    def test_the_cut_offs_are_untouched(self):
        """Only the observed extremes move. The cut-offs are percentiles
        over a large population and must not swing on a dozen fresh rows."""
        before = bands()
        after = before.extend_observed([1336])
        assert (after.low, after.high, after.usual, after.source) == \
               (before.low, before.high, before.usual, before.source)


class TestTheGaugeContainsItsOwnHeadline:
    def test_the_floor_is_never_above_the_cheapest_shown(self):
        shown = [1336, 1343, 1391, 1444]
        b = bands().extend_observed(shown)
        assert b.seen_low <= min(shown), (
            "the email would advertise a fare below its own scale")

    def test_the_ceiling_is_never_below_the_dearest_shown(self):
        shown = [1336, 99999]
        b = bands().extend_observed(shown)
        assert b.seen_high >= max(shown)


class TestTheWiringInCli:
    """The fix is one call placed after Chrome and after the sweep merge;
    pin the placement, because putting it earlier restores the bug."""

    def _src(self):
        import re
        from pathlib import Path
        return re.sub(r"\s+", " ",
                      Path("tracker/cli.py").read_text(encoding="utf-8"))

    def test_the_bands_are_extended_before_rendering(self):
        src = self._src()
        assert "bands = bands.extend_observed(" in src

    def test_it_happens_after_the_sweep_is_merged(self):
        src = self._src()
        merge = src.index("verified = sorted(verified + fresh")
        extend = src.index("bands = bands.extend_observed(")
        render = src.index("content = email_render.render(")
        assert merge < extend < render, (
            "the gauge must see every fare the email will show")
