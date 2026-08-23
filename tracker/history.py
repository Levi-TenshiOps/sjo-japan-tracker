"""Append-only price log, and the rolling baseline derived from it."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .itinerary import Itinerary

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


def read_prices(
    path: str | Path,
    *,
    origin: str | None = None,
    destination: str | None = None,
    since: Date | None = None,
) -> list[float]:
    """Historical prices for the baseline, optionally filtered."""
    p = Path(path)
    if not p.exists():
        return []
    prices: list[float] = []
    with p.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            if origin and rec.get("origin", "").upper() != origin.upper():
                continue
            if destination and rec.get("destination", "").upper() != destination.upper():
                continue
            if since:
                stamp = rec.get("checked_at_utc", "")[:10]
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
    with p.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            if origin and rec.get("origin", "").upper() != origin.upper():
                continue
            stamp = (rec.get("checked_at_utc") or "")[:10]
            if len(stamp) == 10:
                days.add(stamp)
    return len(days)
