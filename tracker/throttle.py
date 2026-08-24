"""Self-regulating request budget.

Nobody publishes a safe request rate for Google Flights, and any fixed
number would be a guess. So instead of guessing, this measures.

Each run reports how many requests it made and how many came back empty.
A high empty rate is the observable signature of throttling, so the budget
shrinks. Clean runs let it grow back, slowly. The state persists between
runs, which means the tracker converges on whatever rate actually works
from your connection rather than whatever rate seemed reasonable in advance.

Two things worth being explicit about:

* The unit that matters is **requests per day**, not runs per day. Six small
  runs make fewer requests than two large ones. Splitting the same daily
  budget across more runs is strictly better: smaller bursts, more spread
  out, and the cheapest fares get re-checked more often.
* Blocking is not purely a rate problem. A datacenter IP gets flagged on
  reputation regardless of pacing, which is why this project is designed to
  run from a home connection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Requests per run, not per day.
MIN_BUDGET = 8
MAX_BUDGET = 40
START_BUDGET = 24

# An empty result is normal occasionally and damning in bulk.
HEALTHY_EMPTY_RATE = 0.20
BLOCKED_EMPTY_RATE = 0.50

SHRINK_FACTOR = 0.6
GROW_STEP = 2
HISTORY_RUNS = 10


@dataclass
class ThrottleState:
    budget: int = START_BUDGET
    recent: list[dict] = field(default_factory=list)  # newest last
    consecutive_bad: int = 0
    last_run_utc: str = ""
    # Whether the trip owner has already been emailed that the scheduled
    # runs are getting nothing. Kept here rather than in cli.py because it
    # has to survive between runs: six runs a day would otherwise send six
    # identical warnings about one throttle.
    blocked_alarm_sent: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "ThrottleState":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return cls()
        # `json.loads("0")` succeeds and returns an int, so a
        # truncated write - the shape that killed the tracker via
        # sweep_history.csv on 2026-08-24 - gets past the decode and
        # blows up on `.items()`. Valid JSON is not the same as the
        # object this expects.
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    # -- reporting ---------------------------------------------------------
    def record(
        self, *, requests: int, empty: int, now: datetime | None = None
    ) -> str:
        """Log a run and adjust the budget. Returns a human-readable verdict."""
        now = now or datetime.now(timezone.utc)
        self.last_run_utc = now.isoformat(timespec="seconds")

        rate = (empty / requests) if requests else 0.0
        self.recent.append(
            {"at": self.last_run_utc, "requests": requests,
             "empty": empty, "rate": round(rate, 3)}
        )
        self.recent = self.recent[-HISTORY_RUNS:]

        if requests == 0:
            return "no requests made; budget unchanged"

        if rate >= BLOCKED_EMPTY_RATE:
            self.consecutive_bad += 1
            before = self.budget
            self.budget = max(int(self.budget * SHRINK_FACTOR), MIN_BUDGET)
            return (
                f"{rate:.0%} of requests came back empty; likely throttled. "
                f"Budget {before} -> {self.budget}."
            )

        if rate >= HEALTHY_EMPTY_RATE:
            self.consecutive_bad = 0
            return f"{rate:.0%} empty; holding budget at {self.budget}."

        self.consecutive_bad = 0
        if self.budget < MAX_BUDGET:
            before = self.budget
            self.budget = min(self.budget + GROW_STEP, MAX_BUDGET)
            return f"clean run; budget {before} -> {self.budget}."
        return f"clean run; budget already at the {MAX_BUDGET} ceiling."

    # -- inspection --------------------------------------------------------
    @property
    def empty_rate(self) -> float:
        """Empty rate across the retained runs."""
        req = sum(r.get("requests", 0) for r in self.recent)
        emp = sum(r.get("empty", 0) for r in self.recent)
        return (emp / req) if req else 0.0

    @property
    def looks_blocked(self) -> bool:
        """Three bad runs in a row is a pattern, not bad luck."""
        return self.consecutive_bad >= 3

    def advice(self, runs_per_day: int) -> str:
        daily = self.budget * max(runs_per_day, 1)
        if self.looks_blocked:
            return (
                f"Three or more runs in a row came back mostly empty. "
                f"That usually means the source IP is being throttled. If "
                f"this is running in a cloud VM or CI runner, move it to a "
                f"home connection. Currently {self.budget} requests/run "
                f"x {runs_per_day} runs = {daily}/day."
            )
        return (
            f"{self.budget} requests/run x {runs_per_day} runs = "
            f"~{daily} requests/day. Recent empty rate {self.empty_rate:.0%}."
        )


def recommended_runs_per_day(daily_request_budget: int, per_run: int) -> int:
    """How many runs to split a daily budget into.

    More, smaller runs beat fewer, larger ones: the burst is smaller, the
    traffic is spread across the day, and the hot list gets re-priced more
    often. Capped at 6 because past that the marginal value of another check
    is negligible for fares that move daily.
    """
    if per_run <= 0:
        return 1
    return max(1, min(6, daily_request_budget // per_run))
