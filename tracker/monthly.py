"""Google's own "cheapest dates" hint, one request per month.

The date grid prices one (depart, return) window per request, so an 8-month
window at 11 trip lengths is ~2,450 requests for a full pass — over two
weeks even at the ceiling budget. That is fine for *tracking* a fare and
far too slow for *finding* one.

Google will do the searching for us. A plain-text query ("Flights from SJO
to NRT in February 2027") comes back with a recommendation rendered into
the page as, literally:

    Travel Jan 29 - Feb 25, 2027 for $1,347

That is Google's cheapest round trip for the month, found in one request.
Eight requests therefore cover the whole search window with a far better
answer than the grid reaches in a fortnight — measured 2026-08-22, the
three months that returned a hint named $1,604, $1,432 and $1,347, while
26 grid requests that same afternoon bottomed out at $2,197.

So this is the wide net: cheap, shallow, and pointed at the right dates.
What it returns is a *candidate*, not a verified itinerary — the hint gives
no airline, no stops and no routing, so it says nothing about whether the
trip is visa-free. Feed the window into the hot list and let the ordinary
search price it properly; `itinerary.validate()` still decides.

Two known limits, both measured:

* Not every month returns a hint. Five of eight did not on the first pass;
  the page shows an interstitial instead. That is normal, not an error.
* The hint can name a stay longer than the grid can verify. Round trips
  past ~30 nights exist but are absent from the server-rendered HTML this
  project scrapes, so a hint of 32 nights will price as empty. It is still
  worth surfacing, because it tells the trip owner a cheaper fare is there
  even when the tracker cannot confirm it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

MONTH_NUM = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

_LAST_DAY = {"January": 31, "February": 28, "March": 31, "April": 30,
             "May": 31, "June": 30, "July": 31, "August": 31,
             "September": 30, "October": 31, "November": 30, "December": 31}

MONTH_FULL = {i: m for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# "Travel Sep 30 - Oct 30 for $1,604"
# "Travel Jan 29 - Feb 25, 2027 for $1,347"
# The year is present on some renderings and absent on others, and the
# second month is omitted when the range stays inside one month.
_HINT = re.compile(
    r"Travel\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:,\s*(\d{4}))?"
    r"\s*[–—-]\s*"
    r"(?:([A-Z][a-z]{2})\s+)?(\d{1,2})(?:,\s*(\d{4}))?"
    r"\s+for\s+\$([0-9,]+)"
)

_TAGS = re.compile(r"(?s)<[^>]+>")
_SCRIPTS = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


@dataclass(frozen=True)
class MonthHint:
    """Google's cheapest suggestion for one month. Unverified."""
    month: str            # the label we asked about, e.g. "February 2027"
    depart: Date
    ret: Date
    price_usd: int

    @property
    def nights(self) -> int:
        return (self.ret - self.depart).days

    @property
    def key(self) -> str:
        """Must match `schedule.Window.key` exactly or the hot list misses."""
        return f"{self.depart.isoformat()}_{self.ret.isoformat()}"

    def describe(self) -> str:
        return (f"{self.month}: {self.depart} -> {self.ret} "
                f"({self.nights}n) ${self.price_usd:,}")


def visible_text(html: str) -> str:
    """Page text with markup and scripts stripped, whitespace collapsed."""
    return re.sub(r"\s+", " ", _TAGS.sub(" ", _SCRIPTS.sub(" ", html)))


def parse_hint(html_or_text: str, *, month: str, anchor_year: int) -> MonthHint | None:
    """Pull the "Travel A - B for $P" recommendation out of a page.

    `anchor_year` fills in a year the page omitted. A range that runs
    backwards across a month boundary (Dec -> Jan) rolls the year forward.
    Returns None when the page carries no recommendation, which is common
    and not an error.
    """
    text = visible_text(html_or_text) if "<" in html_or_text else html_or_text
    m = _HINT.search(text)
    if not m:
        return None
    mon1, day1, yr1, mon2, day2, yr2, price = m.groups()
    mon2 = mon2 or mon1
    if mon1 not in MONTH_NUM or mon2 not in MONTH_NUM:
        return None

    year1 = int(yr1) if yr1 else anchor_year
    year2 = int(yr2) if yr2 else year1
    if not yr2 and MONTH_NUM[mon2] < MONTH_NUM[mon1]:
        year2 = year1 + 1          # Dec 30 -> Jan 20 straddles new year

    try:
        depart = Date(year1, MONTH_NUM[mon1], int(day1))
        ret = Date(year2, MONTH_NUM[mon2], int(day2))
    except ValueError:
        return None                # Feb 30 and friends
    if ret <= depart:
        return None
    return MonthHint(month=month, depart=depart, ret=ret,
                     price_usd=int(price.replace(",", "")))


def months_in_window(earliest: Date, latest: Date) -> list[tuple[str, int]]:
    """(label, year) for every calendar month the search window touches."""
    out: list[tuple[str, int]] = []
    y, m = earliest.year, earliest.month
    while (y, m) <= (latest.year, latest.month):
        out.append((f"{MONTH_FULL[m]} {y}", y))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


def month_halves(months: Sequence[tuple[str, int]]) -> list[tuple[str, str, int]]:
    """(query fragment, label, anchor year) for each half of each month.

    Google answers one recommendation per query, so a narrower range can
    surface a window the whole-month query never mentions - measured
    2026-08-22, "January 16 to January 31 2027" named a $1,387 window that
    "in January 2027" did not. It is additive, not a replacement: five of
    eight narrow queries that day returned no hint at all.
    """
    out: list[tuple[str, str, int]] = []
    for label, year in months:
        name, _, yr = label.rpartition(" ")
        yr = int(yr) if yr.isdigit() else year
        last = _LAST_DAY.get(name, 30)
        if name == "February" and yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0):
            last = 29
        out.append((f"{name} 1 to {name} 15 {yr}", f"{label} (1st half)", yr))
        out.append((f"{name} 16 to {name} {last} {yr}", f"{label} (2nd half)", yr))
    return out


def scan_months(
    fetch: Callable[[str], str],
    months: Sequence[tuple[str, int]],
    *,
    destination: str = "NRT",
    origin: str = "SJO",
    min_nights: int | None = None,
    max_nights: int | None = None,
    halves: bool = False,
) -> list[MonthHint]:
    """One request per month; returns whatever hints came back.

    `fetch` takes the query string and returns page HTML, so tests inject a
    fake and never touch the network. A month that raises or returns nothing
    is skipped rather than aborting the sweep — a wide net with a hole in it
    still beats no net.
    """
    probes = [(f"in {label}", label, year) for label, year in months]
    if halves:
        probes += month_halves(months)

    hints: list[MonthHint] = []
    seen_windows: set[str] = set()
    for fragment, label, year in probes:
        query = f"Flights from {origin} to {destination} {fragment}"
        try:
            html = fetch(query)
        except Exception:
            continue
        if not html:
            continue
        hint = parse_hint(html, month=label, anchor_year=year)
        if hint is None:
            continue
        if min_nights is not None and hint.nights < min_nights:
            continue
        if max_nights is not None and hint.nights > max_nights:
            continue
        if hint.key in seen_windows:
            continue          # halves often repeat the month's own answer
        seen_windows.add(hint.key)
        hints.append(hint)
    return hints


def hint_window_keys(hints: Iterable[MonthHint]) -> list[str]:
    """Hot-list keys for the windows the hints named, cheapest first."""
    return [h.key for h in sorted(hints, key=lambda h: h.price_usd)]


# --------------------------------------------------------------------------
# The ledger: remember what the wide net said, month by month.
#
# Added 2026-08-23, after an audit asked a question the project could not
# answer: which months are cheap? The sweep had priced 1,604 windows but
# `discoveries.json` remembered only January, February and March - because
# `sweep_order` walks the priority months first and the sweep was 37% into
# its first pass. Five of the eight months had never been priced at all.
#
# That also makes the note in CLAUDE.md - "every cheap window sat in January
# or February" - circular: those were the only months looked at.
#
# The wide net already asks about *every* month, six times a day, for eight
# requests a run. It logged the answers and threw them away. Keeping them
# costs nothing and gives an 8-month price picture immediately rather than
# after the sweep's first full pass.
#
# A month that returns no hint is recorded too. "No hint" is normal - three
# months in eight, measured - and a month that has *never* answered is worth
# telling apart from one that answered expensively.

LEDGER_VERSION = 1
DEFAULT_LEDGER = "month_hints.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_ledger(path: str | Path = DEFAULT_LEDGER) -> dict:
    """Never raises. A missing or damaged ledger is simply an empty one."""
    p = Path(path)
    if not p.exists():
        return {"version": LEDGER_VERSION, "months": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"version": LEDGER_VERSION, "months": {}}
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return {"version": LEDGER_VERSION, "months": {}}
    if not isinstance(data.get("months"), dict):
        data["months"] = {}
    return data


def save_ledger(ledger: dict, path: str | Path = DEFAULT_LEDGER) -> None:
    """Atomic write - a scheduled run may read this while another writes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def record_hints(path: str | Path, hints: Iterable[MonthHint], *,
                 asked: Iterable[str] = (), now: str | None = None) -> dict:
    """Fold this run's hints into the ledger and return it.

    `asked` is every month label the run queried, so months that answered
    nothing are counted rather than being invisible. The best-ever price is
    kept alongside the latest, because the latest alone cannot answer "has
    this month ever been cheap?" - which is the question the ledger exists
    for.
    """
    stamp = now or _now_iso()
    ledger = load_ledger(path)
    months = ledger["months"]

    for label in asked:
        months.setdefault(label, {
            "best_usd": None, "best_depart": "", "best_ret": "", "best_seen": "",
            "last_usd": None, "last_depart": "", "last_ret": "", "last_seen": "",
            "hits": 0, "asks": 0,
        })
        months[label]["asks"] = int(months[label].get("asks", 0)) + 1

    for h in hints:
        row = months.setdefault(h.month, {
            "best_usd": None, "best_depart": "", "best_ret": "", "best_seen": "",
            "last_usd": None, "last_depart": "", "last_ret": "", "last_seen": "",
            "hits": 0, "asks": 1,
        })
        row["hits"] = int(row.get("hits", 0)) + 1
        row["last_usd"] = h.price_usd
        row["last_depart"] = h.depart.isoformat()
        row["last_ret"] = h.ret.isoformat()
        row["last_seen"] = stamp
        best = row.get("best_usd")
        if best is None or h.price_usd < int(best):
            row["best_usd"] = h.price_usd
            row["best_depart"] = h.depart.isoformat()
            row["best_ret"] = h.ret.isoformat()
            row["best_seen"] = stamp

    save_ledger(ledger, path)
    return ledger


def format_ledger(ledger: dict, *, threshold: int | None = None) -> list[str]:
    """Human-readable lines, cheapest month first, for --status."""
    months = ledger.get("months", {})
    if not months:
        return ["No month hints recorded yet."]

    def sort_key(item):
        best = item[1].get("best_usd")
        return (best is None, best if best is not None else 0)

    lines = []
    for label, row in sorted(months.items(), key=sort_key):
        asks, hits = int(row.get("asks", 0)), int(row.get("hits", 0))
        best = row.get("best_usd")
        if best is None:
            lines.append(f"  {label:<16} no hint yet ({hits}/{asks} answered)")
            continue
        flag = "  <-- under threshold" if threshold and int(best) <= threshold else ""
        lines.append(
            f"  {label:<16} best ${int(best):,} "
            f"{row.get('best_depart','')} -> {row.get('best_ret','')} "
            f"({hits}/{asks} answered){flag}")
    return lines
