"""Turn raw fast-flights results into validated, comparable itineraries.

Two things here are easy to get wrong and are therefore done carefully:

1. Total duration. fast-flights gives a per-leg duration in minutes and
   local departure/arrival clock times. You cannot just subtract the first
   departure from the last arrival, because those are in different time
   zones and there is no offset in the payload. Instead we sum the leg
   durations (already true elapsed minutes) and add each layover, computed
   from arrival -> next departure *at the same airport*, which is by
   definition the same time zone. That is exact.

2. Visa safety. Every intermediate airport is checked against the deny
   list. Google's connecting-airport filter is only a hint, so this
   post-filter is what actually enforces the no-US/no-Canada rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime

from .airports import ban_reason, describe_hub, destination_codes


@dataclass(frozen=True)
class Leg:
    from_code: str
    to_code: str
    depart: datetime  # local to from_code
    arrive: datetime  # local to to_code
    duration_min: int
    aircraft: str = ""


@dataclass
class Itinerary:
    """One priced round-trip option, already validated."""

    price_usd: int
    legs: list[Leg]
    airlines: list[str]
    outbound_date: Date
    return_date: Date | None
    origin: str
    destination: str
    deep_link: str = ""
    hub_filter: str | None = None  # hub we asked Google for, if any
    outbound_leg_count: int = 0
    _duration_override: int | None = field(default=None, repr=False)

    # -- routing ----------------------------------------------------------
    @property
    def outbound_legs(self) -> list[Leg]:
        n = self.outbound_leg_count or len(self.legs)
        return self.legs[:n]

    @property
    def return_legs(self) -> list[Leg]:
        n = self.outbound_leg_count or len(self.legs)
        return self.legs[n:]

    @property
    def all_airports(self) -> list[str]:
        out: list[str] = []
        for leg in self.legs:
            if not out or out[-1] != leg.from_code:
                out.append(leg.from_code)
            out.append(leg.to_code)
        return out

    @property
    def destination_airports(self) -> frozenset[str]:
        """The airports that count as arriving, metro codes expanded."""
        return destination_codes(self.destination)

    @property
    def arrival_airport(self) -> str:
        """Where the outbound actually lands.

        With a metro-code search this is the real airport (HND or NRT), which
        is what the traveller needs to see; `destination` may only say TYO.
        """
        legs = self.outbound_legs
        return legs[-1].to_code if legs else self.destination

    @property
    def intermediate_airports(self) -> list[str]:
        """Airports we touch that are neither an origin nor a destination."""
        endpoints = {self.origin, *self.destination_airports, self.destination}
        seen: list[str] = []
        for code in self.all_airports:
            if code not in endpoints and code not in seen:
                seen.append(code)
        return seen

    @property
    def hubs(self) -> list[str]:
        return self.intermediate_airports

    @property
    def stops_outbound(self) -> int:
        return max(len(self.outbound_legs) - 1, 0)

    # -- duration ----------------------------------------------------------
    @property
    def outbound_duration_min(self) -> int:
        if self._duration_override is not None:
            return self._duration_override
        return segment_duration(self.outbound_legs)

    @property
    def total_flight_min(self) -> int:
        return sum(leg.duration_min for leg in self.legs)

    # -- presentation -------------------------------------------------------
    @property
    def route_label(self) -> str:
        return f"{self.origin}\u2013{self.arrival_airport}"

    @property
    def via_label(self) -> str:
        hubs = self.hubs
        if not hubs:
            return "Nonstop"
        return "via " + ", ".join(describe_hub(h) for h in hubs)

    @property
    def stops_label(self) -> str:
        n = self.stops_outbound
        return "Nonstop" if n == 0 else f"{n} stop" if n == 1 else f"{n} stops"

    @property
    def airlines_label(self) -> str:
        seen: list[str] = []
        for a in self.airlines:
            if a and a not in seen:
                seen.append(a)
        return ", ".join(seen) if seen else "Multiple airlines"

    @property
    def signature(self) -> str:
        """Stable id for dedupe across runs."""
        ret = self.return_date.isoformat() if self.return_date else "-"
        return (
            f"{self.origin}-{self.arrival_airport}|{self.outbound_date.isoformat()}"
            f"|{ret}|{'+'.join(self.hubs) or 'nonstop'}"
        )


def segment_duration(legs: list[Leg]) -> int:
    """Total elapsed minutes for one direction, layovers included."""
    if not legs:
        return 0
    total = sum(leg.duration_min for leg in legs)
    for prev, nxt in zip(legs, legs[1:]):
        total += layover_minutes(prev, nxt)
    return total


def layover_minutes(prev: Leg, nxt: Leg) -> int:
    """Minutes on the ground between two legs.

    Both timestamps are local to the same airport, so plain subtraction is
    correct. Negative or absurd values mean the payload was malformed, in
    which case we contribute nothing rather than corrupt the total.
    """
    delta = (nxt.depart - prev.arrive).total_seconds() / 60
    if delta < 0 or delta > 60 * 48:
        return 0
    return int(delta)


def format_duration(minutes: int) -> str:
    """'47 hr' / '23 hr 19 min', matching Google's own phrasing."""
    if minutes <= 0:
        return "\u2014"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    if hours:
        return f"{hours} hr"
    return f"{mins} min"


def format_price(usd: int) -> str:
    return f"${usd:,.0f}"


# --- construction from raw fast-flights objects ---------------------------


def _to_datetime(simple) -> datetime | None:
    """fast_flights SimpleDatetime -> datetime, tolerating partial data."""
    try:
        y, m, d = (int(x) for x in simple.date[:3])
        hh, mm = (int(x) for x in simple.time[:2])
        return datetime(y, m, d, hh, mm)
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def leg_from_single_flight(sf) -> Leg | None:
    depart = _to_datetime(sf.departure)
    arrive = _to_datetime(sf.arrival)
    if depart is None or arrive is None:
        return None
    try:
        from_code = str(sf.from_airport.code).upper()
        to_code = str(sf.to_airport.code).upper()
    except AttributeError:
        return None
    if not from_code or not to_code:
        return None
    duration = int(sf.duration or 0)
    if duration <= 0:
        # Fall back to the clock delta; only valid within one time zone but
        # better than reporting zero.
        duration = max(int((arrive - depart).total_seconds() / 60), 0)
    return Leg(
        from_code=from_code,
        to_code=to_code,
        depart=depart,
        arrive=arrive,
        duration_min=duration,
        aircraft=str(getattr(sf, "plane_type", "") or ""),
    )


def split_outbound(legs: list[Leg], destination: str) -> int:
    """Index after the leg that first arrives at the destination.

    `destination` may be a metro code, in which case landing at any airport
    inside it ends the outbound. Getting this wrong is not cosmetic: the
    whole round trip would count as the outbound and every itinerary would
    then blow through max_total_hours and be rejected.
    """
    wanted = destination_codes(destination)
    for i, leg in enumerate(legs):
        if leg.to_code in wanted:
            return i + 1
    return len(legs)


def build_itinerary(
    raw,
    *,
    origin: str,
    destination: str,
    outbound_date: Date,
    return_date: Date | None,
    deep_link: str = "",
    hub_filter: str | None = None,
) -> Itinerary | None:
    """Convert one fast_flights `Flights` object. Returns None if unusable."""
    price = getattr(raw, "price", None)
    if not isinstance(price, (int, float)) or price <= 0:
        return None

    legs: list[Leg] = []
    for sf in getattr(raw, "flights", []) or []:
        leg = leg_from_single_flight(sf)
        if leg is None:
            return None
        legs.append(leg)
    if not legs:
        return None

    return Itinerary(
        price_usd=int(round(price)),
        legs=legs,
        airlines=[str(a) for a in (getattr(raw, "airlines", []) or [])],
        outbound_date=outbound_date,
        return_date=return_date,
        origin=origin.upper(),
        destination=destination.upper(),
        deep_link=deep_link,
        hub_filter=hub_filter,
        outbound_leg_count=split_outbound(legs, destination),
    )


# --- validation -----------------------------------------------------------


@dataclass(frozen=True)
class Rejection:
    itinerary: Itinerary
    reason: str


def validate(
    itin: Itinerary,
    *,
    max_total_hours: int | None = None,
    min_layover_min: int = 0,
) -> str | None:
    """Return a rejection reason, or None if the itinerary is acceptable."""
    if not itin.destination_airports & set(itin.all_airports):
        return f"never reaches {itin.destination}"
    if itin.legs[0].from_code != itin.origin:
        return f"does not start at {itin.origin}"

    for code in itin.all_airports:
        reason = ban_reason(code)
        if reason:
            return f"routes through {code} ({reason})"

    if max_total_hours is not None:
        if itin.outbound_duration_min > max_total_hours * 60:
            return (
                f"outbound takes {format_duration(itin.outbound_duration_min)}, "
                f"over the {max_total_hours} hr cap"
            )

    if min_layover_min:
        for prev, nxt in zip(itin.legs, itin.legs[1:]):
            if prev.to_code != nxt.from_code:
                continue  # boundary between outbound and return
            gap = layover_minutes(prev, nxt)
            if 0 < gap < min_layover_min:
                return f"{gap} min layover at {prev.to_code} is too tight"

    return None


def partition(
    itineraries: list[Itinerary],
    *,
    max_total_hours: int | None = None,
    min_layover_min: int = 0,
) -> tuple[list[Itinerary], list[Rejection]]:
    """Split into (accepted, rejected)."""
    good: list[Itinerary] = []
    bad: list[Rejection] = []
    for itin in itineraries:
        reason = validate(
            itin, max_total_hours=max_total_hours, min_layover_min=min_layover_min
        )
        (bad.append(Rejection(itin, reason)) if reason else good.append(itin))
    return good, bad


def dedupe(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Keep the cheapest option per signature, cheapest first."""
    best: dict[str, Itinerary] = {}
    for itin in itineraries:
        key = itin.signature
        if key not in best or itin.price_usd < best[key].price_usd:
            best[key] = itin
    return sorted(best.values(), key=lambda i: (i.price_usd, i.outbound_duration_min))
