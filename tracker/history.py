"""Append-only price log, and the rolling baseline derived from it."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .itinerary import Itinerary

log = logging.getLogger(__name__)

FIELDS = [
    "checked_at_utc",
    "origin",
    "destination",
    "depart_date",
    "return_date",
    "price_usd",
    "duration_min",
    "stops",
    "hubs",
    "airlines",
    "band",
    "band_source",
    "deep_link",
]


@dataclass
class Row:
    checked_at_utc: str
    origin: str
    destination: str
    depart_date: str
    return_date: str
    price_usd: int
    duration_min: int
    stops: int
    hubs: str
    airlines: str
    band: str
    band_source: str
    deep_link: str

    def as_dict(self) -> dict[str, object]:
        return {f: getattr(self, f) for f in FIELDS}


def rows_from_verified(
    options,
    *,
    band_of,
    band_source: str = "CHROME",
    checked_at: datetime | None = None,
) -> list[Row]:
    """History rows for Chrome-verified options.

    These matter more than the HTTP rows beside them. Measured across four
    windows, HTTP's cheapest visa-free fare was $2,509 / $2,866 / $3,057 /
    $3,179 where Chrome found $1,347 / $1,432 / $2,688 / $3,093. A hot list
    trained only on the HTTP numbers is being taught that every window is
    expensive, and will keep re-pricing the wrong ones. Logging what Chrome
    actually saw is what lets the hot list converge on real bargains.

    `duration_min` is the whole round trip here, not the outbound leg as in
    `rows_from` - the collapsed DOM does not split it. The column is only
    ever read for display, never for the duration maths.
    """
    stamp = (checked_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return [
        Row(
            checked_at_utc=stamp,
            origin=o.origin,
            destination=o.destination,
            depart_date=o.depart_date.isoformat(),
            return_date=o.return_date.isoformat(),
            price_usd=o.price_usd,
            duration_min=o.total_minutes,
            stops=o.stop_count,
            hubs="+".join(o.stops),
            airlines=";".join(o.airlines),
            band=band_of(o.price_usd),
            band_source=band_source,
            deep_link=o.deep_link,
        )
        for o in options
    ]


def rows_from(
    itineraries: Iterable[Itinerary],
    *,
    band_of,
    band_source: str,
    checked_at: datetime | None = None,
) -> list[Row]:
    stamp = (checked_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    out: list[Row] = []
    for i in itineraries:
        out.append(
            Row(
                checked_at_utc=stamp,
                origin=i.origin,
                destination=i.destination,
                depart_date=i.outbound_date.isoformat(),
                return_date=i.return_date.isoformat() if i.return_date else "",
                price_usd=i.price_usd,
                duration_min=i.outbound_duration_min,
                stops=i.stops_outbound,
                hubs="+".join(i.hubs),
                airlines=";".join(i.airlines),
                band=band_of(i.price_usd),
                band_source=band_source,
                deep_link=i.deep_link,
            )
        )
    return out


def append(path: str | Path, rows: Sequence[Row]) -> int:
    """Append rows, writing the header if the file is new. Returns count."""
    if not rows:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())
    return len(rows)


def _field(rec: dict, name: str) -> str:
    """One CSV field as a string, whatever the row looks like.

    `csv.DictReader` fills missing fields with None, so a truncated row -
    the kind an append leaves behind when the process is killed mid-write -
    yields None rather than "". `rec.get(name, "")` returns that None and
    `.upper()` on it raises.

    That is not hypothetical. On 2026-08-24 a hard kill of the sweeper left
    a single line reading "0" in sweep_history.csv, and from then on **every
    scheduled run crashed** at this exact call - four hours with no email,
    while the sweep itself carried on happily. One malformed line took down
    the product.

    An append-only log written by a process that can be killed will always
    be able to end mid-line, so the reader is the place to be tolerant.
    """
    value = rec.get(name)
    return value if isinstance(value, str) else ""


def _rows(p: Path):
    """Yield the rows of a CSV that something else may be writing right now.

    `sweep_history.csv` is appended to by the sweeper roughly every ninety
    seconds, for ever, while the six scheduled runs read it. A read can
    therefore land mid-append, and on Windows the tail of a file extended
    but not yet flushed reads back as NUL bytes - which `csv` reports as
    `_csv.Error: line contains NUL`, not as anything resembling "try again".

    That killed the 09:03 run on 2026-08-24: exit code 1, no traceback, the
    log simply stopping after the grid phase. It is the same lesson as the
    malformed row that killed every run for four hours the day before -
    **never crash on a file you only read** - but one level up, because
    that fix guarded the fields and this one has to guard the iteration.

    Stopping at the torn row is right rather than merely safe: every row
    already read is valid and complete, and the missing tail is a handful
    of the newest observations, which the next run picks up anyway.
    """
    try:
        with p.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            while True:
                try:
                    yield next(reader)
                except StopIteration:
                    return
                except csv.Error as exc:
                    log.debug("%s is being written (%s); using the %s",
                              p.name, exc, "rows read so far")
                    return
    except OSError as exc:
        log.warning("could not read %s (%s); continuing without it",
                    p.name, exc)


def read_prices(
    path: str | Path,
    *,
    origin: str | None = None,
    destination: str | None = None,
    since: Date | None = None,
    band_source: str | None = None,
) -> list[float]:
    """Historical prices for the baseline, optionally filtered.

    `band_source="CHROME"` restricts to the browser-verified rows, which is
    what the baseline should be built from. The two populations in this file
    are not comparable: measured over 692 rows on one day, the HTTP rows had
    a median of $2,866 and the Chrome rows $2,346, because HTTP cannot see
    the cheap European routings at all. Averaging them together describes
    neither.
    """
    p = Path(path)
    if not p.exists():
        return []
    prices: list[float] = []
    for rec in _rows(p):
        if origin and _field(rec, "origin").upper() != origin.upper():
            continue
        if destination and _field(rec, "destination").upper() != destination.upper():
            continue
        if band_source and _field(rec, "band_source") != band_source:
            continue
        if since:
            stamp = _field(rec, "checked_at_utc")[:10]
            try:
                if datetime.strptime(stamp, "%Y-%m-%d").date() < since:
                    continue
            except ValueError:
                continue
        try:
            value = float(rec.get("price_usd") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            prices.append(value)
    return prices


def distinct_days(path: str | Path, *, origin: str | None = None) -> int:
    """How many separate calendar days the log covers."""
    p = Path(path)
    if not p.exists():
        return 0
    days: set[str] = set()
    for rec in _rows(p):
        if origin and _field(rec, "origin").upper() != origin.upper():
            continue
        stamp = _field(rec, "checked_at_utc")[:10]
        if len(stamp) == 10:
            days.add(stamp)
    return len(days)
