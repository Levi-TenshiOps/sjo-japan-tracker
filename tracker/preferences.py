"""User preferences: asked once at setup, stored locally, editable any time.

Kept separate from `config.yaml` on purpose. config.yaml is committed and
holds nothing personal, so the repo can be shared or made public as-is.
preferences.json holds the answers to the setup questions and is gitignored.
Secrets (SMTP password) live only in .env, never here.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_PATH = "preferences.json"

MIN_WEEKS = 1
MAX_WEEKS = 12
MIN_SEARCH_MONTHS = 1
MAX_SEARCH_MONTHS = 12

# The priority quota reserves priority_share of a fixed 20 result slots.
# Spread across more than three months that reservation stops meaning
# anything: each month's guaranteed share shrinks below one row, so the
# quota degenerates into the plain cheapest-first list it was meant to
# protect against. One to three months keeps the guarantee real.
MAX_PRIORITY_MONTHS = 3

# There is no reliable maximum-stay rule to encode here, and an earlier
# attempt to encode one was wrong. Sampling on 2026-08-22 looked decisive:
# from several departure dates, 19-30 nights returned 9-18 options each and
# 31 nights upward returned nothing at all, every time. That is exactly the
# shape of a 30-day max-stay fare rule, so it was taken for one.
#
# It is not. The trip owner produced a real Google Flights result for
# SJO-NRT departing 2027-02-05 returning 2027-03-09 - 32 nights - at $1,390
# on Edelweiss plus SWISS via Zurich. Re-querying windows that had returned
# results an hour earlier then returned nothing, so "empty" is not a stable
# property of a query and cannot be used to infer that a fare cannot exist.
#
# The cap is therefore deliberately loose: it exists only to stop a typo
# like 400 weeks from generating a useless grid. Anything a traveller might
# plausibly book must stay searchable.
MAX_STAY_NIGHTS = 60

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def add_months(d: Date, months: int) -> Date:
    """Shift a date by whole months, clamping to the end of short months."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return Date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


class PreferencesError(ValueError):
    """Preferences are missing or malformed."""


@dataclass
class Preferences:
    """Everything the setup wizard asks for."""

    alert_email: str = ""

    # Departure search window. By default this rolls forward every day:
    # anything between `min_lead_days` and `search_months` from now. Pin
    # absolute dates only if you want a fixed window that does not move.
    search_months: int = 8
    min_lead_days: int = 21
    earliest_departure: str = ""     # optional pin, YYYY-MM-DD
    latest_departure: str = ""       # optional pin, YYYY-MM-DD
    departure_step_days: int = 4     # sample every Nth day in the window

    # Months to concentrate on, as numbers (1 = January). Fares in these
    # months are searched harder and are guaranteed a share of the results.
    priority_months: list[int] = field(default_factory=lambda: [1, 2, 3])
    priority_share: float = 0.5      # minimum fraction of results from them
    result_count: int = 20           # how many options the email ranks

    # Calendar months to leave out of the search entirely (1 = January).
    # Every window departing in one of these is never generated, so it costs
    # no request and cannot appear in the email.
    #
    # This exists because the alternative - pinning `earliest_departure` and
    # `latest_departure` to cut a month off the front - silently switches the
    # search window from rolling to fixed (`Preferences.rolling`), so the
    # 8-month horizon would stop moving forward and quietly go stale.
    #
    # Excluding a month is a real reduction in coverage. It is the trip
    # owner's call, not an optimisation to apply on their behalf.
    excluded_months: list[int] = field(default_factory=list)

    # Acceptable trip lengths, in whole weeks. The trip owner picks these
    # once; editing the list later is a one-line change or a re-run of setup.
    trip_weeks: list[int] = field(default_factory=lambda: [2, 3, 4])
    duration_flex_days: int = 0      # +/- days around each week count

    # Trip lengths in nights that are not whole weeks. 30 earns its place:
    # it sits just inside the maximum-stay rule above, and in live sampling
    # it was materially cheaper than 28 nights. Whole weeks alone would step
    # straight over it.
    extra_nights: list[int] = field(default_factory=lambda: [30])

    good_price_usd: int = 1380
    great_price_usd: int = 1150

    # Metro codes, so one request covers every airport in the city. Searching
    # NRT and HND separately costs two requests and finds the same cheapest
    # fare. Osaka is kept in scope even though SJO-OSA currently returns
    # nothing at all from Google; drop it to ["TYO"] to spend the whole
    # budget on Tokyo and double the number of travel windows priced per run.
    destinations: list[str] = field(default_factory=lambda: ["TYO", "OSA"])
    hub_tier: str = "LIGHT"

    version: int = 1

    # -- validation --------------------------------------------------------
    def validate(self) -> None:
        if not self.alert_email or "@" not in self.alert_email:
            raise PreferencesError("alert_email is missing or invalid")
        if not (MIN_SEARCH_MONTHS <= self.search_months <= MAX_SEARCH_MONTHS):
            raise PreferencesError(
                f"search_months must be {MIN_SEARCH_MONTHS}-{MAX_SEARCH_MONTHS}"
            )
        for m in self.priority_months:
            if not (1 <= int(m) <= 12):
                raise PreferencesError(f"{m} is not a month number (1-12)")
        if len(set(self.priority_months)) > MAX_PRIORITY_MONTHS:
            raise PreferencesError(
                f"pick at most {MAX_PRIORITY_MONTHS} priority months "
                f"(got {len(set(self.priority_months))}); more than that and "
                f"each month's reserved share drops below a single result row"
            )
        for m in self.excluded_months:
            if not (1 <= int(m) <= 12):
                raise PreferencesError(f"{m} is not a month number (1-12)")
        clash = set(self.excluded_months) & set(self.priority_months)
        if clash:
            raise PreferencesError(
                f"month(s) {sorted(clash)} are both a priority and excluded; "
                f"a month cannot be searched harder and not at all"
            )
        if len(set(self.excluded_months)) >= 12:
            raise PreferencesError("every month is excluded; nothing to search")
        if not (0.0 <= self.priority_share <= 1.0):
            raise PreferencesError("priority_share must be between 0 and 1")
        if self.result_count < 1:
            raise PreferencesError("result_count must be at least 1")
        early, late = self.window
        if late < early:
            raise PreferencesError(
                f"latest_departure ({late}) is before earliest_departure ({early})"
            )
        if not self.trip_weeks:
            raise PreferencesError("trip_weeks is empty; pick at least one length")
        for w in self.trip_weeks:
            if not (MIN_WEEKS <= int(w) <= MAX_WEEKS):
                raise PreferencesError(
                    f"trip length {w} weeks is outside {MIN_WEEKS}-{MAX_WEEKS}"
                )
        if self.departure_step_days < 1:
            raise PreferencesError("departure_step_days must be at least 1")
        if self.duration_flex_days < 0:
            raise PreferencesError("duration_flex_days cannot be negative")
        if self.great_price_usd > self.good_price_usd:
            raise PreferencesError(
                "great_price_usd must not exceed good_price_usd"
            )

    def window_on(self, today: Date | None = None) -> tuple[Date, Date]:
        """The departure range to search, as of `today`.

        Rolling by default, so the horizon moves forward each day and you
        never silently run out of dates. Pinned only if both absolute dates
        are set.
        """
        today = today or Date.today()
        if self.earliest_departure and self.latest_departure:
            return _parse(self.earliest_departure), _parse(self.latest_departure)
        return (
            today + timedelta(days=self.min_lead_days),
            add_months(today, self.search_months),
        )

    @property
    def window(self) -> tuple[Date, Date]:
        return self.window_on()

    @property
    def is_rolling(self) -> bool:
        return not (self.earliest_departure and self.latest_departure)

    def is_priority_month(self, d: Date) -> bool:
        return d.month in set(self.priority_months)

    def is_excluded_month(self, d: Date) -> bool:
        return d.month in set(self.excluded_months)

    @property
    def excluded_label(self) -> str:
        if not self.excluded_months:
            return "none"
        return ", ".join(MONTH_NAMES[int(m)]
                         for m in sorted(set(self.excluded_months)))

    @property
    def priority_label(self) -> str:
        if not self.priority_months:
            return "none"
        return ", ".join(MONTH_NAMES[int(m)] for m in sorted(self.priority_months))

    @property
    def nights_options(self) -> list[int]:
        """Trip lengths in nights, flex applied, sorted and deduplicated.

        Anything past MAX_STAY_NIGHTS is dropped: no round-trip fare exists
        that long, so those searches return nothing every single time.
        """
        out: set[int] = set()
        for weeks in self.trip_weeks:
            base = int(weeks) * 7
            for delta in range(-self.duration_flex_days, self.duration_flex_days + 1):
                if base + delta > 0:
                    out.add(base + delta)
        out.update(int(n) for n in self.extra_nights if n > 0)
        return sorted(n for n in out if n <= MAX_STAY_NIGHTS)

    @property
    def dropped_nights(self) -> list[int]:
        """Requested trip lengths that no round-trip fare can satisfy."""
        out: set[int] = set()
        for weeks in self.trip_weeks:
            out.add(int(weeks) * 7)
        out.update(int(n) for n in self.extra_nights if n > 0)
        return sorted(n for n in out if n > MAX_STAY_NIGHTS)

    def describe(self, today: Date | None = None) -> str:
        early, late = self.window_on(today)
        weeks = ", ".join(f"{n}n" for n in self.nights_options)
        flex = f" (+/-{self.duration_flex_days}d)" if self.duration_flex_days else ""
        if self.dropped_nights:
            flex += ("  [%s dropped: past the %dn max-stay rule]"
                     % (", ".join(f"{n}n" for n in self.dropped_nights),
                        MAX_STAY_NIGHTS))
        span = (
            f"next {self.search_months} months ({early} to {late})"
            if self.is_rolling else f"{early} to {late} (pinned)"
        )
        pct = int(self.priority_share * 100)
        return (
            f"Depart in the {span}, sampled every {self.departure_step_days}d\n"
            f"  Trip lengths : {weeks}{flex}\n"
            f"  Priority     : {self.priority_label} "
            f"(at least {pct}% of the {self.result_count} results)\n"
            + (f"  Excluded     : {self.excluded_label} "
               f"(never searched, never emailed)\n"
               if self.excluded_months else "")
            + f"  Destinations : {', '.join(self.destinations)}\n"
            f"  Alert under  : ${self.good_price_usd:,} "
            f"(standout ${self.great_price_usd:,})"
        )

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "Preferences":
        p = Path(path)
        if not p.exists():
            raise PreferencesError(
                f"{p} not found. Run:  python setup_tracker.py"
            )
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PreferencesError(f"could not read {p}: {exc}") from exc
        known = {f for f in cls.__dataclass_fields__}
        prefs = cls(**{k: v for k, v in data.items() if k in known})
        prefs.trip_weeks = [int(w) for w in prefs.trip_weeks]
        prefs.priority_months = sorted({int(m) for m in prefs.priority_months})
        prefs.destinations = [str(d).upper() for d in prefs.destinations]
        prefs.validate()
        return prefs

    @classmethod
    def load_or_none(cls, path: str | Path = DEFAULT_PATH) -> "Preferences | None":
        try:
            return cls.load(path)
        except PreferencesError:
            return None

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        self.validate()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


def _parse(value: str) -> Date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise PreferencesError(
            f"bad date {value!r}, expected YYYY-MM-DD"
        ) from exc


def default_window(today: Date | None = None) -> tuple[str, str]:
    """A sensible starting window: roughly 6 to 14 months out."""
    today = today or Date.today()
    return (
        (today + timedelta(days=180)).isoformat(),
        (today + timedelta(days=420)).isoformat(),
    )


def parse_weeks(raw: str) -> list[int]:
    """Accept '2,3,4,5' or '2-5' or '2 3 4 5' and return [2, 3, 4, 5]."""
    text = raw.strip().replace(" ", ",")
    if not text:
        raise PreferencesError("no trip lengths given")
    weeks: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip().rstrip("w").rstrip("W")
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise PreferencesError(f"bad range {chunk!r}") from exc
            if lo > hi:
                lo, hi = hi, lo
            weeks.update(range(lo, hi + 1))
        else:
            try:
                weeks.add(int(chunk))
            except ValueError as exc:
                raise PreferencesError(f"bad trip length {chunk!r}") from exc
    out = sorted(weeks)
    for w in out:
        if not (MIN_WEEKS <= w <= MAX_WEEKS):
            raise PreferencesError(
                f"{w} weeks is outside the supported {MIN_WEEKS}-{MAX_WEEKS}"
            )
    if not out:
        raise PreferencesError("no trip lengths given")
    return out


MONTH_LOOKUP = {name.lower(): num for num, name in MONTH_NAMES.items()}
MONTH_LOOKUP.update({name.lower()[:3]: num for num, name in MONTH_NAMES.items()})


def parse_months(raw: str) -> list[int]:
    """Accept 'January, February, March' or 'jan feb mar' or '1,2,3'."""
    text = raw.strip()
    if not text:
        return []
    parts = [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]
    months: set[int] = set()
    for part in parts:
        key = part.lower()
        if key in MONTH_LOOKUP:
            months.add(MONTH_LOOKUP[key])
            continue
        try:
            num = int(part)
        except ValueError as exc:
            raise PreferencesError(f"{part!r} is not a month") from exc
        if not (1 <= num <= 12):
            raise PreferencesError(f"{num} is not a month number (1-12)")
        months.add(num)
    return sorted(months)
