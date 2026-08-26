"""Configuration: YAML file for what to search, env vars for secrets.

Secrets (SMTP password, recipient address) never live in the YAML, so the
repo stays safe to flip public later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import yaml

from .airports import BANNED_AIRPORTS, JAPAN_DESTINATIONS, usable_hubs

DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigError(ValueError):
    """Configuration is malformed or unsafe."""


@dataclass
class Config:
    origins: list[str] = field(default_factory=lambda: ["SJO"])
    destinations: list[str] = field(
        default_factory=lambda: list(JAPAN_DESTINATIONS))
    date_pairs: list[tuple[Date, Date | None]] = field(default_factory=list)
    hubs: list[str] = field(default_factory=list)
    hub_tier: str = "LIGHT"

    good_price_usd: int = 1380
    great_price_usd: int = 1150
    max_stops: int = 2
    # The HTTP grid cannot see stays longer than this; the server-rendered
    # HTML carries no prices past ~30 nights. Measured over all history:
    # 509 grid fares at <=30 nights, zero at >=31. Chrome is unaffected and
    # still prices them. 0 disables the skip.
    http_max_nights: int = 30
    # Finish these departure months before spending anything on the rest.
    # Empty = no focus. It redirects the sweep's effort; it never raises
    # the request rate. The cold rotation is frozen while a focus is on and
    # resumes exactly where it stopped.
    sweep_focus_months: list[int] = field(default_factory=list)
    max_total_hours: int | None = 60
    min_layover_min: int = 75

    # scanning
    deep_hub_sweep: bool = True
    # Requests held back from the window plan so the hub sweep can actually
    # run. Without a reserve the broad sweep spends the whole budget first
    # and the sweep is silently skipped on every run.
    hub_sweep_requests: int = 4
    # A hard ceiling on one run's requests, applied on top of whatever the
    # adaptive throttle worked out. Read by cli.py since 2026-08-23; before
    # that it was read by nothing.
    max_requests_per_run: int = 90
    request_delay_seconds: float = 3.0

    # delivery
    alert_email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_name: str = "SJO-Japan Flight Tracker"
    ntfy_topic: str = ""

    # coverage strategy
    # The cheapest windows re-priced through Chrome on every run. This is
    # what guarantees the top of the email is a *live* price rather than
    # one the sweep happened to see earlier - anything outside it depends
    # on the sweep having touched it within sweep_max_age_hours, and drops
    # off the email when it has not.
    #
    # Raised 8 -> 18 on 2026-08-25, after the focus finished and the
    # cheapest fare of all ($1,347) was found to be 25.5 h old and about to
    # be dropped from the evening email. Eight covered the top eight; the
    # rows below that were dependent on luck.
    #
    # It costs almost nothing. `chrome_max_per_run` is 20 and only 5-13 of
    # it was being spent, so this fills a budget that already existed:
    # ~66 Chrome launches a day against a ceiling of 120, and against
    # ~1,600 requests a day from the sweep.
    hot_list_size: int = 18
    hot_share: float = 0.5       # fraction of the budget spent on them

    # The wide net: one plain-text query per month, asking Google for its own
    # cheapest dates. Costs one request per month in the window and finds
    # far cheaper fares than the grid reaches in a fortnight, so it is on by
    # default. `monthly_scan_destination` is a single airport on purpose -
    # the text query takes a place name, not a metro code list.
    monthly_scan: bool = True
    monthly_scan_destination: str = "NRT"   # text query wants one airport
    # Also ask about each half-month. Google answers one recommendation per
    # query, so a narrower range can name a window the month query never
    # mentions. Costs two extra HTTP requests per month.
    monthly_scan_halves: bool = True

    # Chrome verification. Plain HTTP does not just miss long stays: on
    # 2027-01-29 -> 2027-02-25 it reported a cheapest of $1,635 while the
    # real cheapest was $1,347 on Edelweiss/SWISS via Zurich - a routing
    # absent from the server-rendered HTML entirely. So every window we
    # actually care about gets re-priced through Chrome, which sees it.
    # A launch measures ~13s in practice, so 20 costs about four minutes
    # of a run. Worth it: across four windows HTTP's cheapest visa-free
    # fare was $2,509/$2,866/$3,057/$3,179 where Chrome found
    # $1,347/$1,432/$2,688/$3,093. HTTP lost every time.
    # Chrome queries the metro code, not a single airport: TYO returns both
    # Narita and Haneda for one launch, and measured on 2027-01-29 it gave
    # 15 options against NRT's 13 while still finding the same $1,347. The
    # text-query wide net cannot do this - "to HND" returns no hint at all -
    # which is why the two settings differ.
    chrome_destination: str = "TYO"
    chrome_verify: bool = True
    chrome_path: str = ""              # blank = autodetect
    chrome_max_per_run: int = 20
    # Do not re-verify a window the background sweep priced this recently.
    # Its price is already folded into the email with a "checked N hr ago"
    # label, and a Chrome launch is the most expensive request made here.
    # 0 disables the skip and verifies everything, as before.
    chrome_skip_if_swept_hours: float = 3.0
    chrome_timeout_s: int = 120
    chrome_budget_ms: int = 30000

    # alert sensitivity
    min_drop_usd: int = 25
    min_drop_pct: float = 0.02
    reserve_last_slot: bool = True   # hold email 2 for the day's cheapest
    last_call_hour: int = 20         # ...until this hour, Costa Rica time

    # Send both daily emails whatever the price did, instead of only when a
    # fare clears good_price_usd. The threshold still drives the headline,
    # the row highlighting and the subject line - it just no longer decides
    # whether anything is sent at all. The two-per-day cap is unaffected.
    daily_digest: bool = True

    # files
    history_csv: str = "price_history.csv"
    state_file: str = "state.json"
    throttle_file: str = "throttle.json"
    rotation_file: str = "rotation.json"
    # Written by sweep_forever.py, read here. Missing is fine - it just
    # means the background sweep is not running.
    sweep_store: str = "discoveries.json"
    # The sweep logs its observations to its own CSV rather than sharing
    # price_history.csv. Both processes append, and two writers on one file
    # can interleave a line; separate files cost nothing and the baseline
    # simply reads both.
    sweep_history_csv: str = "sweep_history.csv"
    # What the wide net said, month by month, kept across runs. The sweep
    # walks the priority months first, so five of the eight months had no
    # price data at all while the first pass was still running - yet the
    # wide net had been asking about every one of them six times a day and
    # discarding the answers.
    month_ledger: str = "month_hints.json"
    # The wide net used to fire all 24 probes back to back - the
    # burstiest, most machine-shaped traffic in the project, on the one
    # path that runs six times a day. Paced and jittered now.
    monthly_scan_delay_seconds: float = 3.0
    # One process queries Google at a time. The sweep runs continuously and
    # the scheduled tracker six times a day, so without this they overlap
    # roughly every four hours - and the symptom is silence, not an error.
    google_lock: str = "google.lock"

    # The HTTP grid has never found a fare that could trigger an alert -
    # 0 of 70 windows at or under the threshold, best ever $1,635 against
    # Chrome's $1,347 - because the cheap European routings are absent from
    # the server-rendered HTML. Its *coverage* role is therefore entirely
    # redundant now the sweep prices every window through Chrome.
    #
    # It is not removed, for two reasons. It is the only thing that keeps an
    # email going out when the sweep is down, and its rows are real
    # bookable fares that give the market some context even if they are
    # never the cheapest. So it is cut back to roughly what filling the
    # email table needs, and only when the sweep is actually feeding us.
    grid_budget_when_swept: int = 16
    swept_enough: int = 25          # fresh findings that count as "sweep is working"
    # How stale a swept price may be before the email stops showing it. The
    # store keeps findings for longer so the sweep can tell what it has
    # already seen, but a price is a snapshot: presenting a day-old number
    # beside a just-checked one, with no way to tell them apart, would make
    # the email lie by omission.
    sweep_max_age_hours: float = 10.0
    dashboard_url: str = ""

    # -- validation -------------------------------------------------------
    def validate(self) -> None:
        if not self.origins:
            raise ConfigError("origins is empty")
        if not self.destinations:
            raise ConfigError("destinations is empty")
        if self.great_price_usd > self.good_price_usd:
            raise ConfigError(
                f"great_price_usd ({self.great_price_usd}) must not exceed "
                f"good_price_usd ({self.good_price_usd})"
            )
        for code in [*self.origins, *self.destinations, *self.hubs]:
            if code.upper() in BANNED_AIRPORTS:
                raise ConfigError(
                    f"{code} is on the visa deny list and cannot be used"
                )
        for out, back in self.date_pairs:
            # Round-trip only, enforced here rather than trusted. A bare
            # date in config.yaml parses to (depart, None), and
            # `search.build_query` turns a None return into trip="one-way"
            # without comment. A one-way fare is roughly half the price of
            # the round trip it would be compared against, so it would not
            # look like a bug in the email - it would look like the best
            # deal ever found.
            if back is None:
                raise ConfigError(
                    f"date_pairs entry {out} has no return date. This tracker "
                    f"is round-trip only; a one-way fare would be listed "
                    f"beside round trips at about half the price. Write it as "
                    f"[{out}, <return date>]."
                )
            if back <= out:
                raise ConfigError(f"return {back} is not after departure {out}")
        if self.max_stops < 0 or self.max_stops > 3:
            raise ConfigError("max_stops must be between 0 and 3")

    @property
    def smtp_configured(self) -> bool:
        return bool(self.alert_email and self.smtp_user and self.smtp_password)


def _parse_date(value: Any) -> Date:
    if isinstance(value, Date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(f"bad date {value!r}, expected YYYY-MM-DD") from exc


def _parse_date_pairs(raw: Any) -> list[tuple[Date, Date | None]]:
    if not raw:
        return []
    pairs: list[tuple[Date, Date | None]] = []
    for item in raw:
        if isinstance(item, dict):
            out = _parse_date(item.get("depart") or item.get("out"))
            back_raw = item.get("return") or item.get("back")
            back = _parse_date(back_raw) if back_raw else None
        elif isinstance(item, (list, tuple)):
            out = _parse_date(item[0])
            back = _parse_date(item[1]) if len(item) > 1 and item[1] else None
        else:
            out, back = _parse_date(item), None
        pairs.append((out, back))
    return pairs


DEFAULT_ENV_PATH = ".env"


def load_dotenv(path: str | Path = DEFAULT_ENV_PATH) -> int:
    """Read KEY=VALUE lines from `.env` into the environment.

    This was missing entirely. `setup_tracker.py` wrote a .env, the README
    and .env.example documented it, and nothing ever read it - `_env` only
    ever consulted os.environ. So every run ended at "SMTP credentials
    missing" no matter how correct the file was, which is a silent failure
    of exactly the kind that looks like a broken password.

    A real environment variable always wins over the file, so a scheduled
    job or a shell export can still override it. Returns how many names
    were set, which the caller logs.

    Deliberately hand-rolled rather than adding python-dotenv: it is
    fifteen lines and this project takes no runtime dependency without a
    reason.
    """
    p = Path(path)
    if not p.exists():
        return 0
    set_count = 0
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if not key or key in os.environ:
            continue            # a real env var outranks the file
        value = value.strip()
        # Strip one matching pair of surrounding quotes, and any trailing
        # `# comment` on an unquoted value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        os.environ[key] = value
        set_count += 1
    return set_count


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load(path: str | Path = DEFAULT_CONFIG_PATH, *, use_env: bool = True) -> Config:
    """Load YAML config, then overlay environment variables."""
    data: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        # Malformed YAML must stop the run - a config nobody can read is a
        # real problem the trip owner has to fix - but it must say so as a
        # ConfigError, not as a raw yaml.ParserError from three frames down.
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, ValueError) as exc:
            raise ConfigError(f"{path} is not readable YAML: {exc}") from exc
        if loaded and not isinstance(loaded, dict):
            raise ConfigError(
                f"{path} must contain a YAML mapping, found "
                f"{type(loaded).__name__}. It may have been truncated.")
        data = loaded or {}

    cfg = Config()
    simple = {
        f
        for f in Config.__dataclass_fields__
        if f not in {"date_pairs", "origins", "destinations", "hubs"}
    }
    for key in simple:
        if key in data and data[key] is not None:
            setattr(cfg, key, data[key])

    for key in ("origins", "destinations", "hubs"):
        if data.get(key):
            setattr(cfg, key, [str(x).upper().strip() for x in data[key]])

    cfg.date_pairs = _parse_date_pairs(data.get("date_pairs"))

    if not cfg.hubs:
        cfg.hubs = [h.code for h in usable_hubs(cfg.hub_tier)]  # type: ignore[arg-type]

    if use_env:
        load_dotenv()
        cfg.alert_email = _env("ALERT_EMAIL", cfg.alert_email)
        cfg.smtp_host = _env("SMTP_HOST", cfg.smtp_host)
        cfg.smtp_port = int(_env("SMTP_PORT", str(cfg.smtp_port)) or cfg.smtp_port)
        cfg.smtp_user = _env("SMTP_USER", cfg.smtp_user)
        cfg.smtp_password = _env("SMTP_PASSWORD", cfg.smtp_password)
        cfg.ntfy_topic = _env("NTFY_TOPIC", cfg.ntfy_topic)
        if _env("GOOD_PRICE_USD"):
            cfg.good_price_usd = int(_env("GOOD_PRICE_USD"))
        if _env("GREAT_PRICE_USD"):
            cfg.great_price_usd = int(_env("GREAT_PRICE_USD"))

    cfg.validate()
    return cfg


def upcoming_pairs(
    pairs: Sequence[tuple[Date, Date | None]], today: Date
) -> list[tuple[Date, Date | None]]:
    """Drop windows that have already departed."""
    return [(o, b) for o, b in pairs if o > today]
