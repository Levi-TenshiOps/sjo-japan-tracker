"""Classify a fare as cheap / typical / expensive.

Three sources, best first. Whichever fires, the verdict carries a `source`
so the email can be honest about where the judgement came from.

1. GOOGLE    Google Flights' own price-insight block, if we can pull it out
             of the raw page payload. Best possible answer, but it comes
             from an undocumented structure so it may go missing.
2. HISTORY   Rolling percentiles over our own price_history.csv, once there
             are enough observations for the route. Self-correcting, and it
             adapts to the specific dates being tracked.
3. SEED      Fixed bands derived from the Google Flights digest email for
             this exact route (SJO -> Tokyo): Google put the usual range at
             CRC 550,000-1,050,000 with travellers typically booking at
             CRC 615,055. Converted at 462.79 CRC/USD, itself derived from
             the same Jan 15-24 itinerary priced at CRC 767,308 / $1,658.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Literal, Sequence

Band = Literal["CHEAP", "TYPICAL", "EXPENSIVE"]
Source = Literal["GOOGLE", "HISTORY", "SEED"]

CRC_PER_USD = 462.79

# Recalibrated 2026-08-23 against 1,165 visa-free observations of this exact
# route, and the old numbers are worth recording because they were wrong in
# an instructive way.
#
# They came from a Google Flights digest for SJO-Tokyo: usual range
# CRC 550,000-1,050,000, typical booking CRC 615,055, i.e. $1,188 / $2,269 /
# $1,329. That is a Google-sourced figure, and it therefore carries exactly
# the defect that got the GOOGLE band source demoted below HISTORY in the
# first place: **it describes every routing Google sells, including the US
# and Canadian transits this traveller cannot legally take.**
#
# Demoting GOOGLE while leaving a Google-derived SEED underneath it fixed
# half the problem. Measured against visa-free fares only:
#
#     p20 $2,213    p50 $2,866    p80 $3,202    (n=1,165)
#
# against a seeded "usual" of $1,329 - understated by more than half. The
# visible effect was that $1,347, the cheapest fare found anywhere in eight
# months of searching, was classified TYPICAL rather than CHEAP.
#
# These are deliberately the visa-free percentiles. The seed only applies
# until HISTORY has 25 observations across 5 distinct days, after which it
# is replaced by the same percentiles computed on live data.
#
# One consequence had to be fixed alongside this. The email used to say the
# saving was "below the $X travellers usually pay", which is a claim about
# what people *pay*. This is the median of what is *offered*, and travellers
# obviously do not buy the median - they buy the cheap end. At $1,329 the
# sentence was merely understated; at $2,866 it would have been a confident
# overclaim. The email now says "median visa-free fare seen for these
# dates", which is exactly what the number is.
SEED_LOW_USD = 2213      # p20 of observed visa-free fares
SEED_HIGH_USD = 3202     # p80
SEED_USUAL_USD = 2866    # median

# Percentile cut-offs once we are running off our own history.
CHEAP_PERCENTILE = 20
EXPENSIVE_PERCENTILE = 80
MIN_HISTORY_POINTS = 25
MIN_HISTORY_DAYS = 5   # one run can emit 25 rows; a distribution needs days


@dataclass(frozen=True)
class PriceBands:
    """Boundaries between the three verdicts, in USD."""

    low: int              # below this is CHEAP
    high: int             # above this is EXPENSIVE
    usual: int | None     # typical booking price, for the marker
    source: Source

    def classify(self, price_usd: float) -> Band:
        if price_usd < self.low:
            return "CHEAP"
        if price_usd > self.high:
            return "EXPENSIVE"
        return "TYPICAL"

    def position(self, price_usd: float) -> float:
        """Where the price sits on the bar, clamped to 0.0-1.0.

        The CHEAP zone occupies the first 25% of the bar and the EXPENSIVE
        zone the last 25%, matching the proportions Google uses, so the
        marker lands in the correctly coloured region.
        """
        span = max(self.high - self.low, 1)
        if price_usd < self.low:
            # Spread the cheap tail over the first quarter.
            floor = self.low - span * 0.5
            frac = (price_usd - floor) / max(self.low - floor, 1)
            return max(0.0, min(0.25, frac * 0.25))
        if price_usd > self.high:
            ceiling = self.high + span * 0.5
            frac = (price_usd - self.high) / max(ceiling - self.high, 1)
            return max(0.75, min(1.0, 0.75 + frac * 0.25))
        frac = (price_usd - self.low) / span
        return 0.25 + frac * 0.5


SEED_BANDS = PriceBands(
    low=SEED_LOW_USD, high=SEED_HIGH_USD, usual=SEED_USUAL_USD, source="SEED"
)


def bands_from_history(
    prices: Sequence[float], *, distinct_days: int | None = None
) -> PriceBands | None:
    """Percentile bands from observed prices, or None if too little data.

    Needs both enough observations and enough *days* of them. A single run
    can easily log 25 rows, and percentiles over one snapshot describe that
    snapshot, not the route.
    """
    clean = sorted(float(p) for p in prices if p and p > 0)
    if len(clean) < MIN_HISTORY_POINTS:
        return None
    if distinct_days is not None and distinct_days < MIN_HISTORY_DAYS:
        return None
    low = _percentile(clean, CHEAP_PERCENTILE)
    high = _percentile(clean, EXPENSIVE_PERCENTILE)
    if high <= low:
        return None
    return PriceBands(
        low=int(round(low)),
        high=int(round(high)),
        usual=int(round(statistics.median(clean))),
        source="HISTORY",
    )


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile. Input must already be sorted."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    weight = rank - lo
    return sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight


def median_bands(readings: Sequence[PriceBands]) -> PriceBands | None:
    """One set of bands from the several searches a run makes.

    Every search returns Google's insight for its own route and dates, so a
    run collects a dozen slightly different opinions. The median is used
    rather than the first or the last, so one odd route cannot drag the
    verdict printed in the email.
    """
    clean = [b for b in readings if b is not None]
    if not clean:
        return None
    usuals = [b.usual for b in clean if b.usual is not None]
    return PriceBands(
        low=int(statistics.median([b.low for b in clean])),
        high=int(statistics.median([b.high for b in clean])),
        usual=int(statistics.median(usuals)) if usuals else None,
        source="GOOGLE",
    )


def resolve_bands(
    *,
    google_bands: PriceBands | None = None,
    history_prices: Sequence[float] | None = None,
    history_days: int | None = None,
) -> PriceBands:
    """Pick the best available band source.

    Our own recorded prices come first, ahead of Google's, once there are
    enough of them across enough days. That ordering is deliberate and was
    the other way round.

    Google's price insights describe *every* routing it sells, including the
    US and Canadian transits this traveller cannot use. Measured
    2026-08-23 it called $1,800 the usual price, while the cheapest
    visa-free fare on offer that day was $1,347 and the median across 259
    browser-verified visa-free observations was $2,346. Quoting Google's
    number produced "$165 below the $1,800 travellers usually pay" beside a
    $1,347 headline: understated, and measured against a population the
    reader is not allowed to book from.

    The bar for using our own numbers is unchanged - 25 observations across
    5 distinct days - because percentiles over a single snapshot describe
    the snapshot rather than the route. Until that bar is met Google's
    bands remain the best guess available.
    """
    if history_prices:
        derived = bands_from_history(history_prices, distinct_days=history_days)
        if derived is not None:
            return derived
    if google_bands is not None:
        return google_bands
    return SEED_BANDS


# --- presentation ---------------------------------------------------------

BAND_LABEL: dict[Band, str] = {
    "CHEAP": "cheap",
    "TYPICAL": "typical",
    "EXPENSIVE": "expensive",
}

BAND_COLOR: dict[Band, str] = {
    "CHEAP": "#1e8e3e",     # Google green
    "TYPICAL": "#e37400",   # Google amber
    "EXPENSIVE": "#d93025",  # Google red
}

SOURCE_NOTE: dict[Source, str] = {
    "GOOGLE": ("Based on Google Flights' own price insights, which include "
               "routings through the US and Canada that you cannot use."),
    "HISTORY": ("Based on visa-free fares this tracker has verified in a "
                "browser - the ones you can actually book."),
    "SEED": (
        "Based on over 1,100 visa-free San Jose to Tokyo fares this tracker "
        "recorded, pending enough days of history to compute it live."
    ),
}


def verdict_sentence(price_usd: float, bands: PriceBands) -> str:
    """'$1,287 is cheap for this route' — the headline judgement."""
    band = bands.classify(price_usd)
    return f"${price_usd:,.0f} is {BAND_LABEL[band]} for this route"


def savings_vs_usual(price_usd: float, bands: PriceBands) -> int | None:
    """How many dollars below the usual booking price, if any."""
    if bands.usual is None:
        return None
    diff = int(round(bands.usual - price_usd))
    return diff if diff > 0 else None
