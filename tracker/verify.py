"""Re-price the windows that matter through Chrome, because HTTP lies.

The plain HTTP fetch does not merely miss long stays. On the trip owner's
own target window — SJO to Tokyo, 2027-01-29 to 2027-02-25, 27 nights —
it reported a cheapest of $1,635 (Lufthansa via Frankfurt) and $1,693
(Avianca). The actual cheapest was **$1,347** on Edelweiss/SWISS via
Zurich, confirmed on Google's booking page at 46 hr 20 min. That routing is
absent from the server-rendered HTML at any stop limit, so no amount of
grid coverage would ever have found it.

That matters more than it sounds. The trip owner's alert threshold is
$1,400, so the fares worth an email are exactly the ones HTTP cannot see.
A tracker that reports $1,635 as "the cheapest" is not merely incomplete,
it is wrong in the one direction that makes it useless.

Chrome sees them. It costs about 25 seconds a window against 3 for HTTP,
so it cannot price the whole grid — this module spends a small fixed
budget on the windows most likely to be cheap:

1. Windows the monthly wide net named, which are Google's own picks for
   the cheapest dates in each month.
2. The hot list — windows history already knows to be cheap.
3. Whatever the HTTP grid thought was cheapest this run, since that is the
   best guess available for anything the first two missed.

Everything Chrome returns is visa-checked exactly like the HTTP path, and
an option whose routing could not be read fails closed.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, replace
from datetime import date as Date
from datetime import timedelta
from typing import Callable, Iterable, Sequence

from fast_flights import FlightQuery, Passengers, create_query

from .browser import BrowserOption, chrome_path, fetch_dom, parse_options
from .search import RouteQuery, build_query

log = logging.getLogger(__name__)


# How far above the found price to cap the deep link. Tight enough that the
# link opens on the one flight rather than a page of thirteen, loose enough
# that a small overnight rise does not turn it into an empty page.
LINK_PRICE_MARGIN = 0.10


def within_duration(option, max_total_hours: int | None) -> bool:
    """Is this option inside the configured total-duration cap?

    The Chrome paths filtered on `visa_ok` alone, so `max_total_hours` -
    which `cli.collect` has always enforced on the HTTP grid through
    `itinerary.partition` - did nothing at all on the path that decides the
    alert price and the email headline. Nothing had exceeded it yet (the
    longest of 384 observed fares was 52.2 h against a 60 h cap), so this
    is closing a gap rather than fixing a visible symptom: tightening the
    cap would silently have had no effect where it mattered most.

    A duration of 0 means the page did not state one. That is not rejected:
    unlike the visa rule, an unreadable duration carries no legal risk, and
    failing closed here would throw away fares over a cosmetic field.
    """
    if not max_total_hours:
        return True
    minutes = getattr(option, "total_minutes", 0) or 0
    return minutes <= max_total_hours * 60


def booking_link(
    option: BrowserOption, *, max_stops: int | None = 2,
    margin: float = LINK_PRICE_MARGIN,
) -> str:
    """A link that opens Google Flights *on this fare*, not on a result list.

    Google's own booking URL encodes the individual flight numbers, and
    those only exist in the DOM after a result is clicked - which
    `--dump-dom` cannot do. A price cap gets to the same place: measured on
    the $1,347 Zurich fare, the uncapped search returned 13 options while
    the capped one returned exactly 1. So the trip owner lands on the
    flight and is one click from Google's Book button.
    """
    cap = int(round(option.price_usd * (1 + margin) / 10.0) * 10)
    legs = [
        FlightQuery(date=option.depart_date.isoformat(),
                    from_airport=option.origin, to_airport=option.destination,
                    max_stops=max_stops),
        FlightQuery(date=option.return_date.isoformat(),
                    from_airport=option.destination, to_airport=option.origin,
                    max_stops=max_stops),
    ]
    return create_query(
        flights=legs, trip="round-trip", seat="economy",
        passengers=Passengers(adults=1), currency="USD", language="en",
        max_price=cap,
    ).url()


@dataclass(frozen=True)
class VerifyTarget:
    depart: Date
    ret: Date
    source: str                 # "hint" | "hot" | "grid" - for the log only

    @property
    def key(self) -> str:
        return f"{self.depart.isoformat()}_{self.ret.isoformat()}"


def _parse_key(key: str) -> tuple[Date, Date] | None:
    try:
        a, b = key.split("_")
        return Date.fromisoformat(a), Date.fromisoformat(b)
    except (ValueError, AttributeError):
        return None


def choose_targets(
    *,
    hint_keys: Sequence[str] = (),
    hot_keys: Sequence[str] = (),
    grid_keys: Sequence[str] = (),
    limit: int = 10,
    today: Date | None = None,
    min_lead_days: int = 0,
) -> list[VerifyTarget]:
    """The windows worth spending a Chrome launch on, best first.

    Ordering is the point: wide-net hints are Google's own cheapest-date
    picks and earn the first launches, then windows history knows are
    cheap, then the grid's best guess. Duplicates collapse to their
    highest-priority source, and a window already in the past is dropped.
    """
    today = today or Date.today()
    floor = today + timedelta(days=min_lead_days)

    out: list[VerifyTarget] = []
    seen: set[str] = set()
    for source, keys in (("hint", hint_keys), ("hot", hot_keys), ("grid", grid_keys)):
        for key in keys:
            if key in seen or len(out) >= limit:
                continue
            pair = _parse_key(key)
            if pair is None:
                continue
            depart, ret = pair
            if depart < floor or ret <= depart:
                continue
            seen.add(key)
            out.append(VerifyTarget(depart, ret, source))
        if len(out) >= limit:
            break
    return out


def verify(
    targets: Iterable[VerifyTarget],
    *,
    origin: str = "SJO",
    destination: str = "NRT",
    max_stops: int | None = 2,
    chrome: str | None = None,
    chrome_override: str = "",
    timeout_s: int = 120,
    budget_ms: int = 30000,
    fetch: Callable[..., str] | None = None,
    sleep: Callable[[float], None] | None = None,
    delay_s: float = 2.0,
    jitter_s: float = 2.0,
    max_total_hours: int | None = None,
) -> list[BrowserOption]:
    """Price each target through Chrome. Visa-rejected options are dropped.

    `fetch` is injectable so tests never launch a browser. Returns every
    surviving option across all targets, cheapest first.
    """
    exe = chrome or chrome_path(chrome_override)
    if exe is None and fetch is None:
        log.info("Chrome not found; skipping verification "
                 "(set chrome_path in config.yaml if it is installed elsewhere)")
        return []

    grab = fetch or (lambda url: fetch_dom(url, chrome=exe, timeout=timeout_s,
                                           virtual_time_budget_ms=budget_ms))
    found: list[BrowserOption] = []
    for n, t in enumerate(targets):
        query = build_query(RouteQuery(origin=origin, destination=destination,
                                       outbound=t.depart, inbound=t.ret,
                                       max_stops=max_stops, hub=None))
        url = query.url()
        dom = grab(url)
        options = parse_options(dom, origin=origin, destination=destination,
                                depart_date=t.depart, return_date=t.ret,
                                deep_link=url)
        usable = [
            replace(o, deep_link=booking_link(o, max_stops=max_stops))
            for o in options
            if o.visa_ok and within_duration(o, max_total_hours)
        ]
        rejected = len(options) - len(usable)
        if options:
            log.info("Chrome %s %s -> %s +%dn: %d option(s), %d visa-rejected, "
                     "cheapest usable %s",
                     t.source, t.depart, t.ret, (t.ret - t.depart).days,
                     len(options), rejected,
                     f"${min(o.price_usd for o in usable):,}" if usable else "none")
        else:
            log.info("Chrome %s %s +%dn: nothing returned",
                     t.source, t.depart, (t.ret - t.depart).days)
        found.extend(usable)
        # Jittered, like every other path that reaches Google. A fixed wait
        # is a fingerprint: nobody browses on a perfect clock. This one was
        # the last flat delay left after `monthly.scan_months` was paced on
        # 2026-08-23 - less glaring than the wide net's 1.5s burst, because
        # a Chrome launch takes a variable 6-18s and blurs the cadence on
        # its own, but there is no reason to rely on that.
        if sleep and delay_s:
            sleep(delay_s + random.uniform(0, max(jitter_s, 0.0)))

    found.sort(key=lambda o: o.price_usd)
    return found


def cheapest(options: Sequence[BrowserOption]) -> BrowserOption | None:
    return min(options, key=lambda o: o.price_usd) if options else None


def under(options: Sequence[BrowserOption], threshold: int) -> list[BrowserOption]:
    """Options at or under the alert threshold, cheapest first."""
    return sorted((o for o in options if o.price_usd <= threshold),
                  key=lambda o: o.price_usd)
