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
from .browser import BrowserOption, chrome_path, fetch_dom, parse_options
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
    recent: list = field(default_factory=list)   # 1 = empty, 0 = had fares
    # Windows whose "no fares" answer arrived while the connection looked
    # throttled. They are not empty, they are *unverified*, and they stay
    # here until they can be re-checked during a healthy stretch.
    suspect: list = field(default_factory=list)
    throttle_events: int = 0
    warm_index: int = 0        # rotation over the schedule-plausible windows
    # key -> {at, empty, healthy}. Every check, not just the finds.
    checked: dict = field(default_factory=dict)
    consecutive_rests: int = 0
    throttled_since: str = ""
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
        self.throttled_since = ""
        self.consecutive_rests = 0
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

    def health(self) -> str:
        """One line on whether the connection is being trusted right now."""
        recent = self.recent[-EMPTY_ALARM_WINDOW:]
        rate = (100 * sum(recent) / len(recent)) if recent else 0.0
        bits = [f"empty rate {rate:.0f}% (13% is normal)"]
        if self.suspect:
            bits.append(f"{len(self.suspect)} window(s) awaiting a re-check")
        if self.throttled_since:
            bits.append("THROTTLED NOW since "
                        f"{self.throttled_since[11:16]} UTC")
        elif self.throttle_events:
            bits.append(f"{self.throttle_events} throttle event(s) so far")
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


# A Chrome launch that renders a real result page, measured on this machine.
# Used to turn a delay into a launch rate.
LAUNCH_SECONDS = 6.1
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
        rec = store.checked.get(w.key)
        if rec is None or not rec.get("healthy"):
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
# One launch in four goes to the re-check backlog, so recovering from a
# throttle cannot starve the cold rotation.
RECHECK_EVERY = 4
# Bound on the per-window check ledger. Older entries fall off; they
# describe a check too old to be worth trusting anyway.
MAX_CHECKED = 6000


def looks_throttled(recent: Sequence[int], *, window: int = EMPTY_ALARM_WINDOW,
                    rate: float = EMPTY_ALARM_RATE) -> bool:
    """True when the recent empty rate is too high to be honest."""
    sample = list(recent)[-window:]
    if len(sample) < window:
        return False
    return (sum(sample) / len(sample)) > rate


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


def sweep_batch(
    windows: Sequence,
    store: SweepStore,
    *,
    origin: str = "SJO",
    destination: str = "TYO",
    max_stops: int | None = 2,
    max_total_hours: int | None = None,
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
        if should_stop is not None and should_stop():
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
        healthy = not looks_throttled(store.recent)
        recheck_turn = store.windows_priced % RECHECK_EVERY == 0
        if store.suspect and healthy and recheck_turn:
            key = store.suspect.pop(0)
            w = next((x for x in windows if x.key == key), None)
            if w is None:
                continue            # window expired out of the rolling span
            replay = True
        else:
            # Hot windows are re-priced out of turn and must not consume the
            # cold cursor, or coverage would stall on the cheap ones.
            # Spend only what freshness actually requires; the rest goes
            # on coverage. `hot_share` is the ceiling, not the setting.
            share = needed_hot_share(
                len(hot_keys(store, threshold=hot_threshold)),
                cycle_s=delay_s + LAUNCH_SECONDS, cap=hot_share)
            w, was_hot = next_window(windows, store, threshold=hot_threshold,
                                     hot_share=share)
            if w is None:
                break
            replay = was_hot

        depart, ret = w.depart, w.back
        url = _search_url(origin, destination, depart, ret, max_stops)
        started = time.monotonic()
        try:
            # Per window, not per run. Holding it for a whole pass would
            # make the scheduled runs queue behind fourteen hours of sweep.
            with gate.google("sweep", path=lock_path, timeout=lock_timeout):
                dom = grab(url)
        except Exception as exc:            # noqa: BLE001 - never die mid-sweep
            log.debug("sweep fetch failed for %s: %s", depart, exc)
            dom = ""
        elapsed = time.monotonic() - started

        options = [o for o in parse_options(
            dom, origin=origin, destination=destination,
            depart_date=depart, return_date=ret)
            if o.visa_ok and within_duration(o, max_total_hours)]
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

        # Record whether this window came back empty, and re-queue it if the
        # emptiness is suspicious - too fast to be a real page, or arriving
        # in the middle of a run of them.
        store.recent.append(0 if options else 1)
        del store.recent[:-EMPTY_ALARM_WINDOW * 2]
        throttled = looks_throttled(store.recent)

        # An empty answer used to leave no trace anywhere: nothing is written
        # to sweep_history, and `found` only holds windows that produced a
        # fare. So a genuine empty and a throttled one were indistinguishable
        # afterwards, and there was no way to ask "which windows were checked
        # while the connection was bad?" - which is exactly the question that
        # matters after a throttle. Every check is now stamped.
        store.checked[w.key] = {
            "at": _now(),
            "empty": not options,
            "healthy": not throttled and elapsed >= SUSPECT_FAST_SECONDS,
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
        unverified = not options and (throttled or elapsed < SUSPECT_FAST_SECONDS)
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
                log.warning("Still throttled after %.0f min - resting %.0f min "
                            "(rest #%d). %d window(s) waiting to be re-checked.",
                            stuck_s / 60, rest_for / 60,
                            store.consecutive_rests, len(store.suspect))
                stopped = rest_in_slices(sleep, rest_for, should_stop)
                store.recent.clear()        # judge the next stretch afresh
                store.throttled_since = ""
                if stopped:
                    break
                continue
            log.warning("Empty rate %.0f%% over the last %d windows - looks "
                        "throttled, not empty. Slowing down; %d window(s) "
                        "queued for a re-check once it clears.",
                        rate, EMPTY_ALARM_WINDOW, len(store.suspect))
        elif store.throttled_since:
            log.info("Empty rate back to normal after %.0f min; "
                     "%d window(s) to re-check.",
                     _age_hours(store.throttled_since) * 60, len(store.suspect))
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
