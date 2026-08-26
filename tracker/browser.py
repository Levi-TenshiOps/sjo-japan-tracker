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
    # When this price was actually observed, ISO-8601, or blank for one
    # checked during the current run.
    #
    # The email merges fares verified minutes ago with findings the
    # background sweep made up to `sweep_max_age_hours` (10) ago, sorts them
    # together and puts a "book" link on every row. Without this they are
    # indistinguishable, so the reader cannot tell a live price from one
    # observed before breakfast - which is the "lie by omission" the age cap
    # exists to prevent, just at a ten-hour granularity instead of a day.
    checked_at: str = ""

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


DEFAULT_PROFILE = ".chrome-profile"


def fetch_dom(
    url: str,
    *,
    chrome: str,
    timeout: int = 120,
    virtual_time_budget_ms: int = 25000,
    profile_dir: str | None = DEFAULT_PROFILE,
) -> str:
    """Rendered DOM for `url`, or "" if Chrome could not produce one.

    `--virtual-time-budget` is what makes this work: without it the dump
    happens before Google's JavaScript has populated the results and the
    page is as empty as the plain HTTP fetch.

    `--user-data-dir` matters just as much, for a different reason. Without
    it Chrome starts from a blank profile every single time: no cookies, no
    history, no session. A sweep of 4,000 windows then looks to Google like
    four thousand brand-new browsers from one address, each running exactly
    one flight search and never returning. That is a far louder bot signal
    than the request rate, and no amount of slowing down fixes it - a
    perfectly paced request from a browser that has never existed before is
    still obviously not a person.

    Reusing one profile keeps the cookies Google sets, so the requests read
    as one browser coming back rather than thousands appearing once. Pass
    `profile_dir=None` to opt out, which is only sensible in a test.

    One profile cannot serve two Chromes at once - the second would find the
    directory locked - which is fine, because `gate.google()` already
    guarantees only one process queries Google at a time.
    """
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio",
        f"--virtual-time-budget={virtual_time_budget_ms}",
    ]
    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        cmd.append(f"--user-data-dir={Path(profile_dir).resolve()}")
    cmd += ["--dump-dom", url]
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


# Google states its own result count in the page prose ("16 results
# returned"). Comparing it against what we parsed is the only way to notice
# that a window was under-collected: measured 2026-08-23 a live page claimed
# 16 while the parser found 13, and nothing anywhere said so.
# Google's own wording for a routing it cannot price. Distinct from a row
# we failed to parse, and counted apart from one.
_NO_PRICE = re.compile(r"price is unavailable", re.I)


_PRICE_ONLY = re.compile(r"(\d[\d,]*) US dollars")
_CLAIMED = re.compile(r"(\d{1,4})\s+results?\s+(?:returned|found)", re.I)


def claimed_result_count(html: str) -> int | None:
    """How many results Google says it has, or None if it does not say.

    The page also carries a "View more flights" control that `--dump-dom`
    cannot click, so a shortfall is expected rather than alarming. What
    matters is that it stops being invisible: a silent shortfall is
    indistinguishable from a window that genuinely had fewer fares, and the
    trip owner asked for 100% of applicable flights, not 99%.
    """
    if not html:
        return None
    m = _CLAIMED.search(html)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 0 < n < 1000 else None


def dom_price_order(html: str) -> list[int]:
    """Prices in Google's own row order, before we sort them.

    `parse_options` sorts by price, which destroys the one piece of evidence
    that says whether truncation can hide a bargain. If Google's list is
    price-ascending then the rows behind the un-clickable "View more
    flights" control are the dearest ones, and a shortfall provably cannot
    cost us a cheap fare. If it is "Best" order - a blend of price and
    duration - then a cheap slow fare could sit below the fold, and the
    shortfall matters.

    Reading it costs nothing: the DOM is already in hand. Answering the
    question by re-querying with price caps would cost requests against an
    IP that has only just recovered.
    """
    if not html:
        return []
    # The same selector chain `parse_options` uses. A single selector would
    # return [] whenever Google restyles, while the parser carried on via
    # its fallbacks - and an empty list looks exactly like a page whose rows
    # were in ascending order. Reassuring evidence manufactured out of a
    # parsing failure is the one thing a detector must never produce.
    tree = LexborHTMLParser(html)
    rows = tree.css("li.pIav2d") or tree.css("ul.Rk10dc li") or tree.css("li")
    out: list[int] = []
    for row in rows:
        labels = " ".join(n.attributes.get("aria-label", "") or ""
                          for n in row.css("[aria-label]"))
        m = _PRICE_ONLY.search(labels)
        if m:
            out.append(int(m.group(1).replace(",", "")))
    return out


def dom_row_count(html: str) -> int:
    """How many result rows are physically in the DOM.

    The decisive number for the truncation gap. Google says "16 results
    returned" and the parser produces 13, and there are two very different
    explanations:

    * the three rows are not in the page at all, behind the "View more
      flights" control that `--dump-dom` cannot click - nothing to be done
      short of a different query; or
    * they *are* in the page and `parse_options` is dropping them - a
      parser bug, fixable for free, and costing us fares.

    Counting the rows separates the two, and costs nothing: the DOM is
    already in hand.
    """
    if not html:
        return 0
    tree = LexborHTMLParser(html)
    rows = tree.css("li.pIav2d") or tree.css("ul.Rk10dc li") or tree.css("li")
    return len(rows)


def unreadable_count(options) -> int:
    """Options dropped because their routing could not be read.

    These are not visa rejections - they are fares we may well be able to
    book, discarded because `banned_reason` fails closed when it cannot
    check the rule. That is the right call for safety and the wrong thing to
    do silently, so it is counted separately.
    """
    return sum(1 for o in options
               if (o.banned_reason or "").startswith("routing could not"))


def parse_options(
    html: str,
    *,
    origin: str,
    destination: str,
    depart_date: Date,
    return_date: Date,
    deep_link: str = "",
    stats: dict | None = None,
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
            if stats is not None:
                # Google says outright when it has no price for a routing:
                # "Total price is unavailable. 2 stops flight with American
                # and JAL ... Dallas Fort Worth ... Chicago O'Hare". That is
                # not a row we failed to read, it is a row with nothing to
                # read - and every one seen so far transits the US, so the
                # visa rule would drop it regardless.
                #
                # Counting it as a parse failure matters, because
                # `rows_missed_by_parser` is what raises "results are
                # arriving in a format we cannot read". Measured 2026-08-25:
                # 39 windows had exactly two such rows each - one logical
                # row, twice, the DOM carrying everything double - and the
                # counter had climbed to 20 against an alarm at 25. The
                # first email that alarm ever sent would have been false.
                key = "unpriced" if _NO_PRICE.search(labels) else "unmatched"
                stats[key] = stats.get(key, 0) + 1
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
            # The DOM repeats each row's summary, so this is mostly real
            # de-duplication. It also collapses two genuinely different
            # departure times that share a price, routing, airline and
            # duration - which costs nothing for finding the cheapest fare,
            # but does inflate the apparent gap between what Google says it
            # returned and what we parsed. Counted so the two can be told
            # apart.
            if stats is not None:
                stats["duplicate"] = stats.get("duplicate", 0) + 1
            continue
        seen.add(fingerprint)
        out.append(opt)

    out.sort(key=lambda o: o.price_usd)
    return out


def visa_free(options: list[BrowserOption]) -> list[BrowserOption]:
    """Only the options a Costa Rican passport can actually fly."""
    return [o for o in options if o.visa_ok]
