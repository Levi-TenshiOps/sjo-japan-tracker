"""Turn "depart sometime in this window, for 2-5 weeks" into actual searches.

The combinatorics bite immediately. A 240-day departure window sampled every
3 days is 80 dates; times 4 trip lengths is 320 date pairs; times 3 Japanese
airports is 960 searches. No schedule survives that.

The fix is to stop treating every run as a full scan:

* **Hot list** - the combinations that have actually been cheap so far are
  re-priced on *every* run. This is what makes "give priority to the
  cheapest" real: the best candidates are watched several times a day and a
  drop is caught within hours.
* **Cold rotation** - the rest of the space is split into slices, one slice
  per run. With four runs a day a 28-slice space is fully covered weekly,
  and the slice index persists across runs so coverage does not restart.

Result: a small, predictable number of requests per run, complete coverage
over a week, and fast reaction on the fares that matter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Sequence

from .preferences import Preferences


@dataclass(frozen=True)
class Window:
    """One concrete trip: leave on this date, come back on that one."""

    depart: Date
    back: Date
    priority: bool = False

    @property
    def nights(self) -> int:
        return (self.back - self.depart).days

    @property
    def key(self) -> str:
        return f"{self.depart.isoformat()}_{self.back.isoformat()}"

    def as_pair(self) -> tuple[Date, Date]:
        return (self.depart, self.back)


# Fixed anchor for the departure sample grid. Any constant date works; what
# matters is that it never moves, so the same calendar dates are sampled on
# every run regardless of when the run happens.
GRID_EPOCH = Date(2000, 1, 3)


def snap_to_grid(day: Date, step: int) -> Date:
    """The first grid date on or after `day`."""
    if step <= 1:
        return day
    offset = (day - GRID_EPOCH).days % step
    return day if offset == 0 else day + timedelta(days=step - offset)


def generate_windows(
    prefs: Preferences, *, today: Date | None = None,
    min_lead_days: int | None = None,
) -> list[Window]:
    """Every (depart, return) pair implied by the preferences.

    Departures already past, or too close to book sensibly, are skipped.

    The sample grid is anchored to `GRID_EPOCH`, never to `today`. Anchoring
    it to today looks harmless and is not: the whole grid would slide forward
    one day every day, so with `departure_step_days: 4` only one day in four
    would sample the same departure dates as today. The hot list is keyed by
    (depart, return), so it would find no match on the other three days and
    silently go empty, and the rotation cursors would be indexing a different
    pool on every run. A fixed grid is what makes coverage add up over time.
    """
    today = today or Date.today()
    earliest, latest = prefs.window_on(today)
    floor = max(earliest, today + timedelta(days=min_lead_days or prefs.min_lead_days))

    step = max(prefs.departure_step_days, 1)
    nights_options = prefs.nights_options

    windows: list[Window] = []
    seen: set[str] = set()
    day = snap_to_grid(floor, step)
    while day <= latest:
        for nights in nights_options:
            w = Window(day, day + timedelta(days=nights),
                       priority=prefs.is_priority_month(day))
            if w.key not in seen:
                seen.add(w.key)
                windows.append(w)
        day += timedelta(days=step)
    return windows


def estimate_requests(
    prefs: Preferences, *, today: Date | None = None
) -> tuple[int, int]:
    """(total combinations, searches if you scanned everything at once)."""
    windows = generate_windows(prefs, today=today)
    return len(windows), len(windows) * len(prefs.destinations)


# --- coverage rotation ----------------------------------------------------


# A destination that keeps coming back empty is not always a bug to fix. It
# can simply be a route Google will not build: SJO to Osaka returns nothing
# at all today, at any stop count, while SJO to Tokyo returns a full page.
# Searching it every run costs half the budget for nothing, so it drops to an
# occasional probe - and promotes itself back the instant it returns a fare.
DEST_PROBATION_AFTER = 12   # consecutive fruitless searches
DEST_PROBE_EVERY = 8        # ...then price it once every N runs


@dataclass
class RotationState:
    """Where each cold sweep got to, so runs continue rather than restart.

    Priority and general pools advance on separate cursors; otherwise the
    smaller pool would wrap constantly and the larger one would crawl.
    """

    slice_index: int = 0          # general pool cursor
    priority_index: int = 0       # priority-month pool cursor
    slices_total: int = 0
    priority_slices_total: int = 0
    last_window_key: str = ""
    dest_misses: dict = field(default_factory=dict)   # code -> empty runs
    dest_wait: dict = field(default_factory=dict)     # code -> runs until probe

    @classmethod
    def load(cls, path: str | Path) -> "RotationState":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8")

    def active_destinations(
        self, destinations: Sequence[str], *,
        probation_after: int = DEST_PROBATION_AFTER,
    ) -> list[str]:
        """The destinations the window plan should be built around.

        Never returns nothing: if every destination is on probation they are
        all searched, because a run with no searches learns nothing.
        """
        out = [d for d in destinations
               if self.dest_misses.get(d, 0) < probation_after]
        return out or list(destinations)

    def destinations_due_for_probe(
        self, destinations: Sequence[str], *,
        probation_after: int = DEST_PROBATION_AFTER,
    ) -> list[str]:
        """Demoted destinations it is time to re-check, one request each.

        A probe deliberately sits *outside* the window plan. Folding it back
        in would halve the windows per run whenever it fired, and - worse -
        change how many slices the cold pool divides into, which re-maps the
        rotation cursor and turns an orderly sweep into a random walk.
        """
        active = set(self.active_destinations(
            destinations, probation_after=probation_after))
        return [d for d in destinations
                if d not in active and self.dest_wait.get(d, 0) <= 0]

    def record_destinations(
        self, searched: Sequence[str], produced: Iterable[str], *,
        probation_after: int = DEST_PROBATION_AFTER,
        probe_every: int = DEST_PROBE_EVERY,
    ) -> None:
        """Fold this run's yield per destination into the probation state."""
        produced = set(produced)
        for d in searched:
            if d in produced:
                self.dest_misses[d] = 0
                self.dest_wait[d] = 0
            else:
                self.dest_misses[d] = self.dest_misses.get(d, 0) + 1
                if self.dest_misses[d] >= probation_after:
                    self.dest_wait[d] = probe_every
        for d, waited in list(self.dest_wait.items()):
            if d not in searched and waited > 0:
                self.dest_wait[d] = waited - 1

    def advance(self, slices_total: int, priority_slices_total: int = 1) -> None:
        self.slices_total = max(slices_total, 1)
        self.slice_index = (self.slice_index + 1) % self.slices_total
        self.priority_slices_total = max(priority_slices_total, 1)
        self.priority_index = (
            (self.priority_index + 1) % self.priority_slices_total
        )


def take_slice(
    items: Sequence[Window], *, index: int, size: int
) -> tuple[list[Window], int]:
    """The `index`-th chunk of `size` items, and how many chunks exist."""
    if not items or size <= 0:
        return [], 1
    total = (len(items) + size - 1) // size
    start = (index % total) * size
    return list(items[start : start + size]), total


@dataclass
class ScanPlan:
    """What a single run should actually search."""

    hot: list[Window]
    cold: list[Window]
    priority_cold: list[Window]
    destinations: list[str]
    slice_index: int
    slices_total: int
    priority_slices_total: int = 1

    @property
    def windows(self) -> list[Window]:
        seen: set[str] = set()
        out: list[Window] = []
        for w in [*self.hot, *self.priority_cold, *self.cold]:
            if w.key not in seen:
                seen.add(w.key)
                out.append(w)
        return out

    @property
    def request_estimate(self) -> int:
        return len(self.windows) * len(self.destinations)

    def date_pairs(self) -> list[tuple[Date, Date]]:
        return [w.as_pair() for w in self.windows]

    def describe(self) -> str:
        return (
            f"{len(self.hot)} hot + {len(self.priority_cold)} priority + "
            f"{len(self.cold)} general window(s), "
            f"slice {self.slice_index + 1}/{self.slices_total}, "
            f"~{self.request_estimate} searches"
        )


def build_plan(
    prefs: Preferences,
    *,
    hot_keys: Sequence[str] = (),
    rotation: RotationState | None = None,
    request_budget: int = 24,
    hot_share: float = 0.4,
    today: Date | None = None,
    destinations: Sequence[str] | None = None,
) -> ScanPlan:
    """Choose this run's searches inside a request budget.

    Three claims on the budget, in order:

    1. `hot_keys` - windows already known to be cheap. Re-priced every run,
       which is what makes a price drop on your best candidate get caught
       within hours rather than within a rotation cycle.
    2. Priority months - the months the trip owner asked to focus on. They
       get `priority_share` of what is left, so the results actually have
       priority-month candidates to choose from.
    3. Everything else - the remainder, rotating one slice at a time.
    """
    rotation = rotation or RotationState()
    all_windows = generate_windows(prefs, today=today)
    dests = list(destinations if destinations is not None else prefs.destinations)
    n_dest = max(len(dests), 1)

    # Budget is in requests; each window costs one request per destination.
    window_budget = max(request_budget // n_dest, 1)
    hot_budget = max(int(window_budget * hot_share), 1) if hot_keys else 0

    by_key = {w.key: w for w in all_windows}
    hot = [by_key[k] for k in hot_keys if k in by_key][:hot_budget]
    hot_set = {w.key for w in hot}

    remaining = max(window_budget - len(hot), 1)

    pri_pool = [w for w in all_windows if w.priority and w.key not in hot_set]
    gen_pool = [w for w in all_windows if not w.priority and w.key not in hot_set]

    # Split what is left. If one pool is empty its share goes to the other,
    # so budget is never wasted on a pool with nothing in it.
    pri_budget = int(round(remaining * prefs.priority_share)) if pri_pool else 0
    gen_budget = remaining - pri_budget
    if not gen_pool:
        pri_budget, gen_budget = remaining, 0
    elif pri_pool and pri_budget < 1:
        pri_budget, gen_budget = 1, max(remaining - 1, 0)

    priority_cold, pri_slices = take_slice(
        pri_pool, index=rotation.priority_index, size=max(pri_budget, 1)
    ) if pri_pool and pri_budget else ([], 1)

    cold, slices_total = take_slice(
        gen_pool, index=rotation.slice_index, size=max(gen_budget, 1)
    ) if gen_pool and gen_budget else ([], 1)

    return ScanPlan(
        hot=hot,
        cold=cold,
        priority_cold=priority_cold,
        destinations=dests,
        slice_index=rotation.slice_index,
        slices_total=slices_total,
        priority_slices_total=pri_slices,
    )


def coverage_days(slices_total: int, runs_per_day: int) -> float:
    """How long a full pass over the cold space takes, in days."""
    if runs_per_day <= 0:
        return float("inf")
    return slices_total / runs_per_day


def hot_keys_from_history(
    rows: Iterable[dict], *, limit: int = 8
) -> list[str]:
    """The cheapest distinct (depart, return) windows seen so far."""
    best: dict[str, float] = {}
    for rec in rows:
        depart = (rec.get("depart_date") or "").strip()
        back = (rec.get("return_date") or "").strip()
        if not depart or not back:
            continue
        try:
            price = float(rec.get("price_usd") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        key = f"{depart}_{back}"
        if key not in best or price < best[key]:
            best[key] = price
    return [k for k, _ in sorted(best.items(), key=lambda kv: kv[1])[:limit]]
