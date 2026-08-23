"""Decide whether this run has earned the right to send an email.

The rules, in the order they are applied:

1. Nothing at or under the threshold  -> no email, *unless* always_send.
2. Two emails already sent today      -> no email, ever. Hard cap.
3. First email of the day             -> send.
4. Second email of the day            -> only if the price is *materially*
   better than what the first email already told you, and enough time has
   passed. Otherwise the second slot is wasted restating the first.

`always_send` (the daily-digest mode, and the default for this tracker)
changes rules 1 and 4 only. The price threshold stops gating delivery, so
the day's two emails arrive whatever the market did, and the second one no
longer has to prove an improvement to earn its slot. What it does NOT change
is the cap or the reservation: still at most two, and the second is still
held until `last_call_hour` so the day's cheapest fare is what fills it.
A digest that always arrives is only useful if it still carries the best
number of the day.

"Materially better" means a drop of at least MATERIAL_DROP_PCT or
MATERIAL_DROP_USD, whichever is easier to clear. Crossing into the GREAT
tier for the first time also counts, since that is genuinely new news.

The day boundary is midnight in Costa Rica (UTC-6, no DST), not UTC, so
"two per day" matches the day the trip owner is actually living in.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CR_TZ = ZoneInfo("America/Costa_Rica")

MAX_EMAILS_PER_DAY = 2
MATERIAL_DROP_PCT = 0.02   # 2%
MATERIAL_DROP_USD = 25
MIN_HOURS_BETWEEN = 2.5

# The second email is held back until this hour (Costa Rica local) unless a
# standout fare shows up first, so the day's cheapest fare can still claim it.
LAST_CALL_HOUR = 20


@dataclass
class AlertState:
    day: str = ""                      # YYYY-MM-DD, Costa Rica local
    emails_sent_today: int = 0
    last_email_at: str | None = None   # ISO 8601, UTC
    last_best_price: int | None = None
    last_signature: str | None = None
    great_alerted_today: bool = False
    total_emails_sent: int = 0

    @classmethod
    def load(cls, path: str | Path) -> "AlertState":
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
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def roll_day(self, now: datetime) -> None:
        """Reset the daily counters if we have crossed into a new CR day."""
        today = now.astimezone(CR_TZ).date().isoformat()
        if self.day != today:
            self.day = today
            self.emails_sent_today = 0
            self.great_alerted_today = False


@dataclass
class AlertDecision:
    should_send: bool
    reason: str
    is_great: bool = False
    slot: int = 0                   # which email of the day this would be
    remaining_today: int = 0
    notes: list[str] = field(default_factory=list)


def decide(
    state: AlertState,
    *,
    best_price: int | None,
    best_signature: str | None,
    good_threshold: int,
    great_threshold: int,
    now: datetime | None = None,
    min_drop_usd: int = MATERIAL_DROP_USD,
    min_drop_pct: float = MATERIAL_DROP_PCT,
    min_hours_between: float = MIN_HOURS_BETWEEN,
    reserve_last_slot: bool = True,
    last_call_hour: int = LAST_CALL_HOUR,
    always_send: bool = False,
) -> AlertDecision:
    """Pure decision function. Does not mutate `state`.

    The second daily email exists to tell you the price got *better*. It is
    never spent on a fare that is equal to or worse than the one already
    reported, which is what keeps the two-email budget pointed at the
    cheapest thing found rather than at whatever happened to be first.
    """
    now = now or datetime.now(CR_TZ)
    working = AlertState(**asdict(state))
    working.roll_day(now)

    remaining = max(MAX_EMAILS_PER_DAY - working.emails_sent_today, 0)

    if best_price is None:
        return AlertDecision(
            False, "no fares returned this run", remaining_today=remaining
        )

    if best_price > good_threshold and not always_send:
        return AlertDecision(
            False,
            f"cheapest was ${best_price:,}, above the ${good_threshold:,} threshold",
            remaining_today=remaining,
        )

    is_great = best_price <= great_threshold

    if remaining <= 0:
        return AlertDecision(
            False,
            f"daily cap of {MAX_EMAILS_PER_DAY} emails already reached",
            is_great=is_great,
            remaining_today=0,
        )

    slot = working.emails_sent_today + 1

    if working.emails_sent_today == 0:
        return AlertDecision(
            True,
            f"first qualifying fare today at ${best_price:,}",
            is_great=is_great,
            slot=slot,
            remaining_today=remaining,
        )

    # --- this would be the second, and final, email of the day ---
    hours = _hours_since(working.last_email_at, now)
    if hours is not None and hours < min_hours_between:
        return AlertDecision(
            False,
            f"only {hours:.1f}h since the last email (min {min_hours_between}h)",
            is_great=is_great,
            remaining_today=remaining,
        )

    # A standout fare is worth the last slot immediately: it may not survive
    # until the evening.
    newly_great = is_great and not working.great_alerted_today
    if newly_great:
        return AlertDecision(
            True,
            f"first standout fare today at ${best_price:,}",
            is_great=True,
            slot=slot,
            remaining_today=remaining,
        )

    prev = working.last_best_price
    if prev is None:
        return AlertDecision(
            True,
            f"qualifying fare at ${best_price:,}",
            is_great=is_great,
            slot=slot,
            remaining_today=remaining,
        )

    drop = prev - best_price
    improved = drop >= min_drop_usd or drop >= prev * min_drop_pct

    # In digest mode the second email is owed to the trip owner whatever the
    # market did, so "no improvement" stops being a reason to stay silent. It
    # still has to wait for last call below, so what finally goes out is the
    # day's cheapest, not merely its most recent.
    if not improved and not always_send:
        return AlertDecision(
            False,
            (
                f"${best_price:,} is not materially better than the ${prev:,} "
                f"already emailed today"
            ),
            is_great=is_great,
            remaining_today=remaining,
        )

    # The fare improved, but spending the last slot now means a cheaper find
    # later today could not be reported at all. So the slot is held back for
    # either a standout fare or the final run of the day, whichever comes
    # first. This is what keeps the two-email budget aimed at the cheapest
    # fare rather than the earliest one.
    local_hour = now.astimezone(CR_TZ).hour
    if reserve_last_slot and local_hour < last_call_hour:
        return AlertDecision(
            False,
            (
                f"${best_price:,} beats the ${prev:,} already sent, but the "
                f"last email slot is being held until {last_call_hour}:00 in "
                f"case something cheaper appears"
            ),
            is_great=is_great,
            remaining_today=remaining,
            notes=["held"],
        )

    if drop > 0:
        reason = f"best of the day at ${best_price:,}, down ${drop:,} from ${prev:,}"
    elif drop == 0:
        reason = f"end-of-day digest; ${best_price:,} held all day"
    else:
        reason = (
            f"end-of-day digest; ${best_price:,} is ${-drop:,} above the "
            f"${prev:,} reported earlier"
        )
    return AlertDecision(
        True,
        reason,
        is_great=is_great,
        slot=slot,
        remaining_today=remaining,
    )


def record_sent(
    state: AlertState,
    *,
    best_price: int,
    best_signature: str | None,
    is_great: bool,
    now: datetime | None = None,
) -> AlertState:
    """Commit a send to the state. Returns the same (mutated) object."""
    now = now or datetime.now(CR_TZ)
    state.roll_day(now)
    state.emails_sent_today += 1
    state.total_emails_sent += 1
    state.last_email_at = now.astimezone(ZoneInfo("UTC")).isoformat()
    state.last_best_price = best_price
    state.last_signature = best_signature
    if is_great:
        state.great_alerted_today = True
    return state


def _hours_since(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=ZoneInfo("UTC"))
    return (now - then).total_seconds() / 3600
