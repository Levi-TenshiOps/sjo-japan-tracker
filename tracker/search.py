"""Talk to Google Flights via fast-flights, politely.

Design notes
------------
* Deep links come from `Query.url()`, which rebuilds the exact `?tfs=`
  protobuf we searched with. Clicking a row in the email therefore lands on
  the same filtered result set, which is what makes the links trustworthy.
* `connecting_airports` is an include-hint. We still post-filter every
  itinerary in `itinerary.validate`, so a Google filter miss cannot leak a
  US or Canadian connection into the email.
* Everything network-facing is funnelled through `_fetch`, so tests can
  inject a fake and the whole pipeline runs offline.
* Google throttles. We jitter delays, back off on failure, and cap the
  number of requests per run rather than hammering until we are blocked.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Callable, Iterable, Sequence

from fast_flights import (
    FlightQuery, Passengers, create_query, fetch_flights_html,
)
from fast_flights.exceptions import FlightsNotFound
from fast_flights.parser import parse as parse_flights_html

from .airports import BANNED_AIRPORTS
from .itinerary import Itinerary, build_itinerary
from .pricing import PriceBands

log = logging.getLogger(__name__)

BASE_DELAY_SECONDS = 3.0
JITTER_SECONDS = 2.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.5

# A request that raised is worth retrying: the network or the parser blipped.
# A request that came back parsed but *empty* usually means Google genuinely
# has nothing for that route on that date, and retrying it twice more spends
# three requests to learn the same thing. Empty responses therefore get one
# retry, not two, so the budget stays pointed at windows nobody has priced.
MAX_EMPTY_RETRIES = 1


@dataclass(frozen=True)
class RouteQuery:
    """One search we intend to run."""

    origin: str
    destination: str
    outbound: Date
    inbound: Date | None
    hub: str | None = None          # None = let Google pick (broad sweep)
    max_stops: int = 2

    @property
    def label(self) -> str:
        via = f" via {self.hub}" if self.hub else " (any route)"
        back = f" / {self.inbound}" if self.inbound else ""
        return f"{self.origin}-{self.destination} {self.outbound}{back}{via}"

    @property
    def cache_key(self) -> str:
        return (
            f"{self.origin}|{self.destination}|{self.outbound}|{self.inbound}"
            f"|{self.hub or '*'}|{self.max_stops}"
        )


@dataclass
class SearchOutcome:
    query: RouteQuery
    itineraries: list[Itinerary] = field(default_factory=list)
    google_bands: PriceBands | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.itineraries)


def build_query(rq: RouteQuery, *, currency: str = "USD", language: str = "en"):
    """Build the fast-flights Query object for a RouteQuery."""
    legs = [
        FlightQuery(
            date=rq.outbound.isoformat(),
            from_airport=rq.origin,
            to_airport=rq.destination,
            max_stops=rq.max_stops,
            connecting_airports=[rq.hub] if rq.hub else None,
        )
    ]
    trip = "one-way"
    if rq.inbound is not None:
        trip = "round-trip"
        legs.append(
            FlightQuery(
                date=rq.inbound.isoformat(),
                from_airport=rq.destination,
                to_airport=rq.origin,
                max_stops=rq.max_stops,
                connecting_airports=[rq.hub] if rq.hub else None,
            )
        )
    return create_query(
        flights=legs,
        trip=trip,
        seat="economy",
        passengers=Passengers(adults=1),
        currency=currency,
        language=language,
    )


def deep_link(rq: RouteQuery, *, currency: str = "USD", language: str = "en") -> str:
    """A Google Flights URL reproducing exactly this search."""
    return build_query(rq, currency=currency, language=language).url()


# --- Google's own price insights -----------------------------------------
# fast-flights' parser drops this, so we dig it out of the raw payload
# ourselves. The structure is undocumented and may change, hence the very
# defensive parsing and the graceful fall back to None.

_LOW_HIGH = re.compile(r"[\"']?(\d{2,7})[\"']?\s*,\s*[\"']?(\d{2,7})[\"']?")


def extract_google_bands(html: str) -> PriceBands | None:
    """Best-effort pull of Google's typical-price range from the page.

    Returns None whenever anything looks off — a wrong answer here is worse
    than no answer, because the email would state it as fact.
    """
    if not html:
        return None
    try:
        payload = _extract_payload(html)
    except Exception:  # noqa: BLE001 - undocumented structure, stay quiet
        return None
    if payload is None:
        return None

    candidate = _insight_at_known_paths(payload) or _find_price_insight(payload)
    if candidate is None:
        return None

    low, high, usual = candidate
    # Sanity: a long-haul economy round trip is not $12 and not $90,000.
    if not (100 <= low < high <= 20_000):
        return None
    if usual is not None and not (low * 0.5 <= usual <= high * 1.5):
        usual = None
    return PriceBands(low=int(low), high=int(high), usual=usual, source="GOOGLE")


# Positions confirmed against live payloads from four different searches
# (SJO-NRT, SJO-HND, MEX-NRT and one that returned nothing at all):
#   payload[7][3]    -> [low, high], the ends of Google's own price bar
#   payload[7][2][2] -> the price Google labels as what travellers usually pay
# The blind walk below stays as a fallback for when Google moves things.
GOOGLE_RANGE_PATH = (7, 3)
GOOGLE_USUAL_PATH = (7, 2, 2)


def _at(payload, path):
    node = payload
    for i in path:
        if not isinstance(node, list) or i >= len(node):
            return None
        node = node[i]
    return node


def _insight_at_known_paths(payload):
    """(low, high, usual) read from the positions Google actually uses."""
    pair = _at(payload, GOOGLE_RANGE_PATH)
    if not (isinstance(pair, list) and len(pair) == 2):
        return None
    low, high = pair
    if not all(isinstance(x, (int, float)) for x in (low, high)):
        return None
    if not low < high:
        return None
    usual = _at(payload, GOOGLE_USUAL_PATH)
    if not isinstance(usual, (int, float)) or not low < usual < high:
        usual = None
    return int(low), int(high), (int(usual) if usual is not None else None)


def _extract_payload(html: str):
    marker = "data:"
    idx = html.find("script class=\"ds:1\"")
    if idx == -1:
        idx = html.find("ds:1")
    if idx == -1:
        return None
    chunk = html[idx:]
    start = chunk.find(marker)
    if start == -1:
        return None
    body = chunk[start + len(marker):]
    depth = 0
    for i, ch in enumerate(body):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(body[: i + 1])
    return None


def _find_price_insight(node, depth: int = 0):
    """Walk the payload for a [low, high] pair that looks like a price range."""
    if depth > 12 or not isinstance(node, list):
        return None
    nums = [x for x in node if isinstance(x, (int, float))]
    if len(nums) >= 2:
        lo, hi = min(nums[:3]), max(nums[:3])
        if 100 <= lo < hi <= 20_000 and hi >= lo * 1.2:
            usual = None
            for x in nums:
                if lo < x < hi:
                    usual = int(x)
                    break
            return int(lo), int(hi), usual
    for child in node:
        found = _find_price_insight(child, depth + 1)
        if found is not None:
            return found
    return None


# --- fetching -------------------------------------------------------------

FetchFn = Callable[[object], object]


class _Fetched(list):
    """The parsed itineraries, with the page they came from still attached.

    Keeping the HTML lets the caller pull Google's own price insight out of
    the same request instead of spending a second one on it. Injected test
    fakes return a plain list and simply have no `raw_html`.
    """

    raw_html: str = ""


def _default_fetch(query):
    """Fetch and parse one search, failing soft on an unfamiliar payload.

    fast-flights reads an undocumented structure. When a search genuinely has
    no itineraries Google omits the whole itinerary block, and the upstream
    guard only covers one level of that - it raises TypeError instead of
    returning nothing (confirmed live on SJO-KIX). Every parse failure is
    therefore treated as "this search found nothing", which is both the
    likeliest truth and the safe reading: an exception here would otherwise
    burn the full retry allowance rediscovering the same empty page.
    """
    html = fetch_flights_html(query)
    try:
        results = list(parse_flights_html(html))
    except FlightsNotFound:
        results = []
    except (TypeError, IndexError, KeyError, ValueError) as exc:
        log.info("payload did not parse (%s: %s); treating as no results",
                 type(exc).__name__, exc)
        results = []
    out = _Fetched(results)
    out.raw_html = html
    return out


@dataclass
class _Progress:
    """Per-query retry bookkeeping, so run_all can interleave attempts."""

    outcome: SearchOutcome
    tries: int = 0
    empty_tries: int = 0
    done: bool = False
    last_error: str | None = None

    def wants_another_try(self, *, max_retries: int, max_empty_retries: int) -> bool:
        if self.done or self.tries >= max_retries:
            return False
        return self.empty_tries <= max_empty_retries


class Searcher:
    """Runs RouteQueries with caching, jitter, retries and a request budget."""

    def __init__(
        self,
        *,
        fetch: FetchFn | None = None,
        delay: float = BASE_DELAY_SECONDS,
        jitter: float = JITTER_SECONDS,
        max_retries: int = MAX_RETRIES,
        max_empty_retries: int = MAX_EMPTY_RETRIES,
        max_requests: int | None = None,
        currency: str = "USD",
        language: str = "en",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fetch = fetch or _default_fetch
        self.delay = delay
        self.jitter = jitter
        self.max_retries = max_retries
        self.max_empty_retries = max_empty_retries
        self.max_requests = max_requests
        self.currency = currency
        self.language = language
        self.sleep = sleep
        self.requests_made = 0
        self.barren_requests = 0   # attempts that yielded no itinerary at all
        self.requests_by_destination: dict[str, int] = {}
        self.barren_by_destination: dict[str, int] = {}
        self._cache: dict[str, SearchOutcome] = {}

    @property
    def budget_exhausted(self) -> bool:
        return self.max_requests is not None and self.requests_made >= self.max_requests

    @property
    def empty_rate(self) -> float:
        """Share of requests that produced nothing.

        Counted per *request*, not per query. Per-query understates it badly:
        three retries of one dead search are three requests that bought
        nothing, and the throttle has to see that to react to it.
        """
        if not self.requests_made:
            return 0.0
        return self.barren_requests / self.requests_made

    # -- one attempt -------------------------------------------------------
    def _attempt(self, rq: RouteQuery, query, link: str, prog: _Progress) -> None:
        """Spend exactly one request on `rq` and fold the result into `prog`."""
        if prog.tries:
            self.sleep(self.delay * (BACKOFF_FACTOR ** prog.tries))
        prog.tries += 1
        self.requests_made += 1
        dest = rq.destination
        self.requests_by_destination[dest] = (
            self.requests_by_destination.get(dest, 0) + 1)
        try:
            result = self.fetch(query)
        except Exception as exc:  # noqa: BLE001 - scraper throws anything
            self._count_barren(dest)
            prog.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("%s attempt %d failed: %s", rq.label, prog.tries,
                        prog.last_error)
            self._pause()
            return

        html = getattr(result, "raw_html", "")
        if html and prog.outcome.google_bands is None:
            prog.outcome.google_bands = extract_google_bands(html)

        itins = self._convert(result, rq, link)
        if itins:
            prog.outcome.itineraries = itins
            prog.last_error = None
            prog.done = True
            self._pause()
            return

        self._count_barren(dest)
        prog.empty_tries += 1
        prog.last_error = "no results"
        log.info("%s attempt %d: no results", rq.label, prog.tries)
        self._pause()

    def _count_barren(self, dest: str) -> None:
        self.barren_requests += 1
        self.barren_by_destination[dest] = (
            self.barren_by_destination.get(dest, 0) + 1)

    def _start(self, rq: RouteQuery):
        """A cached outcome, or a fresh progress record to work on."""
        if rq.cache_key in self._cache:
            return self._cache[rq.cache_key]
        return _Progress(SearchOutcome(rq))

    def _finish(self, rq: RouteQuery, prog: _Progress) -> SearchOutcome:
        if prog.last_error:
            prog.outcome.error = prog.last_error
        self._cache[rq.cache_key] = prog.outcome
        return prog.outcome

    def _exhausted(self, rq: RouteQuery) -> SearchOutcome:
        outcome = SearchOutcome(rq, error="request budget exhausted")
        self._cache[rq.cache_key] = outcome
        return outcome

    # -- public API --------------------------------------------------------
    def run(self, rq: RouteQuery) -> SearchOutcome:
        started = self._start(rq)
        if isinstance(started, SearchOutcome):
            return started
        if self.budget_exhausted:
            return self._exhausted(rq)

        query = build_query(rq, currency=self.currency, language=self.language)
        link = query.url()
        prog = started
        while prog.wants_another_try(
            max_retries=self.max_retries, max_empty_retries=self.max_empty_retries
        ):
            if self.budget_exhausted:
                break
            self._attempt(rq, query, link, prog)
        return self._finish(rq, prog)

    def run_all(self, queries: Iterable[RouteQuery]) -> list[SearchOutcome]:
        """Search every query, breadth-first: one attempt each, then retries.

        Retrying depth-first - three attempts on the first query before the
        second is touched at all - lets a handful of dead searches eat the
        request budget and starve the tail of the plan. Those windows are
        never priced, yet the rotation cursor advances past them anyway, so
        they wait a whole cycle for another chance. Breadth-first guarantees
        every planned window is searched once before a single retry is spent.
        """
        queries = list(queries)
        results: dict[int, SearchOutcome] = {}
        pending: list[tuple[int, RouteQuery, object, str, _Progress]] = []

        for i, rq in enumerate(queries):
            started = self._start(rq)
            if isinstance(started, SearchOutcome):
                results[i] = started
                continue
            query = build_query(rq, currency=self.currency, language=self.language)
            pending.append((i, rq, query, query.url(), started))

        while pending:
            still: list[tuple[int, RouteQuery, object, str, _Progress]] = []
            for i, rq, query, link, prog in pending:
                if self.budget_exhausted:
                    results[i] = (
                        self._exhausted(rq) if prog.tries == 0
                        else self._finish(rq, prog)
                    )
                    continue
                self._attempt(rq, query, link, prog)
                if prog.wants_another_try(
                    max_retries=self.max_retries,
                    max_empty_retries=self.max_empty_retries,
                ):
                    still.append((i, rq, query, link, prog))
                else:
                    results[i] = self._finish(rq, prog)
            pending = still

        return [results[i] for i in range(len(queries))]

    def _convert(self, result, rq: RouteQuery, link: str) -> list[Itinerary]:
        out: list[Itinerary] = []
        for raw in result or []:
            itin = build_itinerary(
                raw,
                origin=rq.origin,
                destination=rq.destination,
                outbound_date=rq.outbound,
                return_date=rq.inbound,
                deep_link=link,
                hub_filter=rq.hub,
            )
            if itin is not None:
                out.append(itin)
        return out

    def _pause(self) -> None:
        if self.delay <= 0:
            return
        self.sleep(self.delay + random.uniform(0, self.jitter))


# --- query planning -------------------------------------------------------


def plan_broad(
    *,
    origins: Sequence[str],
    destinations: Sequence[str],
    date_pairs: Sequence[tuple[Date, Date | None]],
    max_stops: int = 2,
) -> list[RouteQuery]:
    """Unfiltered sweep: let Google surface whatever it likes, then filter.

    Cheap (one request per origin/destination/date combination) and the best
    way to discover routings nobody thought to whitelist.
    """
    return [
        RouteQuery(o, d, out, back, hub=None, max_stops=max_stops)
        for o in origins
        for d in destinations
        for out, back in date_pairs
    ]


def plan_hub_sweep(
    *,
    origins: Sequence[str],
    destinations: Sequence[str],
    date_pairs: Sequence[tuple[Date, Date | None]],
    hubs: Sequence[str],
    max_stops: int = 2,
) -> list[RouteQuery]:
    """Force Google to price each hub, including ones it would bury."""
    return [
        RouteQuery(o, d, out, back, hub=h, max_stops=max_stops)
        for o in origins
        for d in destinations
        for out, back in date_pairs
        for h in hubs
        if h.upper() not in BANNED_AIRPORTS
    ]
