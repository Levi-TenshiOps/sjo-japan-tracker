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
import random
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import gate
from . import history as history_mod
from . import airports as airports_mod
from .browser import (
    BrowserOption, chrome_path, claimed_result_count, dom_price_order,
    dom_row_count, fetch_dom, parse_options, unreadable_count,
)
from .verify import booking_link, within_duration

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
            total_minutes=self.total_minutes, deep_link=self.deep_link,
            checked_at=self.seen_at)

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
    # 1 = a *fast* empty, the shape a throttle makes. Not the same as
    # "no fares here", which is a fact about the date.
    recent: list = field(default_factory=list)
    # Plain emptiness, for reporting only. Never drives the alarm.
    recent_blank: list = field(default_factory=list)
    # 1 when a launch came back with fares on the page, 0 when it did not.
    # Unlike `recent` and `recent_blank` this records *every* launch,
    # re-checks and focus picks included, because a re-check that succeeds
    # is perfectly good evidence that Google is answering. The exclusion
    # those two carry exists to stop re-check *empties* inflating the
    # throttle signal; a success inflates nothing.
    recent_worked: list = field(default_factory=list)
    # How many times a focus has priced each window. A focus must be able
    # to finish: without a bound, a window that keeps answering blank on a
    # connection that cannot be proven stays pending for ever, and the
    # focus rotates through the same tail indefinitely.
    focus_tries: dict = field(default_factory=dict)
    # Windows whose "no fares" answer arrived while the connection looked
    # throttled. They are not empty, they are *unverified*, and they stay
    # here until they can be re-checked during a healthy stretch.
    suspect: list = field(default_factory=list)
    throttle_events: int = 0
    # Windows where Google claimed more results than we could parse,
    # and options dropped because their routing was unreadable. Both
    # are ways a fare can be missed on a window we *did* check.
    shortfalls: int = 0
    unreadable: int = 0
    # Re-check keys discarded because the window had rolled out of
    # the search span. The only way a queued window leaves without
    # being priced, so it is counted rather than silent.
    dropped_rechecks: int = 0
    # Result rows physically present in the DOM that `parse_options`
    # did not turn into an option. Distinct from rows Google never
    # sent: this half would be a parser bug, and fixable for free.
    rows_missed_by_parser: int = 0
    # Rows Google itself says it cannot price ("Total price is
    # unavailable"). Counted apart from a parse failure: there is nothing
    # to read in them, and every one seen so far transits the US anyway.
    rows_unpriced: int = 0
    #: One-shot: has `rows_missed_by_parser` been cleared of its pre-`_NO_PRICE`
    #: meaning? An explicit flag rather than a fingerprint, because the store
    #: is rewritten every window - by the time anyone looks, the old value is
    #: already sitting in a new-format file. That is the same trap that stopped
    #: the `recent` migration firing on 2026-08-24.
    parser_counts_migrated: bool = False
    # Rows collapsed as duplicates. Harmless for finding the cheapest
    # fare - a second option at the same price, routing, airline and
    # duration is the same deal - but it inflates the apparent gap.
    rows_deduped: int = 0
    # {hub: {"n": times seen, "min": cheapest fare lost to it}} for
    # fares refused only because the hub has never been researched.
    rejected_unknown: dict = field(default_factory=dict)
    # Whether "focus complete" has already been said, so it is said
    # once rather than on every launch afterwards.
    focus_done_logged: bool = False
    # Of the shortfall windows, how many had Google's rows in
    # ascending price order. If they always are, the rows behind the
    # un-clickable control are the dearest and a shortfall is
    # harmless; if not, a cheap slow fare can hide below the fold.
    dom_sorted: int = 0
    dom_unsorted: int = 0
    # Whether the trip owner has already been told about the throttle
    # currently in progress. Persisted, so a restart mid-throttle does
    # not send a second alarm about the same event.
    alarm_sent_for: str = ""
    # Whether the trip owner has been told the scheduled runs stopped
    # delivering. Persisted so a restart does not re-send, and reset
    # when emails start flowing again.
    silence_alarm_sent: bool = False
    warm_index: int = 0        # rotation over the schedule-plausible windows
    # key -> {at, empty, healthy}. Every check, not just the finds.
    checked: dict = field(default_factory=dict)
    consecutive_rests: int = 0
    #: Every rest ever taken, and the only rest counter that is never reset.
    #: `consecutive_rests` is deliberately cleared on recovery and by
    #: `forget_stale_health`, which makes it useless for "has a new rest
    #: happened since I last looked": after one rest and one recovery it
    #: returns to 0, so the next rest reads as 1 again and compares equal to
    #: what was already seen. The rate tripwire watched it and would have
    #: fired exactly once, then stayed dead for the life of the process.
    rests_total: int = 0
    throttled_since: str = ""
    #: The delay the sweep is really pacing at, written every batch. The
    #: read-only views run in their own process with their own --delay
    #: default, so without this `--watch` reports an ETA for a rate nobody
    #: is using - and the tripwire can change the rate mid-run.
    delay_s: float = 0.0
    last_throttle: str = ""

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
        store = cls(**{k: v for k, v in data.items() if k in known})

        # `recent` changed meaning on 2026-08-24: it used to count every
        # empty window and now counts only *fast* empties, which is a
        # different quantity entirely. Values written under the old meaning
        # read as a throttle - the store came back at 81% while the fresh
        # samples beside it said 0% blank and pages were taking 12.5s.
        #
        # `recent_blank` arrived in the same change, so its absence beside a
        # populated `recent` is exactly the fingerprint of the old format.
        # Drop the stale judgement rather than let it alarm; the next twenty
        # windows rebuild it in half an hour.
        if store.recent and not store.recent_blank:
            log.info("Discarding %d connection-health sample(s) written "
                     "before the meaning changed; judging fresh.",
                     len(store.recent))
            store.recent.clear()
            store.recent_blank.clear()
            store.recent_worked.clear()
            store.throttled_since = ""
            store.consecutive_rests = 0

        # `rows_missed_by_parser` changed meaning the same way. Before
        # `_NO_PRICE` it counted every row `parse_options` did not turn into
        # an option, which lumped Google's own "Total price is unavailable"
        # rows in with markup this parser genuinely cannot read. Only the
        # second kind means "fares are silently going missing", and only the
        # second kind should raise that alarm.
        #
        # Measured on the live log, 2026-08-25: 40 pre-fix "unreadable" rows,
        # in 20 windows of exactly 2 - the fingerprint of the unpriced row,
        # which the DOM carries twice. Post-fix: 2 unpriced, **0** genuinely
        # unmatched. So the accumulated 40 was entirely the wrong quantity,
        # it sat above PARSER_ALARM_ROWS (25), and the next scheduled run
        # would have emailed "results are arriving in a format we cannot
        # read" - this project's fourth false alarm, from the very fix
        # written to prevent it.
        #
        # It resets to 0 rather than to an estimate because the old value
        # conflated two quantities and cannot be decomposed, while the
        # post-fix count of the quantity the alarm is about was measured
        # at zero. Zero is the honest number here, not a convenience.
        if not store.parser_counts_migrated:
            if store.rows_missed_by_parser:
                log.info("Discarding %d unreadable-row count(s) recorded "
                         "before Google's own 'no price available' rows were "
                         "counted apart; the parser alarm judges fresh.",
                         store.rows_missed_by_parser)
            store.rows_missed_by_parser = 0
            store.parser_counts_migrated = True
        return store

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
        """Record the latest price for this window.

        `found` answers "what can be booked on this date now", not "what
        was the best this date ever showed" - the all-time low lives in
        `sweep_history.csv`, which is append-only and never rewritten.

        That distinction used to be wrong in the one direction that
        matters. A dearer sighting kept the old, lower price *and*
        refreshed its timestamp, so the email went on advertising a fare
        the market no longer had and labelled it "checked 20 min ago".
        Measured against the live store on 2026-08-24: 14 of the 48
        windows priced more than once were showing a price a later check
        had not found. The gaps were $10-12 there, but nothing bounds
        them - a $1,347 window rising to $2,000 would have gone on
        claiming $1,347, freshly checked, and that is the headline number
        the trip owner acts on.

        Refreshing the timestamp on an *equal* price is still right, and
        is why this is not simply "keep the minimum": a fare the sweep
        keeps re-confirming must not age out of the email.

        Returns True only when the price actually improved, so `on_find`
        still fires on news rather than on every re-check.
        """
        d = Discovery.from_option(option)
        prev = self.found.get(d.key)
        self.found[d.key] = asdict(d)
        if prev is None:
            return True
        return d.price_usd < int(prev.get("price_usd", 10 ** 9))

    def forget_stale_health(self, *, max_idle_hours: float = 0.5,
                            now: datetime | None = None) -> bool:
        """Drop connection-health samples that describe a different day.

        `recent` persists with the store, and it is the *only* input to
        `looks_throttled`. So a sweep stopped while throttled comes back up,
        reads yesterday's empty samples, concludes it is throttled before
        making a single request, and drops straight into 4x backoff - one
        window every six minutes - on a connection that may be perfectly
        healthy. Worse, it cannot clear that verdict until it has priced 20
        fresh windows, which at six minutes each is two hours of crawling
        to disprove something it never checked.

        Findings are untouched. A price is still a price; it is only the
        judgement about the *connection* that goes stale, and it goes stale
        fast. Returns True if anything was forgotten.
        """
        if not (self.recent or self.throttled_since):
            return False
        if _age_hours(self.last_active, now) <= max_idle_hours:
            return False
        self.recent.clear()
        self.recent_blank.clear()
        self.recent_worked.clear()
        self.throttled_since = ""
        self.consecutive_rests = 0
        return True

    def prune(self, *, max_entries: int = MAX_ENTRIES,
              drop_after_hours: float = DROP_AFTER_HOURS,
              now: datetime | None = None) -> int:
        """Drop stale and surplus entries. Returns how many went.

        Dropping one must not lose the *fact that the window was checked*.
        `found` keeps only the cheapest MAX_ENTRIES findings, so a window
        priced at $2,132 falls out of it within a day - and it was then in
        none of the four coverage states, because those read `found` and
        the check ledger and nothing else. Measured against the live store
        on 2026-08-24: 24 of 62 orphans were windows that had been priced
        perfectly well and simply were not cheap.

        Two costs, and the second is the larger. The audit stops meaning
        what it says, and `unverified_windows` re-queues the window for
        ever - spending Chrome launches re-pricing dates already known to
        be dear, which is exactly the traffic the tiering exists to avoid.

        So a dropped entry leaves a ledger stamp instead: it was checked,
        it answered, and the fare itself is in `sweep_history.csv`
        permanently. `secs` is omitted rather than invented - it exists to
        re-calibrate the timing threshold from real measurements, and a
        fabricated one would quietly corrupt that.
        """
        was = dict(self.found)
        alive = {k: v for k, v in self.found.items()
                 if _age_hours(v.get("seen_at", ""), now) <= drop_after_hours}
        if len(alive) > max_entries:
            ranked = sorted(alive.items(), key=lambda kv: kv[1].get("price_usd", 10 ** 9))
            alive = dict(ranked[:max_entries])
        self.found = alive
        for key, rec in was.items():
            if key in self.found:
                continue
            seen = str(rec.get("seen_at") or "")
            prior = self.checked.get(key, {}).get("at", "")
            # Do not overwrite a *newer* check with an older finding.
            if not seen or seen >= str(prior):
                self.checked[key] = {"at": seen or _now(),
                                     "empty": False, "healthy": True}
        return len(was) - len(self.found)

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

    def health(self) -> str:
        """One line on whether the connection is being trusted right now."""
        recent = self.recent[-EMPTY_ALARM_WINDOW:]
        rate = (100 * sum(recent) / len(recent)) if recent else 0.0
        blank = self.recent_blank[-EMPTY_ALARM_WINDOW:]
        blank_rate = (100 * sum(blank) / len(blank)) if blank else 0.0
        # Two different numbers, and conflating them is what produced a false
        # throttle alarm. The first is the connection; the second is the
        # calendar.
        bits = [f"throttle signal {rate:.0f}% (fast empties)",
                f"{blank_rate:.0f}% of windows had no visa-free fare"]
        if self.suspect:
            bits.append(f"{len(self.suspect)} window(s) awaiting a re-check")
        if self.throttled_since:
            bits.append("THROTTLED NOW since "
                        f"{self.throttled_since[11:16]} UTC")
        elif self.throttle_events:
            bits.append(f"{self.throttle_events} throttle event(s) so far")
        # Completeness *within* a window, as distinct from coverage *of*
        # windows. Both are ways a fare can be missed on a date we did check,
        # and both were previously invisible.
        if self.shortfalls:
            # Not truncation. Measured 2026-08-24: the DOM holds exactly
            # twice what Google claims - every row's summary appears twice -
            # and the difference between "claims" and "parsed" is fares that
            # are identical in price, routing, airline and duration, i.e.
            # the same deal at another departure time.
            bits.append(f"{self.shortfalls} window(s) with duplicate "
                        f"listings ({self.rows_deduped} rows collapsed, "
                        f"{self.rows_missed_by_parser} unreadable)")
        if self.unreadable:
            bits.append(f"{self.unreadable} option(s) dropped as unreadable")
        if self.dom_sorted or self.dom_unsorted:
            bits.append(f"row order ascending on {self.dom_sorted} of "
                        f"{self.dom_sorted + self.dom_unsorted} shortfalls")
        return ", ".join(bits)

    def progress(self, total: int) -> str:
        pct = (100.0 * self.cursor / total) if total else 0.0
        return (f"window {self.cursor}/{total} ({pct:.1f}% of this pass), "
                f"{self.passes_completed} pass(es) done, "
                f"{len(self.found)} window(s) remembered")


# How the sweep divides its launches once it knows anything. Measured
# 2026-08-23 over 400 priced windows: 1% were at or under the $1,400 alert
# threshold, 4% at or under $1,600, and every one of those sat in January or
# February. Sweeping all 4,014 windows at equal priority spends ~96% of the
# budget on dates that will never produce an alert - and that budget is
# exactly what got the address throttled.
#
# So: most launches go to windows already known to be cheap, or never
# priced at all; the rest continue the cold rotation so coverage still
# completes. Both halves matter. Only chasing the cheap ones would never
# notice a new bargain appearing somewhere cold.
# One launch in four goes to a window already known to be cheap; the rest
# continue the cold rotation. Measured 2026-08-23 over 400 priced windows,
# 1% were at or under the $1,400 alert threshold and 4% at or under $1,600,
# every one of them in January or February. Treating all 4,014 windows
# equally spends ~96% of the budget on dates that cannot produce an alert,
# and that budget is what got the address throttled.
#
# A quarter, not more. There are only ~40 hot windows: at a higher share
# they would be re-priced several times an hour, which buys nothing and
# costs coverage everywhere else. Both halves are needed - chasing only the
# cheap ones would never notice a bargain appearing somewhere cold.
HOT_SHARE = 0.25
HOT_PRICE_MULTIPLE = 1.3       # "cheap" means within this of the best seen


def hot_keys(store: "SweepStore", *, threshold: int | None = None,
             multiple: float = HOT_PRICE_MULTIPLE) -> list[str]:
    """Windows worth re-pricing often, cheapest first.

    Anchored on the cheapest fare actually seen rather than a fixed number,
    so it keeps working when the market moves. `threshold` widens the net to
    anything under the alert price even when the best seen is far below it.
    """
    priced = [(v.get("price_usd", 10 ** 9), k) for k, v in store.found.items()]
    if not priced:
        return []
    best = min(p for p, _ in priced)
    ceiling = best * multiple
    if threshold is not None:
        ceiling = max(ceiling, float(threshold))
    return [k for p, k in sorted(priced) if p <= ceiling]


# What a Chrome launch actually costs, measured on this machine. Used to
# turn a delay into a launch rate, which is what `needed_hot_share` divides
# freshness by.
#
# Re-measured 2026-08-25 across 342 timed checks: median 12.1s, mean 13.8s.
# The constant said 6.1, from an early sample taken when a page rendered in
# about six seconds - whatever changed since (machine, network, Chrome,
# Google), it is now more than twice that.
#
# The **mean** is the right statistic here, not the median: this converts a
# delay into throughput, and total time is launches x mean.
#
# The error was not harmless and got worse when the rate went up, because a
# fixed launch cost is a larger share of a shorter cycle. Believing 6.1s at
# a 40s delay claims 78 launches an hour where there are 67, so the derived
# hot share under-delivers - 7.9 refreshes an hour against the 8.9 the
# 10-hour freshness target needs. Erring high is the safe direction: it
# overestimates the cycle, so it buys more freshness than asked for.
LAUNCH_SECONDS = 14.0
# A swept price older than this is dropped from the email (config's
# `sweep_max_age_hours`), so it is exactly how fresh a hot window must be.
HOT_FRESHNESS_HOURS = 10.0


def needed_hot_share(n_hot: int, *, cycle_s: float,
                     freshness_hours: float = HOT_FRESHNESS_HOURS,
                     cap: float = HOT_SHARE) -> float:
    """The smallest hot share that still keeps every hot window fresh.

    `HOT_SHARE` was a flat 0.25, chosen as an upper bound - the note in
    CLAUDE.md argues only that *more* than a quarter buys nothing. Measured
    2026-08-23, it is also far more than is needed: 41 hot windows at a
    10-hour freshness limit need 4.1 launches an hour, and a 90-second delay
    supplies 37.5. A quarter of those is 9.4 an hour, so more than half the
    hot budget was re-pricing windows that were not close to going stale.

    That excess is not free. Every hot launch is a cold window not covered,
    so it lengthened a full pass from 4.6 days to 5.5 at no benefit, and the
    gap widens as the delay drops - at 30 seconds the need is 4%, not 25%.

    Derived per launch rather than fixed, because the inputs move: the hot
    list grows as cheap windows are found, and the rate changes with
    `--delay`. Capped at `HOT_SHARE` so this can never spend *more* than the
    old behaviour, only less.
    """
    if n_hot <= 0 or cycle_s <= 0 or freshness_hours <= 0:
        return 0.0
    launches_per_hour = 3600.0 / cycle_s
    return min(cap, (n_hot / freshness_hours) / launches_per_hour)


# Measured 2026-08-23 over 1,165 observations. Every fare at or under
# $1,600 departed on a Friday or a Monday, and the Edelweiss/SWISS routing
# through Zurich - which carries every cheap fare found so far - appears on
# Monday, Wednesday and Friday departures only:
#
#     Mon  41 of 228 priced (18.0%)      Tue  0 of 228      Thu  0 of 143
#     Wed  46 of 201 priced (22.9%)      Sat  0 of  58      Sun  0 of  63
#     Fri  67 of 244 priced (27.5%)
#
# That is a flight schedule, not noise: 371 Tuesday and Thursday windows
# priced, not one Zurich routing among them. Only 220 of 2,745 windows - 8%
# - carry a (departure, return) weekday pair that has ever produced a fare
# at or under $1,600, and the sweep was spending 92% of its launches on
# dates that structurally cannot hold one.
#
# So there is a third tier between hot and cold. It is derived from the
# history rather than hardcoded, because a hardcoded weekday list would be
# the circular-reasoning trap this project has already fallen into once:
# the pairs come from what has been *observed*, and the cold tier keeps
# every other date in rotation so a new pattern is still discovered.
WARM_SHARE = 0.25          # keeps the ~220 plausible windows fresh daily
WARM_PAIR_MULTIPLE = 1.15  # a pair qualifies on a fare within this of target


# One clean day, defined so it can be checked rather than felt.
READY_QUIET_HOURS = 24.0


# Enough to see the shape without letting the store grow without bound.
MAX_REJECTED_HUBS = 60


def _note_unresearched(store: "SweepStore", parsed) -> None:
    """Record fares lost only because a hub has never been looked up.

    `sweep_history.csv` logs what we accept, so the cost of the visa
    filter has been invisible: a fare refused for a US transit and a fare
    refused because nobody has researched Orly left exactly the same
    trace, which is none. The first is refused for ever. The second is a
    gap in a hand-kept list - Costa Rica has visa-free Schengen access,
    and CDG is listed while ORY is not, FRA and MUC while BER and HAM are
    not.

    Nothing here changes what is allowed; `ban_reason` still fails closed
    and `BANNED_AIRPORTS` is untouched. This only makes the cost
    measurable, so adding a hub can be an evidence-based decision rather
    than a guess - and so that if none of these hubs ever carries a
    cheaper fare, that is known too.
    """
    for opt in parsed:
        if opt.visa_ok:
            continue
        codes = [c.upper() for c in opt.stops]
        # If anything on the itinerary is refused outright, researching the
        # rest would not recover this fare.
        if any(airports_mod.ban_reason(c) and not airports_mod.is_unresearched(c)
               for c in codes):
            continue
        unknown = [c for c in codes if airports_mod.is_unresearched(c)]
        if not unknown:
            continue
        for code in unknown:
            rec = store.rejected_unknown.get(code)
            if rec is None:
                if len(store.rejected_unknown) >= MAX_REJECTED_HUBS:
                    continue
                rec = {"n": 0, "min": opt.price_usd}
                store.rejected_unknown[code] = rec
            rec["n"] = int(rec.get("n", 0)) + 1
            rec["min"] = min(int(rec.get("min", opt.price_usd)), opt.price_usd)


_MONTH_LABEL = {1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November",
                12: "December"}


def watch_lines(windows: Sequence, store: "SweepStore", *,
                threshold: int | None, delay_s: float = 90.0,
                started: tuple | None = None,
                focus_months: Sequence[int] = (),
                now: datetime | None = None) -> list[str]:
    """A compact live view of the sweep, for `--watch`.

    Reads the store and nothing else, so it is safe to leave running
    beside the sweep: the two never write the same file, and this makes no
    request to Google.

    `started` is (timestamp, cursor) from when watching began, which is
    what turns a position into a rate. The configured delay gives a
    *theoretical* rate; the observed one is the honest number, because the
    sweep also waits on the Google lock and rests when throttled.
    """
    now = now or datetime.now(timezone.utc)
    total = len(windows) or 1
    done = min(store.cursor, total)
    pct = 100.0 * done / total
    filled = int(pct / 2.5)
    bar = "#" * filled + "." * (40 - filled)

    out = []
    # While a focus is on, the cold cursor is deliberately frozen - so
    # leading with it would show a bar that does not move for a day, which
    # reads as a stalled sweep. Show the work actually being done.
    pending = focus_pending(windows, store, focus_months) if focus_months else []
    if focus_months:
        names = ", ".join(_MONTH_LABEL.get(m, str(m)) for m in focus_months)
        if pending:
            in_focus = sum(1 for w in windows
                           if w.depart.month in set(focus_months))
            settled = in_focus - len(pending)
            fpct = 100.0 * settled / (in_focus or 1)
            ffill = int(fpct / 2.5)
            out.append(f"  FOCUS {names}")
            out.append(f"  [{'#' * ffill}{'.' * (40 - ffill)}] {fpct:5.1f}%")
            out.append(f"  {settled:,} of {in_focus:,} answered   "
                       f"{len(pending):,} still open")
        else:
            # "Complete" and "there was nothing there" look identical from
            # a count of zero, and this project has been caught by that
            # before - a named month the horizon cannot reach searches
            # nothing and reads as a month with no cheap fares. Say which.
            have = {w.depart.month for w in windows}
            absent = [m for m in focus_months if m not in have]
            if absent and not (set(focus_months) & have):
                out.append(f"  FOCUS {names}: NOT SEARCHED - no windows in "
                           f"those months at all (check included_months)")
            elif absent:
                out.append(f"  FOCUS {names}: complete (note: "
                           f"{', '.join(_MONTH_LABEL.get(m, str(m)) for m in absent)}"
                           f" has no windows at all)")
            else:
                out.append(f"  FOCUS {names}: complete")
        out.append("")

    out += [f"  {'full sweep (paused)' if pending else 'full sweep'}",
            f"  [{bar}] {pct:5.1f}%",
            f"  window {done:,} of {total:,}   "
            f"{len(store.found):,} fares remembered   "
            f"{len(store.suspect):,} awaiting a re-check"]

    rate = None
    if started:
        t0, c0 = started[0], started[1]
        hours = (now - t0).total_seconds() / 3600.0
        moved = done - c0
        if hours > 0 and moved > 0:
            rate = moved / hours
    if pending:
        # The cursor is frozen, so its rate is zero and would be a lie.
        # Rate the focus instead, from questions closed while watching.
        if started and len(started) > 2:
            t0, _, p0 = started[0], started[1], started[2]
            hours = (now - t0).total_seconds() / 3600.0
            closed = p0 - len(pending)
            if hours > 0 and closed > 0:
                per_hour = closed / hours
                eta = len(pending) / per_hour
                out.append(f"  {per_hour:.0f} answered/hour observed   "
                           f"~{eta:.1f} h left   focus ends about "
                           f"{(now + timedelta(hours=eta)).astimezone():%a %d %b %H:%M}")
            else:
                out.append("  (watching for a rate...)")
        else:
            out.append("  (watching for a rate...)")
    elif rate:
        left = max(total - done, 0)
        eta = left / rate
        finish = now + timedelta(hours=eta)
        out.append(f"  {rate:.0f} new window(s)/hour observed   "
                   f"{left:,} left   ~{eta / 24:.1f} day(s)")
        out.append(f"  first full pass ends about "
                   f"{finish.astimezone():%a %d %b %H:%M}")
    else:
        out.append(f"  {max(total - done, 0):,} window(s) left "
                   f"(watching for a rate...)")

    # Per departure month, because "have we done January yet" is the
    # question actually being asked. "walked" is not the same as "known":
    # a window that came back empty while the connection was in doubt is
    # queued for a second look rather than believed.
    queued = set(store.suspect)
    months: dict = {}
    for i, w in enumerate(windows):
        key = w.depart.strftime("%Y-%m")
        row = months.setdefault(key, {"total": 0, "walked": 0,
                                      "fare": 0, "again": 0})
        row["total"] += 1
        if i < store.cursor:
            row["walked"] += 1
            if w.key in store.found:
                row["fare"] += 1
            elif w.key in queued:
                row["again"] += 1
    out.append("")
    out.append(f"  {'month':9}{'walked':>14}{'with a fare':>13}{'re-check':>10}")
    for key in sorted(months, key=lambda k: (k[5:], k)):
        r = months[key]
        share = 100.0 * r["walked"] / (r["total"] or 1)
        walked = f"{r['walked']}/{r['total']} ({share:.0f}%)"
        out.append(f"  {key:9}{walked:>14}{r['fare']:>13}{r['again']:>10}")

    best = store.best(limit=1, threshold=None, now=now)
    if best:
        d = best[0]
        out.append("")
        out.append(f"  cheapest right now  ${d.price_usd:,}  "
                   f"{d.depart} -> {d.ret}")
    fast = store.recent[-EMPTY_ALARM_WINDOW:]
    blank = store.recent_blank[-EMPTY_ALARM_WINDOW:]
    out.append(f"  throttle signal {(100 * sum(fast) / len(fast)) if fast else 0:.0f}%"
               f"   no visa-free fare {(100 * sum(blank) / len(blank)) if blank else 0:.0f}%"
               f"   throttle events {store.throttle_events}")
    return out


#: The rate ladder, seconds between launches, slowest first.
#:
#: The floor was 15s on the reasoning that gains below it flatten. Measured
#: 2026-08-25 against the live window set, that was simply false - the
#: chance of catching a fare that lasts one day runs 40% / 55% / 74% / 89%
#: / 100% at 40 / 25 / 15 / 10 / 5 seconds. The curve steepens.
#:
#: So the floor is a *risk* judgement, not a value one, and 10s is where it
#: sits: ~3,600 requests a day, half the ~7,200 that throttled this address
#: on 2026-08-23, and that day also had a fresh browser profile every
#: launch and two processes competing - both since fixed.
#:
#: Below 10s the margin against a known-bad rate stops being comfortable
#: and the traffic shape gets harder to pass off as a person. Note also
#: that a page costs LAUNCH_SECONDS (~14s) on its own, so the delay is no
#: longer the dominant term: even --delay 0 only reaches ~6,200/day. The
#: last rungs buy less throughput than their numbers suggest.
RATE_LADDER = (90.0, 60.0, 40.0, 25.0, 15.0, 10.0)


def next_rate_step(current: float) -> float | None:
    """The next rung down from `current`, or None at the floor.

    Derived rather than written into the advice text, because a hardcoded
    "90 -> 45 -> 30" kept telling the reader to make changes that had
    already been made - on 2026-08-25 it advised dropping to 45s while the
    sweep was already running at 40, and raising hot_list_size to 18 when
    it was 18. Advice that has to be re-read against the config to be
    trusted is worse than no advice.
    """
    for rung in RATE_LADDER:
        if rung < current - 0.01:
            return rung
    return None


def slower_rate_step(current: float) -> float | None:
    """The next rung *up* (slower) from `current`, or None at the top.

    The other half of `next_rate_step`, and the one that makes raising the
    rate a bounded experiment rather than an open one. A rest means Google
    is already refusing; continuing at the rate that provoked it is the
    exact move that turned a short throttle into an hour of one on
    2026-08-23. The sweep steps itself down instead.

    One-way within a session, on purpose. It never speeds back up on its
    own - a quiet stretch right after a rest only means the health samples
    were cleared, which is precisely the false all-clear this project has
    already shipped once.
    """
    for rung in reversed(RATE_LADDER):
        if rung > current + 0.01:
            return rung
    return None


def readiness_report(store: "SweepStore", *, throttle_state, hours_since_email,
                     quiet_hours: float = READY_QUIET_HOURS,
                     delay_s: float | None = None,
                     hot_list_size: int | None = None,
                     now: datetime | None = None) -> tuple[bool, list[str]]:
    """Is it safe to raise the sweep rate or the Chrome budget yet?

    The rule this replaces was "do not raise it while the health line
    reports an empty rate above ~20%", and that number cannot be used any
    more: the empty rate measures the calendar, which is the whole finding
    of 2026-08-24. So readiness is judged on things that actually mean
    something about the connection.

    Returns (ready, lines). Every check is read from files already on disk -
    **nothing here queries Google.** Asking Google whether it is still
    refusing is the mistake that turned a short throttle into an hour of
    one on 2026-08-23.
    """
    lines: list[str] = []
    checks: list[bool] = []

    def check(ok: bool, label: str, detail: str) -> None:
        checks.append(ok)
        lines.append(f"  [{'OK ' if ok else 'no '}] {label}: {detail}")

    quiet = _age_hours(store.last_throttle, now) if store.last_throttle else None
    if quiet is None:
        check(True, "no throttle on record", "the counter has never fired")
    else:
        check(quiet >= quiet_hours,
              "quiet since the last throttle",
              f"{quiet:.1f}h of {quiet_hours:.0f}h needed "
              f"(last {store.last_throttle[:16].replace('T', ' ')} UTC)")

    check(store.consecutive_rests == 0, "not backing off",
          f"consecutive_rests={store.consecutive_rests}")

    blocked = bool(getattr(throttle_state, "blocked_alarm_sent", False))
    check(not blocked, "scheduled runs are getting answers",
          "a blocked alarm is outstanding" if blocked else "no blocked alarm")

    bad = int(getattr(throttle_state, "consecutive_bad", 0))
    check(bad <= 2, "grid is not degrading", f"consecutive_bad={bad}")

    if hours_since_email is None:
        check(False, "email is being delivered", "no email has ever been sent")
    else:
        check(hours_since_email <= 16.0, "email is being delivered",
              f"last one {hours_since_email:.1f}h ago")

    timings = sorted(v["secs"] for v in store.checked.values()
                     if isinstance(v, dict) and "secs" in v
                     and v.get("blank") is False)
    if len(timings) < 10:
        check(True, "pages that return fares are not slowing",
              f"only {len(timings)} timed samples yet - not judged")
    else:
        median = timings[len(timings) // 2]
        check(median <= 40.0, "pages that return fares are not slowing",
              f"median {median:.1f}s (9-27s is the measured band)")

    ready = all(checks)
    lines.append("")
    if ready:
        moves: list[str] = []
        if delay_s is not None:
            nxt = next_rate_step(delay_s)
            moves.append(
                f"   sweep rate   --delay {delay_s:.0f} -> {nxt:.0f}"
                if nxt is not None else
                f"   sweep rate   already at {delay_s:.0f}s, the floor of the "
                f"ladder - leave it"
            )
        if hot_list_size is not None:
            moves.append(
                f"   Chrome reach hot_list_size {hot_list_size} -> "
                f"{min(hot_list_size * 2, 24)} (fills chrome_max_per_run)"
                if hot_list_size < 18 else
                f"   Chrome reach hot_list_size is {hot_list_size}, enough to "
                f"fill chrome_max_per_run - leave it"
            )
        anything_left = any("->" in m for m in moves) or not moves
        lines.append(
            "READY. Raise ONE thing, then watch a full day before the next:"
            if anything_left else
            "READY - but there is nothing left to raise:"
        )
        lines.extend(moves or [
            "   sweep rate   --delay one rung down the ladder "
            f"({' -> '.join(f'{r:.0f}' for r in RATE_LADDER)})",
            "   Chrome reach hot_list_size, up to 18",
        ])
        lines.append("Never both at once - if the address complains you want to")
        lines.append("know which change did it.")
    else:
        lines.append("NOT READY. Change nothing yet; the failing lines say why.")
    return ready, lines


def coverage_report(windows: Sequence, store: "SweepStore", *,
                    threshold: int | None, delay_s: float = 90.0) -> list[str]:
    """How often each kind of window is revisited, and what that catches.

    The question this answers is the trip owner's: "a cheap price that lasts
    a day or two - do we get it?" For a fare that persists D days on a
    window revisited every R days, the chance of seeing it is ~min(1, D/R).
    So the guarantee is not about the search space, it is about R, and R is
    different for each tier.
    """
    cycle = delay_s + LAUNCH_SECONDS
    per_day = 86400 / cycle
    hot = set(hot_keys(store, threshold=threshold))
    pairs = promising_weekday_pairs(store, threshold=threshold)
    warm = {w.key for w in windows
            if (w.depart.weekday(), w.back.weekday()) in pairs}
    hs = needed_hot_share(len(hot), cycle_s=cycle)
    rs = (1.0 / RECHECK_EVERY) if store.suspect else 0.0
    cs = max(0.0, 1.0 - hs - WARM_SHARE - rs)

    rows = [f"{per_day:.0f} launches/day at {delay_s:.0f}s",
            "",
            f"{'tier':<24}{'windows':>9}{'share':>7}{'revisit':>11}"]
    for name, n, share in (("hot (known cheap)", len(hot), hs),
                           ("warm (plausible dates)", len(warm), WARM_SHARE),
                           ("re-check backlog", len(store.suspect), rs),
                           ("cold (all windows)", len(windows), cs)):
        if n == 0 or share <= 0:
            rows.append(f"{name:<24}{n:>9}{share*100:>6.0f}%{'-':>11}")
            continue
        rows.append(f"{name:<24}{n:>9}{share*100:>6.0f}%"
                    f"{n/(per_day*share):>9.1f} d")

    warm_r = len(warm) / (per_day * WARM_SHARE) if warm else float("inf")
    cold_r = len(windows) / (per_day * cs) if cs > 0 else float("inf")
    rows += ["", "a fare that lasts this long is caught:"]
    for d in (1, 2, 3, 7):
        rows.append(f"   {d} day(s): {min(1, d/warm_r)*100:>4.0f}% on a "
                    f"plausible date, {min(1, d/cold_r)*100:>4.0f}% elsewhere")
    return rows


def unverified_windows(windows: Sequence, store: "SweepStore") -> list[str]:
    """Windows walked this pass that we cannot honestly call empty.

    A window that returned nothing leaves no trace in `sweep_history.csv`
    and none in `found`, so before the `checked` ledger existed a genuine
    empty and a throttled one were indistinguishable afterwards. That is the
    question that matters after a throttle, and it could not be asked.

    Anything walked with no fare recorded and no trustworthy check behind it
    counts as unverified. Measured 2026-08-23 after a day of throttling:
    1,440 of 1,673 walked windows, ~960 of them in January and February -
    the months holding every cheap fare found so far. All were written off
    as "no fares on this date" and none were queued for a second look.
    """
    out = []
    for w in windows[:min(store.cursor, len(windows))]:
        if w.key in store.found:
            continue                       # it produced a fare; nothing to do
        rec = _checked_rec(store, w.key)
        if not rec or not rec.get("healthy"):
            out.append(w.key)
    return out


def queue_unverified(windows: Sequence, store: "SweepStore", *,
                     limit: int | None = None) -> int:
    """Put unverified windows back in the re-check queue. Returns how many."""
    already = set(store.suspect)
    add = [k for k in unverified_windows(windows, store) if k not in already]
    if limit is not None:
        add = add[:limit]
    store.suspect.extend(add)
    return len(add)


def promising_weekday_pairs(store: "SweepStore", *, threshold: int | None,
                            multiple: float = WARM_PAIR_MULTIPLE) -> set:
    """(depart weekday, return weekday) pairs that have produced a cheap fare.

    Derived, never hardcoded. If Edelweiss moves to Tuesdays this follows
    within a pass; a literal {Mon, Wed, Fri} would not.
    """
    if threshold is None:
        return set()
    ceiling = threshold * multiple
    pairs = set()
    for v in store.found.values():
        try:
            if float(v.get("price_usd", 10 ** 9)) > ceiling:
                continue
            d = Date.fromisoformat(v["depart"])
            b = Date.fromisoformat(v["ret"])
        except (ValueError, TypeError, KeyError):
            continue
        pairs.add((d.weekday(), b.weekday()))
    return pairs


# While a focus is active, one launch in this many still goes to the hot
# list. Freshness of the cheapest known fare is the thing the email is
# built on, and letting it go stale to finish a backfill sooner is a bad
# trade at any speed.
FOCUS_HOT_EVERY = 5


def _checked_rec(store: "SweepStore", key: str) -> dict:
    """The ledger entry for a window, or {} if it is not usable.

    The store is JSON on disk, written by a process that can be killed and
    occasionally edited by hand. `checked[key]` being a string rather than
    an object is exactly the "valid JSON is not the object this expects"
    shape that killed every scheduled run for four hours on 2026-08-23.
    Readers guard; writers cannot.
    """
    rec = store.checked.get(key)
    return rec if isinstance(rec, dict) else {}


def _tries(store: "SweepStore", key: str) -> int:
    """How many times a focus has priced this window, defensively."""
    try:
        return int(store.focus_tries.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def connection_proven(store: "SweepStore", window: int | None = None) -> bool:
    """Did anything in the recent stretch actually come back with fares?

    This is the evidence that makes an empty answer *mean* something. On
    its own an empty page says nothing about the connection - measured
    2026-08-24, a date with no flights answers in 3.6-4.6 seconds and a
    refusal looks identical.

    So an empty result is trusted only when something nearby worked. The
    previous rule required the empty page itself to have taken at least
    SUSPECT_FAST_SECONDS, which is unsatisfiable: an empty page is always
    fast. That made `healthy` permanently False for genuinely empty
    windows, so the re-check queue could never drain - it sat at ~1,330
    through five hours of continuous sweeping, rising as often as falling.

    Absence of evidence is not trust: right after a throttle rest both
    samples are cleared, so nothing is proven and empties stay queued.
    That blind spot is deliberate and this preserves it.
    """
    seen = list(store.recent_worked)[-(window or EMPTY_ALARM_WINDOW):]
    return any(seen)


# A focus never re-prices the same window twice inside this. Progress is
# guaranteed by rotation, not by the connection signal agreeing.
FOCUS_COOLDOWN_SECONDS = 900.0

# How many times a focus will price one window before moving on without it.
# "100%" then means "every window given this many honest attempts", which
# is reachable - unlike "every window trusted", which is not: a date that
# is genuinely blank can only be believed once something *else* has
# returned fares, and if nothing does, nothing ever can.
FOCUS_MAX_TRIES = 3


def focus_next(pending: Sequence, store: "SweepStore", *,
               cooldown_s: float = FOCUS_COOLDOWN_SECONDS,
               now: datetime | None = None):
    """The next focus window to price - never the one just priced.

    `focus_pending` keeps its month-then-date order, so January is still
    finished before February. This only skips past windows checked in the
    last `cooldown_s`, which is what makes progress structural.

    Without it a focus can deadlock, and the deadlock is self-reinforcing.
    A window that answers blank while nothing else has recently returned
    fares is not trusted, so it stays at the head of `pending` and is
    picked again - and since it is now the *only* thing being priced, the
    evidence needed to trust it can never arrive. Reproduced 2026-08-24
    with an empty hot list: eight launches, one window.

    In production the one-in-five freshness launch happens to break it, by
    pricing a hot window that returns fares. That is luck, not design: it
    only holds while the hot list is non-empty.
    """
    if not pending:
        return None
    now = now or datetime.now(timezone.utc)
    for w in pending:
        at = _checked_rec(store, w.key).get("at") or ""
        if not at:
            return w                      # never checked - take it
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            return w
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (now - when).total_seconds() >= cooldown_s:
            return w
    # Everything is inside the cooldown, which means the focus is nearly
    # done and lapping a small set. Take the least recently checked rather
    # than stalling.
    return min(pending,
               key=lambda w: _checked_rec(store, w.key).get("at") or "")


def focus_pending(windows: Sequence, store: "SweepStore",
                  months: Sequence[int],
                  max_tries: int = FOCUS_MAX_TRIES) -> list:
    """Windows in the focus months with no trustworthy answer yet.

    "100% of January" is not "January has been walked". A window counts as
    answered when it has a fare, or an empty result recorded while the
    connection was healthy. Anything else - never walked, or emptied while
    the connection was in doubt - is still an open question, and those are
    exactly the windows a focus exists to close.

    Ordered by the caller's month order first, then by date, so asking for
    [1, 2, 3] really does finish January before starting February.
    """
    rank = {m: i for i, m in enumerate(months)}
    out = []
    for w in windows:
        i = rank.get(w.depart.month)
        if i is None:
            continue
        if w.key in store.found:
            continue
        if _checked_rec(store, w.key).get("healthy"):
            continue
        # Given a fair number of attempts already. It stays in the ordinary
        # re-check queue - nothing is written off - but the focus stops
        # waiting on it, so the focus can actually end.
        if _tries(store, w.key) >= max_tries:
            continue
        out.append((i, w.depart, w.back, w))
    out.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in out]


def next_window(windows: Sequence, store: "SweepStore", *,
                threshold: int | None = None, hot_share: float = HOT_SHARE,
                warm_share: float = WARM_SHARE):
    """The next window to price: (window, was_hot).

    A window never priced counts as cold and is always taken when the cold
    cursor reaches it - an unpriced window might be the cheapest there is,
    and skipping it would leave whole regions permanently invisible.
    """
    if not windows:
        return None, False
    cold = windows[store.cursor % len(windows)]

    hot = [k for k in hot_keys(store, threshold=threshold)]
    if not hot:
        return cold, False          # nothing known yet

    # Deterministic interleave: every Nth launch is a hot one. Rotating
    # through the hot list rather than always taking the cheapest keeps the
    # whole hot set fresh instead of one window.
    # Truncate rather than round. `every` is the launch interval, so
    # rounding it *up* silently spends less on freshness than asked for and
    # a hot window can age past the limit the share was derived from.
    # Truncating can only ever make the interval shorter, i.e. err towards
    # fresher, which is the safe direction.
    every = max(int(1 / max(hot_share, 0.01)), 2)
    if store.windows_priced % every == 0:
        by_key = {w.key: w for w in windows}
        picks = [by_key[k] for k in hot if k in by_key]
        if picks:
            return picks[(store.windows_priced // every) % len(picks)], True

    # Warm: dates whose weekday pair has actually produced a cheap fare.
    # Rotated on their own cursor so the whole plausible set stays fresh
    # rather than one corner of it.
    #
    # This deliberately fires even when the cold window is one never priced
    # before. The first version returned early on an unpriced cold window -
    # the rule that guarantees coverage - and since the sweep is 37% into
    # its first pass that was almost every launch, so the warm tier fired
    # 12% of the time against a 10% share of the space: it did essentially
    # nothing. Coverage is not weakened by interleaving, only slowed: an
    # unpriced window is still always taken when the cold cursor reaches it.
    if warm_share > 0:
        warm_every = max(int(1 / warm_share), 2)
        if store.windows_priced % warm_every == 1:
            pairs = promising_weekday_pairs(store, threshold=threshold)
            if pairs:
                warm = [w for w in windows
                        if (w.depart.weekday(), w.back.weekday()) in pairs]
                if warm:
                    store.warm_index = (store.warm_index + 1) % len(warm)
                    return warm[store.warm_index], True
    return cold, False


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


# A window with no visa-free option is normal - measured, 13% of them are
# genuinely all-US or all-Canada routings. A *run* of them is not. When
# Google throttles, it answers in three seconds with an empty page, and the
# sweep cannot tell that from "no fares here" - it records a false empty,
# moves on, and does not look again for a whole pass.
#
# Measured 2026-08-23: pricing windows from a second process at the same
# time took the hit rate from 87% to 24%, and the empty responses came back
# in 3-4s against the 6s a real page takes.
EMPTY_ALARM_WINDOW = 20        # judge over this many recent windows
EMPTY_ALARM_RATE = 0.60        # above this, assume throttling rather than truth
THROTTLE_BACKOFF = 4.0         # multiply the delay while it looks throttled
SUSPECT_FAST_SECONDS = 4.5     # a genuine page has never come back this fast
THROTTLE_REST_SECONDS = 900    # full stop after this looks sustained
# Each rest that fails to clear it doubles the next one, up to an hour. A
# fixed 15 minutes just cycles: rest, resume, get throttled again, rest.
# Observed 2026-08-23 doing exactly that for forty minutes.
THROTTLE_REST_MAX = 3600
JITTER_FRACTION = 0.25         # +/- this much on every delay
# The re-check backlog gets one launch in eight, not one in four.
#
# It competes directly with the cold rotation, and the two do the same job:
# every queued window sits behind the cursor, so the pass would re-price it
# anyway. Measured 2026-08-24 with a 1,256-window backlog, a quarter of the
# launches pushed a full cold pass from 4.9 days out to 8.2 - and the
# backlog is mostly January and February dates that have *never* produced a
# cheap fare, while October to December were still unexplored.
#
# An eighth keeps the priority months front-loaded, which is worth
# something, without paying for it in the frontier.
RECHECK_EVERY = 8
# Bound on the per-window check ledger. Older entries fall off; they
# describe a check too old to be worth trusting anyway.
MAX_CHECKED = 6000


def looks_throttled(recent: Sequence[int], *, window: int = EMPTY_ALARM_WINDOW,
                    rate: float = EMPTY_ALARM_RATE,
                    blank: Sequence[int] | None = None) -> bool:
    """True when Google appears to be refusing rather than answering.

    A high fast-empty rate is necessary but not sufficient, and `blank` is
    what makes it sufficient: **a throttle takes everything down**. If any
    page in the same stretch came back with fares on it, the connection is
    working and the empties are the calendar. That is the same rule
    `cli.run_looks_blocked` already applies to a scheduled run, and the
    sweep's own detector did not have it.

    It matters because the timing premise this detector rests on is not
    holding. Measured 2026-08-24, after `elapsed` was fixed to time the
    fetch rather than the wait for the lock:

        pages that returned fares    13.2s  14.4s  19.1s  26.0s
        pages with no fares           3.8s  3.9s  4.1s  4.1s  4.4s ...

    Tight, non-overlapping clusters - and the empties were consecutive
    return dates on one departure day, interleaved with windows that
    answered with seventeen results. That is a calendar, not a refusal.
    The file's original premise, "a date Google genuinely has no flights
    for still costs it the time to say so", is simply false on this
    machine now: an empty page is *always* fast. So a fast-empty rate on
    its own measures the calendar, exactly like the plain-empty rate it
    was brought in to replace.

    Rather than move the threshold again - the third time would be the
    third guess - this asks the question the data can actually answer.
    It can only ever make the detector quieter, which is the agreed
    direction to be wrong in.
    """
    sample = list(recent)[-window:]
    if len(sample) < window:
        return False
    if (sum(sample) / len(sample)) <= rate:
        return False
    if blank is not None:
        seen = list(blank)[-window:]
        if any(b == 0 for b in seen):
            return False
    return True


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


def rest_in_slices(sleep: Callable[[float], None], seconds: float,
                   should_stop: Callable[[], bool] | None,
                   *, step: float = 5.0) -> bool:
    """Sleep `seconds`, but notice a stop request while doing it.

    A throttle rest is 15 to 60 minutes and used to be one flat
    `sleep(rest_for)`. Because `sweep_forever` installs a SIGINT handler
    that only sets a flag, Ctrl-C during a rest did nothing at all: Python
    ran the handler and went straight back to sleeping the remainder, and
    the flag is not read until `sweep_batch` returns. Observed 2026-08-23 -
    the trip owner pressed Ctrl-C twice, got "Stop requested" twice, and the
    process sat there for another twenty minutes.

    Returns True if it was asked to stop.
    """
    if should_stop is None:
        sleep(seconds)
        return False
    slept = 0.0
    while slept < seconds:
        if should_stop():
            return True
        chunk = min(step, seconds - slept)
        sleep(chunk)
        slept += chunk
    return should_stop()


def _safe_call(fn, *args, what: str = "callback", default=None):
    """Run an injected callback without letting it end the sweep.

    Audited 2026-08-24 after an integration test caught the alarm doing
    exactly this: `on_find` and `should_stop` were both unguarded too, and
    either would abort the batch and cost the outer loop a 60-second pause
    plus the remaining windows.

    `on_find` is the realistic one - it formats a `Discovery` built from
    scraped data, so a malformed date is all it takes. None of these
    callbacks does anything the sweep depends on, which is precisely why
    none of them should be able to stop it.
    """
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception as exc:              # noqa: BLE001
        log.warning("%s failed (%s); sweeping on", what, exc)
        return default


def _safe_alarm(on_alarm, kind: str, facts: dict) -> None:
    """Raise an alarm without letting it take the sweep down.

    `sweep_forever` wraps its own callback, but `sweep_batch` must not
    depend on a caller doing that: an injected callback that raises aborted
    the whole batch, and the outer loop then paused 60 seconds and lost the
    remaining windows. Found by an integration test 2026-08-24 - the unit
    tests covered the email builders and never exercised the wiring.

    The same reasoning as everywhere else here: the sweep surviving matters
    more than the telling.
    """
    if on_alarm is None:
        return
    try:
        on_alarm(kind, facts)
    except Exception as exc:              # noqa: BLE001
        log.warning("alarm callback failed (%s); sweeping on", exc)


def sweep_batch(
    windows: Sequence,
    store: SweepStore,
    *,
    origin: str = "SJO",
    destination: str = "TYO",
    max_stops: int | None = 2,
    max_total_hours: int | None = None,
    focus_months: Sequence[int] = (),
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
    lock_path: str = gate.DEFAULT_LOCK,
    lock_timeout: float = 300.0,
    hot_threshold: int | None = None,
    hot_share: float = HOT_SHARE,
    save_to: str | Path | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_alarm: Callable[[str, dict], None] | None = None,
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
        if _safe_call(should_stop, what="should_stop", default=False):
            break
        if store.cursor >= len(windows):
            store.cursor = 0
            store.passes_completed += 1
            store.pass_started = _now()
            log.info("Sweep completed pass %d", store.passes_completed)

        # Anything queued for a second look comes first: those are windows
        # that answered empty while the sweep looked throttled, so their
        # "no fares" verdict was never trustworthy.
        # A suspect window is re-checked only once the connection looks
        # healthy again. Re-checking mid-throttle just collects another false
        # empty and teaches us nothing, which is why a plain retry counter
        # was not enough.
        # Re-checks take a bounded share of launches. A backlog can be
        # large - 1,440 windows were left unverified by the 2026-08-23
        # throttle - and draining one per launch would stall the cold
        # rotation for a day and a half, trading one blind spot for
        # another.
        healthy = not looks_throttled(store.recent,
                                      blank=store.recent_blank)

        # A focus finishes the months that matter before spending anything
        # on the rest. It redirects effort; it never asks for more of it,
        # which is the only kind of "go faster" that is safe here.
        #
        # `replay=True` is what freezes the cold cursor, so the rotation
        # resumes exactly where it stopped once the focus is done. The
        # alternative - walking the cursor past months not in focus - would
        # leave those windows in none of the four coverage states, silently
        # written off, which is the one outcome this store exists to
        # prevent.
        # A focus is a *session*, not a standing condition. Once it has
        # finished it stays finished for the life of this process, and the
        # ordinary rotation owns the sweep again.
        #
        # Without that, the two tiers oscillate: the cold rotation
        # eventually wraps back into the focus months, prices a window,
        # gets a blank it cannot yet trust - and the window re-enters
        # `focus_pending` on the spot, freezing the cursor again for
        # another few launches. Measured 2026-08-24: a batch of two after
        # completion advanced the cursor by one. Bounded by FOCUS_MAX_TRIES
        # rather than infinite, but it stalls the rotation in exactly the
        # months the focus has already finished, and re-logs "complete"
        # every time round.
        #
        # Restarting the sweep is how a focus is asked for again, and the
        # startup path clears both this flag and the attempt counters.
        pending = (focus_pending(windows, store, focus_months)
                   if focus_months and not store.focus_done_logged else [])
        if focus_months and not pending and not store.focus_done_logged:
            log.info("Focus on month(s) %s complete: every window has a fare "
                     "or a trusted answer. Resuming the full rotation.",
                     ", ".join(str(m) for m in focus_months))
            store.focus_done_logged = True

        recheck_turn = store.windows_priced % RECHECK_EVERY == 0
        # One launch in FOCUS_HOT_EVERY still goes to the hot list even
        # under a focus. The cheapest known fare is what the email is built
        # on, and letting it go stale to finish a backfill sooner is a bad
        # trade at any speed.
        # The freshness launch is only worth taking when there is actually
        # a hot window to take. Without that check `next_window` falls back
        # to the cold cursor, which creeps forward through the very months
        # the focus is deferring - and those windows would then be behind
        # the cursor with no answer, i.e. silently written off.
        freshness_turn = store.windows_priced % FOCUS_HOT_EVERY == 0
        hot_now = bool(hot_keys(store, threshold=hot_threshold))
        focus_turn = (pending and healthy
                      and not (freshness_turn and hot_now))
        if focus_turn:
            w = focus_next(pending, store)
            if w is None:
                w = pending[0]
            store.focus_tries[w.key] = _tries(store, w.key) + 1
            if w.key in store.suspect:
                store.suspect.remove(w.key)
            replay = True               # freezes the cold cursor
            is_recheck = True
        elif store.suspect and healthy and recheck_turn and not pending:
            key = store.suspect.pop(0)
            w = next((x for x in windows if x.key == key), None)
            if w is None:
                # It really has rolled out of the search window - min_lead_days
                # moves the front edge forward every day - so dropping it is
                # right. Say so, because the alternative reading of a
                # shrinking queue is that the re-checks are happening, and
                # this is the one place a queued window can leave without
                # ever being priced.
                log.debug("re-check key %s is no longer a live window; "
                          "dropping it", key)
                store.dropped_rechecks += 1
                continue
            replay = True
            is_recheck = True
        else:
            # Hot windows are re-priced out of turn and must not consume the
            # cold cursor, or coverage would stall on the cheap ones.
            # Spend only what freshness actually requires; the rest goes
            # on coverage. `hot_share` is the ceiling, not the setting.
            if pending:
                # The focus's freshness launch. Pick the hot window
                # *directly* rather than asking `next_window` for one:
                # with hot_share=1.0 its interleave works out to "every
                # second launch", so on the others it falls through to the
                # cold cursor and advances it - measured 2026-08-24, the
                # cursor crept forward during a focus that had promised to
                # freeze it. Nothing was lost, but the focus was diluted
                # and the log said something untrue.
                hot = hot_keys(store, threshold=hot_threshold)
                by_key = {x.key: x for x in windows}
                picks = [by_key[k] for k in hot if k in by_key]
                if picks:
                    w = picks[(store.windows_priced // FOCUS_HOT_EVERY)
                              % len(picks)]
                    was_hot = True
                else:
                    w, was_hot = focus_next(pending, store) or pending[0], True
            else:
                share = needed_hot_share(
                    len(hot_keys(store, threshold=hot_threshold)),
                    cycle_s=delay_s + LAUNCH_SECONDS, cap=hot_share)
                w, was_hot = next_window(windows, store,
                                         threshold=hot_threshold,
                                         hot_share=share)
            if w is None:
                break
            replay = was_hot
            is_recheck = False

        depart, ret = w.depart, w.back
        url = _search_url(origin, destination, depart, ret, max_stops)
        elapsed = 0.0
        measured = False
        try:
            # Per window, not per run. Holding it for a whole pass would
            # make the scheduled runs queue behind fourteen hours of sweep.
            with gate.google("sweep", path=lock_path, timeout=lock_timeout,
                             on_timeout="wait"):
                # The clock starts *inside* the lock, and that placement is
                # the whole point. `elapsed` is the throttle detector's only
                # input and it also decides whether an empty answer may be
                # trusted - and the sweep waits behind the scheduled runs'
                # Chrome phase for about four minutes, six times a day.
                # Timing from outside charged that wait to Google: measured
                # 2026-08-24, one window recorded 216.5s when no real fetch
                # can exceed its own 120s timeout and no page that returned
                # fares has ever taken over 26.8s.
                #
                # It was wrong in both directions. A throttled page that had
                # queued looked slow, so the detector missed it precisely
                # when a scheduled run was hitting Google too; and the same
                # window was then stamped `healthy`, meaning "genuinely
                # empty, trusted", so it was never re-checked.
                started = time.monotonic()
                dom = grab(url)
                elapsed = time.monotonic() - started
                measured = True
        except Exception as exc:            # noqa: BLE001 - never die mid-sweep
            log.debug("sweep fetch failed for %s: %s", depart, exc)
            dom = ""

        # Parse once, then filter, so the shortfall check can see what was
        # dropped and why. Filtering inside the comprehension hid both.
        stats: dict = {}
        parsed = parse_options(dom, origin=origin, destination=destination,
                               depart_date=depart, return_date=ret,
                               stats=stats)
        options = [o for o in parsed
                   if o.visa_ok and within_duration(o, max_total_hours)]
        _note_unresearched(store, parsed)
        claimed = claimed_result_count(dom)
        if claimed is not None and claimed > len(parsed):
            store.shortfalls += 1
            # Whether the rows we could not reach are the dear ones depends
            # entirely on how Google orders its list. Record it from the DOM
            # we already have, rather than spending requests to find out.
            order = dom_price_order(dom)
            if order:
                if order == sorted(order):
                    store.dom_sorted += 1
                else:
                    store.dom_unsorted += 1
            # Say "unknown" rather than "ascending" when the order could
            # not be read: [] == sorted([]) is True, so an unreadable page
            # would otherwise report the reassuring answer.
            if not order:
                verdict = "row order unknown"
            elif order == sorted(order):
                verdict = "row order ascending"
            else:
                verdict = "row order NOT ascending"
            # Three numbers, because the gap between the first two has two
            # completely different causes and only `rows` tells them apart.
            rows = dom_row_count(dom)
            dup = stats.get("duplicate", 0)
            unmatched = stats.get("unmatched", 0)
            unpriced = stats.get("unpriced", 0)
            if unmatched:
                store.rows_missed_by_parser += unmatched
            store.rows_unpriced += unpriced
            store.rows_deduped += dup
            log.info("%s +%dn: Google claims %d, DOM %d rows, parsed %d "
                     "(%d duplicate, %d unpriced, %d unreadable) - %s",
                     depart, (ret - depart).days, claimed, rows, len(parsed),
                     dup, unpriced, unmatched, verdict)
        blind = unreadable_count(parsed)
        if blind:
            store.unreadable += blind
            log.warning("%s +%dn: %d option(s) dropped - routing unreadable, "
                        "so the visa rule could not be checked",
                        depart, (ret - depart).days, blind)
        if options:
            cheapest = min(options, key=lambda o: o.price_usd)
            cheapest = _with_link(cheapest, max_stops)
            if store.record(cheapest) and on_find is not None:
                _safe_call(on_find, Discovery.from_option(cheapest),
                           what="on_find")

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

        # Record whether this window came back empty, and re-queue it if the
        # emptiness is suspicious - too fast to be a real page, or arriving
        # in the middle of a run of them.
        #
        # Re-checks are deliberately left out of the health sample. They are
        # windows queued *because* they came back empty, so re-pricing them
        # produces more empties, which raises the measured empty rate, which
        # trips the throttle detector, which queues more windows. A feedback
        # loop that ends in the sweep reporting a throttle it caused itself.
        #
        # That is exactly what happened on 2026-08-24: the alarm fired at 70%
        # while Google was in fact answering 15-16 options on most windows,
        # the independent HTTP grid sat steady at 25%, and the sweep was
        # still logging fares throughout. `recent` must measure the
        # *connection*, so only a fresh pick is evidence about it.
        # Judge the *connection*, not the visa filter. `options` is the
        # visa-free survivors; `parsed` is everything Google returned. A
        # November Saturday returns 12-16 perfectly good options that are
        # all US or Canada routings, so every one of them is visa-rejected
        # and `options` is empty - which read as "Google sent nothing".
        #
        # It is not the same thing at all, and the difference is the whole
        # question. CLAUDE.md has said so since 2026-08-22: "the
        # discriminator is whether the payload contains any price at all: a
        # genuine no-results page has zero, a good one had 96." The detector
        # was not using it.
        #
        # This is what fired the false alarm on 2026-08-24 at 70%: the cold
        # cursor had walked into November Saturdays, which carry no Zurich
        # routing at all (0 of 58 measured), while the warm picks in the same
        # minutes were getting 16 results each.
        # Exclude *re-checks* only. Hot and warm picks are fresh queries to
        # Google and are perfectly good evidence about the connection - the
        # first version of this guard keyed off `replay`, which is also true
        # for them, so the sample collapsed to cold picks alone. With the
        # cold cursor grinding through November Saturdays that read as 100%
        # empty while hot picks in the same minutes were getting 17 results.
        if not is_recheck:
            # **A throttle is fast; a barren date is not.**
            #
            # Counting every empty made the detector a measure of how good
            # the *dates* are, not how good the *connection* is. The cold
            # cursor walking November Saturdays - which carry no Zurich
            # routing at all - read as 90% empty while hot picks in the same
            # minutes were getting 17 results, and the alarm emailed a
            # throttle that was not happening.
            #
            # The discriminator was measured on 2026-08-23 and then never
            # used here: a throttled page comes back in 3-4 seconds, a real
            # one takes about 6. So only a *suspiciously fast* empty is
            # evidence of a throttle. A date Google genuinely has no flights
            # for still costs it the time to say so.
            # A fetch that raised produced no timing, so it says nothing
            # about the connection either way. Appending a 0 would be a
            # sample claiming "not throttled" on no evidence.
            if measured:
                store.recent.append(
                    1 if (not parsed and elapsed < SUSPECT_FAST_SECONDS) else 0)
                del store.recent[:-EMPTY_ALARM_WINDOW * 2]
            store.recent_blank.append(0 if parsed else 1)
            del store.recent_blank[:-EMPTY_ALARM_WINDOW * 2]
        # Every launch, including re-checks and focus picks - see
        # `connection_proven`. Without this a focus records nothing at all
        # (its picks are re-checks), so nothing is ever proven and the
        # first empty window it meets is re-priced for ever.
        store.recent_worked.append(1 if parsed else 0)
        del store.recent_worked[:-EMPTY_ALARM_WINDOW * 2]
        throttled = looks_throttled(store.recent,
                                    blank=store.recent_blank)
        proven = connection_proven(store)

        # An empty answer used to leave no trace anywhere: nothing is written
        # to sweep_history, and `found` only holds windows that produced a
        # fare. So a genuine empty and a throttled one were indistinguishable
        # afterwards, and there was no way to ask "which windows were checked
        # while the connection was bad?" - which is exactly the question that
        # matters after a throttle. Every check is now stamped.
        # Whoever priced it, it no longer needs re-checking. Without this
        # the cold pass and the re-check queue both visit the same window:
        # measured 2026-08-24, all 1,256 queued windows sat behind the
        # cursor, so every one of them was going to be re-priced by the pass
        # regardless.
        if w.key in store.suspect:
            store.suspect.remove(w.key)

        store.checked[w.key] = {
            "at": _now(),
            # Two different questions, and conflating them is what sent a
            # 70% false alarm on 2026-08-24. `empty` is "nothing this
            # passport can use"; `blank` is "Google returned no fares at
            # all". A window answering with fourteen US-transit options is
            # emptied by the visa rule, not by Google, and only `blank`
            # says anything about the connection.
            "empty": not options,
            "blank": not parsed,
            # "Was the connection trustworthy when this was checked?" -
            # and nothing else. It used to also require the page to have
            # taken at least SUSPECT_FAST_SECONDS, which quietly made it
            # unsatisfiable: measured 2026-08-24, a page with no fares
            # comes back in 3.6-4.6s *always*, so an empty window could
            # never be marked healthy, could never leave the re-check
            # queue, and was re-priced for ever.
            #
            # The backlog is the proof. It sat at ~1,330 for five hours of
            # continuous sweeping, rising as often as falling, because
            # every genuinely empty window went straight back on the end
            # of it. And `focus_pending` reads the same flag, so a focus
            # would loop on the first empty window and never complete -
            # observed live, re-pricing 2027-01-03 +33n on consecutive
            # launches.
            #
            # The timing term was measuring whether the page had fares, not
            # whether Google was answering. `looks_throttled` is the
            # connection signal now, and it asks the question that can
            # actually be answered: did anything in this stretch come back
            # with fares on it.
            "healthy": measured and not throttled and proven,
            # Kept so the timing threshold can be re-calibrated from
            # real data rather than from the one measurement in 2026.
            "secs": round(elapsed, 1),
        }
        if len(store.checked) > MAX_CHECKED:
            for k in sorted(store.checked,
                            key=lambda k: store.checked[k]["at"])[:len(store.checked) - MAX_CHECKED]:
                del store.checked[k]
        # An empty answer is only trustworthy when the connection is healthy
        # AND the page took long enough to have really been rendered. Both
        # failing means "unverified", not "no fares". A replay that comes
        # back empty while healthy is trustworthy, so it simply leaves the
        # list rather than looping there forever.
        # Same correction, same reason: a fast empty is what a date with no
        # flights looks like, not what a refusal looks like. Re-queueing on
        # speed alone is what made the queue undrainable.
        unverified = not options and (throttled or not measured or not proven)
        if unverified and w.key not in store.suspect:
            store.suspect.append(w.key)

        if not replay:
            store.last_key = w.key
            store.cursor += 1
        store.windows_priced += 1
        store.last_active = _now()
        done += 1

        # Persist here, not just at the end of the batch. `--status` reads
        # this file, and a batch of 25 is ~40 minutes of pricing - longer
        # once a throttle starts adding rests. Saving per batch therefore
        # made the health line most stale exactly when it was most needed:
        # asked mid-throttle on 2026-08-23 it reported a 91-minute-old
        # snapshot, which read as "still stuck" long after the rests began
        # working. The write is atomic and the file is ~200KB, so the cost
        # is irrelevant beside answering "is it throttled right now?".
        if save_to:
            try:
                store.save(save_to)
            except OSError as exc:
                log.debug("could not save the sweep store: %s", exc)

        if throttled:
            if not store.throttled_since:
                store.throttled_since = _now()
                store.throttle_events += 1
            store.last_throttle = _now()
            rate = 100 * sum(store.recent[-EMPTY_ALARM_WINDOW:]) / EMPTY_ALARM_WINDOW
            stuck_s = _age_hours(store.throttled_since) * 3600
            rest_for = min(THROTTLE_REST_SECONDS * (2 ** store.consecutive_rests),
                           THROTTLE_REST_MAX)
            if stuck_s > THROTTLE_REST_SECONDS:
                # Per-request backoff has not helped for a quarter of an
                # hour. Stop entirely for a while: continuing to poke a host
                # that is already refusing is how a soft throttle becomes a
                # hard block, and the scheduled runs share this IP.
                store.consecutive_rests += 1
                store.rests_total += 1
                log.warning("Still throttled after %.0f min - resting %.0f min "
                            "(rest #%d). %d window(s) waiting to be re-checked.",
                            stuck_s / 60, rest_for / 60,
                            store.consecutive_rests, len(store.suspect))
                # Tell the trip owner, once per throttle event. A background
                # process that goes quiet looks exactly like one that is
                # working: on 2026-08-23 the address was throttled most of a
                # day and the only trace was a log line nobody was reading.
                if on_alarm is not None and store.alarm_sent_for != store.throttled_since:
                    store.alarm_sent_for = store.throttled_since
                    _safe_alarm(on_alarm, "blocked", {
                        "empty_rate": rate, "since": store.throttled_since,
                        "suspect": len(store.suspect),
                        "rest_minutes": rest_for / 60,
                        "rest_number": store.consecutive_rests})
                stopped = rest_in_slices(sleep, rest_for, should_stop)
                store.recent.clear()        # judge the next stretch afresh
                store.recent_blank.clear()
                store.recent_worked.clear()  # and "did anything work"
                store.throttled_since = ""
                if stopped:
                    break
                continue
            # Say what is actually being measured. "Empty rate" was the old
            # wording and it is now wrong twice over: this counts only fast
            # empties, and a date with no flights is not one of them.
            log.warning("%.0f%% of the last %d windows came back empty *fast* "
                        "(under %.1fs) - that is a throttle, not a quiet "
                        "date. Slowing down; %d window(s) queued for a "
                        "re-check once it clears.",
                        rate, EMPTY_ALARM_WINDOW, SUSPECT_FAST_SECONDS,
                        len(store.suspect))
        elif store.throttled_since or store.alarm_sent_for:
            # Not throttled now. Two separate things may need closing, and
            # keying both off `throttled_since` was wrong: resting clears
            # it, so an episode that was alarmed *and then rested* left
            # `alarm_sent_for` set with nothing able to clear it. The
            # all-clear then rode the next unrelated episode.
            #
            # Live on 2026-08-24: the 11:20 alarm rested, and its all-clear
            # arrived at 16:55 - five and a half hours later - reporting
            # "cleared after 5 minutes", which was the duration of a
            # completely different blip that evening. The trip owner spotted
            # it immediately: "the block was early today, weird".
            #
            # So recovery is keyed on the outstanding *alarm*, and its
            # duration is measured from the episode that alarm was about.
            #
            # A rest also empties `recent`, so "not throttled" can simply
            # mean "not enough samples yet". Waiting for a full window
            # before declaring an all-clear costs about half an hour and
            # keeps the message honest.
            settled = len(store.recent) >= EMPTY_ALARM_WINDOW
            if settled:
                if store.throttled_since:
                    log.info("Empty rate back to normal after %.0f min; "
                             "%d window(s) to re-check.",
                             _age_hours(store.throttled_since) * 60,
                             len(store.suspect))
                if store.alarm_sent_for and on_alarm is not None:
                    _safe_alarm(on_alarm, "recovered", {
                        "minutes": _age_hours(store.alarm_sent_for) * 60,
                        "suspect": len(store.suspect),
                        "windows_priced": store.windows_priced})
                store.alarm_sent_for = ""
                store.throttled_since = ""
                store.consecutive_rests = 0

        # Jitter every wait. A request every six seconds on a perfect clock
        # is a fingerprint; nobody browses like a metronome.
        nap = delay_s * (THROTTLE_BACKOFF if throttled else 1.0)
        nap *= 1.0 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION)
        if nap:
            sleep(nap)
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
