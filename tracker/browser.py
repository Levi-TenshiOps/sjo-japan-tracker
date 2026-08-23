"""Chrome fallback for the fares plain HTTP cannot see.

Measured 2026-08-22, SJO-NRT departing 2027-02-05, same URL both ways:

    nights   HTTP fetch              Chrome --dump-dom
       28    7 prices, min $1,860    8 prices, min $1,854
       31    EMPTY                   6 prices, min $1,854
       32    EMPTY                   6 prices, min $1,863
       35    EMPTY                   6 prices, min $1,908
       38    EMPTY                   6 prices, min $1,948

Round trips longer than about 30 nights are simply absent from the
server-rendered HTML. Currency, locale, `gl`, `hl`, multi-city framing and
the date-grid `tfu` were all tried and all returned the same empty ~1.8 MB
shell, while the 28-night control returned 2.4 MB with 90 prices. One-way
searches render fine at any length, so it is specific to long round trips.
Only running Google's JavaScript reaches them.

This uses Chrome's own `--headless --dump-dom`, so there is no new Python
dependency: `selectolax` already ships with fast-flights, and Chrome is
expected to be on the machine. That is deliberate — the alternative was
Playwright, which means a bundled browser download and a heavier
automation surface for the same answer.

Two limits worth knowing before relying on this:

* A launch costs roughly 25 seconds against ~3 for HTTP, so Chrome is for
  the stay lengths HTTP cannot see, never as a general replacement.
* `--dump-dom` returns the *collapsed* result list. Per-leg clock times
  live in a detail panel that only renders when a row is clicked, which
  `--dump-dom` cannot do. So an option here carries price, airline, total
  duration and the connection airports, but not per-leg timings — enough
  to price it and to rule on the visa, not enough to build a full
  `Itinerary`. Nothing is invented to fill the gap.

The connection airports are the part that matters most: they are what
`airports.is_banned` needs, and the US/Canada transit rule is decided here
exactly as it is for the HTTP path.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

from .airports import ban_reason

log = logging.getLogger(__name__)

# Where Chrome usually lives, per platform. `chrome_path` also honours an
# explicit override from config, which is what a non-standard install needs.
_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
_ON_PATH = ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge")

# "From 1863 US dollars round trip total. 1 stop flight with Air Canada."
_MASTER = re.compile(
    r"From\s+([0-9][0-9,]*)\s+US dollars[^.]*\.\s*"
    r"(Nonstop|\d+\s+stops?)\s+flight\s+with\s+([^.]+?)\.",
    re.I)
_DURATION = re.compile(r"Total duration\s+(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.I)
# The compact summary line carries IATA codes: "1 stop in YYZ", "2 stops in MEX, MTY"
_STOPS_IN = re.compile(r"\bstops?\s+in\s+([A-Z]{3}(?:\s*,\s*[A-Z]{3})*)")
_NONSTOP = re.compile(r"\bNonstop\b", re.I)

# Sentinel for "this row has stops but the airports could not be read".
# It must never satisfy the visa check: an unknown routing is exactly the
# case where failing open would put a US transit into the email.
UNKNOWN_STOP = "???"


@dataclass(frozen=True)
class BrowserOption:
    """One long-stay result read out of the rendered DOM.

    Deliberately *not* an `Itinerary`: it has no per-leg timings, and
    pretending otherwise would mean fabricating clock times for the email.
    """
    price_usd: int
    origin: str
    destination: str
    depart_date: Date
    return_date: Date
    stops: tuple[str, ...]          # connection airports, IATA
    airlines: tuple[str, ...]
    total_minutes: int
    deep_link: str = ""

    @property
    def nights(self) -> int:
        return (self.return_date - self.depart_date).days

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def banned_reason(self) -> str | None:
        """Why this itinerary is unflyable, or None if it is fine.

        Fails closed. `ban_reason` only knows about codes it has been told
        about, so an unreadable routing would otherwise come back clean and
        a US transit could reach the email through this path.
        """
        for code in self.stops:
            if code == UNKNOWN_STOP or not re.fullmatch(r"[A-Z]{3}", code):
                return "routing could not be read, so the visa rule cannot be checked"
            reason = ban_reason(code)
            if reason:
                return f"routes through {code} ({reason})"
        return None

    @property
    def visa_ok(self) -> bool:
        return self.banned_reason is None

    @property
    def route_label(self) -> str:
        return " - ".join((self.origin, *self.stops, self.destination))

    def describe(self) -> str:
        hrs, mins = divmod(self.total_minutes, 60)
        return (f"${self.price_usd:,} {self.route_label} "
                f"{self.depart_date} +{self.nights}n "
                f"{hrs} hr {mins} min "
                f"[{', '.join(self.airlines) or 'unknown'}]")


def chrome_path(override: str = "") -> str | None:
    """Absolute path to a usable Chrome/Chromium, or None."""
    if override:
        return override if Path(override).exists() else None
    for cand in _CANDIDATES:
        if Path(cand).exists():
            return cand
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch_dom(
    url: str,
    *,
    chrome: str,
    timeout: int = 120,
    virtual_time_budget_ms: int = 25000,
) -> str:
    """Rendered DOM for `url`, or "" if Chrome could not produce one.

    `--virtual-time-budget` is what makes this work: without it the dump
    happens before Google's JavaScript has populated the results and the
    page is as empty as the plain HTTP fetch.
    """
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio",
        f"--virtual-time-budget={virtual_time_budget_ms}",
        "--dump-dom", url,
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("chrome fetch failed: %s", exc)
        return ""
    return done.stdout or ""


def _minutes(text: str) -> int:
    m = _DURATION.search(text)
    if not m:
        return 0
    hrs = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return hrs * 60 + mins


def parse_options(
    html: str,
    *,
    origin: str,
    destination: str,
    depart_date: Date,
    return_date: Date,
    deep_link: str = "",
) -> list[BrowserOption]:
    """Every priced result in a rendered DOM, cheapest first.

    Reads `aria-label` rather than Google's obfuscated class names wherever
    it can: the labels are semantic and survive a restyle, the class names
    do not. IATA codes are the exception - only the visible summary line
    carries them, since the layover label names the airport in prose
    ("... at Toronto Pearson International Airport in Toronto").
    """
    if not html:
        return []
    tree = LexborHTMLParser(html)
    rows = tree.css("li.pIav2d") or tree.css("ul.Rk10dc li") or tree.css("li")

    out: list[BrowserOption] = []
    seen: set[tuple] = set()
    for row in rows:
        labels = " ".join(
            n.attributes.get("aria-label", "") or "" for n in row.css("[aria-label]"))
        master = _MASTER.search(labels)
        if not master:
            continue
        price = int(master.group(1).replace(",", ""))
        airline = master.group(3).strip()

        text = row.text(separator=" ", strip=True)
        codes = _STOPS_IN.search(text)
        if codes:
            stops = tuple(c.strip() for c in codes.group(1).split(","))
        elif _NONSTOP.search(master.group(2)) or _NONSTOP.search(text):
            stops = ()
        else:
            # Stops exist but no codes were found. Dropping it would be the
            # dangerous choice: an unknown routing must never be treated as
            # visa-clean, so mark it unusable rather than guessing.
            stops = (UNKNOWN_STOP,)

        opt = BrowserOption(
            price_usd=price, origin=origin.upper(), destination=destination.upper(),
            depart_date=depart_date, return_date=return_date, stops=stops,
            airlines=tuple(a.strip() for a in re.split(r",| and ", airline) if a.strip()),
            total_minutes=_minutes(labels), deep_link=deep_link,
        )
        fingerprint = (opt.price_usd, opt.stops, opt.airlines, opt.total_minutes)
        if fingerprint in seen:
            continue                      # the DOM repeats each row's summary
        seen.add(fingerprint)
        out.append(opt)

    out.sort(key=lambda o: o.price_usd)
    return out


def visa_free(options: list[BrowserOption]) -> list[BrowserOption]:
    """Only the options a Costa Rican passport can actually fly."""
    return [o for o in options if o.visa_ok]
