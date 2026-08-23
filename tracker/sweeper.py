"""A slow, endless walk over every window, pricing each one properly.

The scheduled runs cannot cover the search space. Chrome prices ~120
windows a day against ~4,000 possible (depart, return) pairs, so a fare on
a window neither the wide net nor the hot list points at can sit unseen for
weeks. Raising the per-run budget does not fix it either: a run has to
finish in minutes because an email is waiting on it.

So the sweep is a separate, long-lived process with no deadline. It walks
the whole space in a fixed order, prices each window through Chrome, keeps
whatever survives the visa rule, and writes it to a store the scheduled run
reads. At roughly 13 seconds a launch plus a delay, one full pass takes
somewhere between half a day and a day and a half depending on the delay -
which turns "weeks, maybe never" into "since yesterday at the latest".

Three things make it safe to leave running:

* **It is resumable.** The cursor is persisted after every batch, so a
  reboot, a crash or a Ctrl-C costs at most one window. It does not start
  over.
* **It is polite.** Every launch waits `delay_s`, and consecutive failures
  back it off further. Blocking the IP would take the scheduled runs down
  with it, so the sweep is deliberately the slowest thing in the project.
* **It never blocks the email.** The store is written atomically and read
  opportunistically. A missing, stale or corrupt store degrades the run to
  exactly what it does today - it never fails it.

What it stores is the *cheapest visa-free option per window*, not every
option. The trip owner cares about one number per date pair, and keeping
only that bounds the file no matter how long the sweep runs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import history as history_mod
from .browser import BrowserOption, chrome_path, fetch_dom, parse_options
from .verify import booking_link

log = logging.getLogger(__name__)

DEFAULT_STORE = "discoveries.json"
STORE_VERSION = 1

# Keep the store bounded. A full pass sees thousands of windows and most are
# expensive; there is no reason to remember a $3,400 fare from last Tuesday.
MAX_ENTRIES = 400

# A price is a snapshot, not a quote. Anything older than this is reported
# with its age attached, and past the hard limit it is dropped entirely.
STALE_AFTER_HOURS = 36
DROP_AFTER_HOURS = 24 * 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_hours(stamp: str, now: datetime | None = None) -> float:
    try:
        then = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - then).total_seconds() / 3600.0


@dataclass
class Discovery:
    """The cheapest visa-free fare seen for one window."""
    depart: str
    ret: str
    price_usd: int
    origin: str = "SJO"
    destination: str = "TYO"
    stops: list = field(default_factory=list)
    airlines: list = field(default_factory=list)
    total_minutes: int = 0
    deep_link: str = ""
    seen_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.depart}_{self.ret}"

    @property
    def nights(self) -> int:
        try:
            return (Date.fromisoformat(self.ret) - Date.fromisoformat(self.depart)).days
        except ValueError:
            return 0

    @property
    def route_label(self) -> str:
        return " - ".join((self.origin, *self.stops, self.destination))

    def age_hours(self, now: datetime | None = None) -> float:
        return _age_hours(self.seen_at, now)

    def describe(self, now: datetime | None = None) -> str:
        age = self.age_hours(now)
        stamp = "just now" if age < 1 else f"{age:.0f}h ago"
        return (f"${self.price_usd:,} {self.route_label} {self.depart} "
                f"+{self.nights}n ({stamp})")

    def to_option(self) -> BrowserOption:
        """Back into the shape the email renderer already knows.

        The email should not care whether a fare came from this run's
        verification or from the sweep that has been grinding away since
        yesterday - only that it is real and visa-checked.
        """
        return BrowserOption(
            price_usd=self.price_usd, origin=self.origin,
            destination=self.destination,
            depart_date=Date.fromisoformat(self.depart),
            return_date=Date.fromisoformat(self.ret),
            stops=tuple(self.stops), airlines=tuple(self.airlines),
            total_minutes=self.total_minutes, deep_link=self.deep_link)

    @classmethod
    def from_option(cls, o: BrowserOption) -> "Discovery":
        return cls(
            depart=o.depart_date.isoformat(), ret=o.return_date.isoformat(),
            price_usd=o.price_usd, origin=o.origin, destination=o.destination,
            stops=list(o.stops), airlines=list(o.airlines),
            total_minutes=o.total_minutes, deep_link=o.deep_link,
            seen_at=_now())


@dataclass
class SweepStore:
    """Findings plus the cursor, persisted so a restart resumes."""
    version: int = STORE_VERSION
    cursor: int = 0
    passes_completed: int = 0
    windows_priced: int = 0
    pass_started: str = ""
    last_active: str = ""
    last_key: str = ""          # window key the cursor last finished
    found: dict = field(default_factory=dict)

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path = DEFAULT_STORE) -> "SweepStore":
        """Never raises. A missing or damaged store is simply a fresh one."""
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("%s unreadable; starting a fresh sweep store", p)
            return cls()
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path = DEFAULT_STORE) -> None:
        """Atomic write - the scheduled run may read this at any moment."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, p)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- contents ----------------------------------------------------------
    def record(self, option: BrowserOption) -> bool:
        """Keep this option if it is the best yet for its window.

        Returns True when the store changed, so the caller can decide
        whether the find is worth logging.
        """
        d = Discovery.from_option(option)
        prev = self.found.get(d.key)
        if prev is not None and int(prev.get("price_usd", 10 ** 9)) <= d.price_usd:
            # Not cheaper, but refresh the timestamp so a still-valid fare
            # does not age out of the email while the sweep keeps seeing it.
            prev["seen_at"] = d.seen_at
            return False
        self.found[d.key] = asdict(d)
        return True

    def prune(self, *, max_entries: int = MAX_ENTRIES,
              drop_after_hours: float = DROP_AFTER_HOURS,
              now: datetime | None = None) -> int:
        """Drop stale and surplus entries. Returns how many went."""
        before = len(self.found)
        alive = {k: v for k, v in self.found.items()
                 if _age_hours(v.get("seen_at", ""), now) <= drop_after_hours}
        if len(alive) > max_entries:
            ranked = sorted(alive.items(), key=lambda kv: kv[1].get("price_usd", 10 ** 9))
            alive = dict(ranked[:max_entries])
        self.found = alive
        return before - len(self.found)

    def best(self, *, limit: int = 10, threshold: int | None = None,
             max_age_hours: float = STALE_AFTER_HOURS,
             now: datetime | None = None) -> list[Discovery]:
        """Cheapest fresh findings, cheapest first."""
        out: list[Discovery] = []
        for raw in self.found.values():
            try:
                d = Discovery(**raw)
            except TypeError:
                continue
            if d.age_hours(now) > max_age_hours:
                continue
            if threshold is not None and d.price_usd > threshold:
                continue
            out.append(d)
        out.sort(key=lambda d: d.price_usd)
        return out[:limit]

    def progress(self, total: int) -> str:
        pct = (100.0 * self.cursor / total) if total else 0.0
        return (f"window {self.cursor}/{total} ({pct:.1f}% of this pass), "
                f"{self.passes_completed} pass(es) done, "
                f"{len(self.found)} window(s) remembered")


def sweep_order(windows: Sequence) -> list:
    """Windows in the order the sweep should walk them.

    Priority months first. In plain date order the sweep starts eight
    months before the dates the trip owner actually cares about, so at ~19
    seconds a window it would be most of a day before it reached January.
    Leading with the priority months makes the first useful finding arrive
    in the first hour instead.

    Within each group the order stays by date, because a stable order is
    what makes the persisted cursor mean anything across restarts.
    """
    priority = [w for w in windows if getattr(w, "priority", False)]
    rest = [w for w in windows if not getattr(w, "priority", False)]
    return priority + rest


def resume_index(windows: Sequence, store: "SweepStore") -> int:
    """Where to carry on, by window rather than by position.

    The window list is rebuilt every run against today's date, so it is not
    a fixed array. Each day the earliest departure falls out of the rolling
    8-month span and a new one appears at the far end - 18 windows off the
    front, 18 onto the back, measured. Everything after the removed ones
    shifts down by 18 positions, so a numeric cursor silently jumps 18
    windows forward and those are never priced on that pass.

    Resuming by the last finished window's key instead makes the position
    mean the same thing across days. If that window has itself expired,
    fall back to the stored index, which is no worse than before.
    """
    if store.last_key:
        for n, w in enumerate(windows):
            if w.key == store.last_key:
                return (n + 1) % max(len(windows), 1)
    return min(store.cursor, max(len(windows) - 1, 0))


def sweep_batch(
    windows: Sequence,
    store: SweepStore,
    *,
    origin: str = "SJO",
    destination: str = "TYO",
    max_stops: int | None = 2,
    batch: int = 10,
    chrome: str | None = None,
    chrome_override: str = "",
    timeout_s: int = 120,
    budget_ms: int = 30000,
    fetch: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    delay_s: float = 8.0,
    on_find: Callable[[Discovery], None] | None = None,
    history_csv: str | None = None,
) -> int:
    """Price the next `batch` windows, advancing and wrapping the cursor.

    Returns how many windows were priced. `fetch` is injectable so tests
    never launch a browser.
    """
    if not windows:
        return 0
    exe = chrome or chrome_path(chrome_override)
    if exe is None and fetch is None:
        log.error("Chrome not found; the sweep cannot run")
        return 0

    grab = fetch or (lambda url: fetch_dom(url, chrome=exe, timeout=timeout_s,
                                           virtual_time_budget_ms=budget_ms))
    if not store.pass_started:
        store.pass_started = _now()

    # Re-anchor on the window we actually finished, not on a raw index into
    # a list that shifts under us every day.
    store.cursor = resume_index(windows, store)

    done = 0
    for _ in range(batch):
        if store.cursor >= len(windows):
            store.cursor = 0
            store.passes_completed += 1
            store.pass_started = _now()
            log.info("Sweep completed pass %d", store.passes_completed)

        w = windows[store.cursor]
        depart, ret = w.depart, w.back
        url = _search_url(origin, destination, depart, ret, max_stops)
        try:
            dom = grab(url)
        except Exception as exc:            # noqa: BLE001 - never die mid-sweep
            log.debug("sweep fetch failed for %s: %s", depart, exc)
            dom = ""

        options = [o for o in parse_options(
            dom, origin=origin, destination=destination,
            depart_date=depart, return_date=ret) if o.visa_ok]
        if options:
            cheapest = min(options, key=lambda o: o.price_usd)
            cheapest = _with_link(cheapest, max_stops)
            if store.record(cheapest) and on_find is not None:
                on_find(Discovery.from_option(cheapest))

            # Log every visa-free option, not just the cheapest. The price
            # baseline is a *distribution* - what a traveller typically pays
            # - so it needs the dear ones too. This is also what earns the
            # baseline its 5-distinct-day requirement in a reasonable time:
            # the scheduled runs alone contribute a few dozen rows a day.
            if history_csv:
                try:
                    history_mod.append(history_csv, history_mod.rows_from_verified(
                        options, band_of=lambda _p: "TYPICAL"))
                except OSError as exc:
                    log.debug("could not log sweep rows: %s", exc)

        store.last_key = w.key
        store.cursor += 1
        store.windows_priced += 1
        store.last_active = _now()
        done += 1
        if delay_s:
            sleep(delay_s)
    return done


def _with_link(option: BrowserOption, max_stops: int | None) -> BrowserOption:
    from dataclasses import replace
    return replace(option, deep_link=booking_link(option, max_stops=max_stops))


def _search_url(origin, destination, depart, ret, max_stops):
    from fast_flights import FlightQuery, Passengers, create_query
    legs = [
        FlightQuery(date=depart.isoformat(), from_airport=origin,
                    to_airport=destination, max_stops=max_stops),
        FlightQuery(date=ret.isoformat(), from_airport=destination,
                    to_airport=origin, max_stops=max_stops),
    ]
    return create_query(flights=legs, trip="round-trip", seat="economy",
                        passengers=Passengers(adults=1), currency="USD",
                        language="en").url()
