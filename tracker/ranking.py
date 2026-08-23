"""Pick the N cheapest options while guaranteeing priority months a share.

The naive answer — take the 20 cheapest and stop — has a failure mode. If
May and June happen to be cheap this week, all 20 slots fill with May and
June, and the months you actually care about are invisible even though a
perfectly good January fare was found.

So selection runs in two passes:

1. **Reserve.** The cheapest priority-month options claim their guaranteed
   share (default half the slots). If fewer priority options exist than
   slots reserved, the unused reservations are released rather than left
   empty.
2. **Fill.** Remaining slots go to the cheapest of everything left, priority
   or not. A month being non-priority never disqualifies a genuinely cheap
   fare.

The returned list is then sorted by price, so what you read is still
strictly cheapest-first. The quota shapes *which* options make the list,
never the order they appear in.

The reservation only binds when it has to: if the 20 cheapest overall
already contain enough priority months, the result is exactly the 20
cheapest and the quota changes nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from .itinerary import Itinerary


@dataclass
class Selection:
    """The chosen options plus enough detail to explain the choice."""

    items: list[Itinerary]
    priority_count: int
    other_count: int
    reserved_slots: int
    total_candidates: int
    priority_available: int

    @property
    def priority_share(self) -> float:
        return (self.priority_count / len(self.items)) if self.items else 0.0

    @property
    def quota_met(self) -> bool:
        return self.priority_count >= min(self.reserved_slots,
                                          self.priority_available)

    @property
    def quota_was_binding(self) -> bool:
        """True when the quota actually changed the outcome."""
        cheapest = sorted(
            [*self._priority, *self._other],
            key=lambda i: i.price_usd,
        )[: len(self.items)]
        return {id(i) for i in cheapest} != {id(i) for i in self.items}

    _priority: list[Itinerary] = None  # type: ignore[assignment]
    _other: list[Itinerary] = None     # type: ignore[assignment]

    def describe(self) -> str:
        pct = int(round(self.priority_share * 100))
        return (
            f"{len(self.items)} of {self.total_candidates} shown; "
            f"{self.priority_count} from priority months ({pct}%)"
        )


def select_top(
    itineraries: Sequence[Itinerary],
    *,
    count: int = 20,
    is_priority: Callable[[Itinerary], bool] | None = None,
    priority_share: float = 0.5,
) -> Selection:
    """Cheapest `count` options, with priority months guaranteed a share."""
    ranked = sorted(
        itineraries, key=lambda i: (i.price_usd, i.outbound_duration_min)
    )
    if is_priority is None:
        chosen = ranked[:count]
        return Selection(
            items=chosen, priority_count=0, other_count=len(chosen),
            reserved_slots=0, total_candidates=len(ranked),
            priority_available=0, _priority=[], _other=list(ranked),
        )

    priority = [i for i in ranked if is_priority(i)]
    other = [i for i in ranked if not is_priority(i)]

    reserved = min(math.ceil(count * priority_share), count)
    take_priority = min(reserved, len(priority), count)

    chosen: list[Itinerary] = priority[:take_priority]
    chosen_ids = {id(i) for i in chosen}

    # Fill the rest from everything still unchosen, cheapest first. Priority
    # options that missed the reservation compete here on equal terms.
    for itin in ranked:
        if len(chosen) >= count:
            break
        if id(itin) not in chosen_ids:
            chosen.append(itin)
            chosen_ids.add(id(itin))

    chosen.sort(key=lambda i: (i.price_usd, i.outbound_duration_min))
    n_priority = sum(1 for i in chosen if is_priority(i))

    return Selection(
        items=chosen,
        priority_count=n_priority,
        other_count=len(chosen) - n_priority,
        reserved_slots=reserved,
        total_candidates=len(ranked),
        priority_available=len(priority),
        _priority=priority,
        _other=other,
    )


def priority_checker(months: Sequence[int]) -> Callable[[Itinerary], bool]:
    """Predicate matching itineraries that depart in one of `months`."""
    wanted = {int(m) for m in months}
    if not wanted:
        return lambda _itin: False
    return lambda itin: itin.outbound_date.month in wanted
