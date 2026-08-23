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
    max_total_hours: int | None = 60
    min_layover_min: int = 75

    # scanning
    broad_sweep: bool = True
    deep_hub_sweep: bool = True
    deep_sweep_top_windows: int = 3
    # Requests held back from the window plan so the hub sweep can actually
    # run. Without a reserve the broad sweep spends the whole budget first
    # and the sweep is silently skipped on every run.
    hub_sweep_requests: int = 4
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
    hot_list_size: int = 8       # cheapest windows re-priced every run
    hot_share: float = 0.5       # fraction of the budget spent on them

    # The wide net: one plain-text query per month, asking Google for its own
    # cheapest dates. Costs one request per month in the window and finds
    # far cheaper fares than the grid reaches in a fortnight, so it is on by
    # default. `monthly_scan_destination` is a single airport on purpose -
    # the text query takes a place name, not a metro code list.
    monthly_scan: bool = True
    monthly_scan_destination: str = "NRT"

    # Chrome verification. Plain HTTP does not just miss long stays: on
    # 2027-01-29 -> 2027-02-25 it reported a cheapest of $1,635 while the
    # real cheapest was $1,347 on Edelweiss/SWISS via Zurich - a routing
    # absent from the server-rendered HTML entirely. So every window we
    # actually care about gets re-priced through Chrome, which sees it.
    # A launch costs ~25s against ~3s, hence a small explicit budget.
    chrome_verify: bool = True
    chrome_path: str = ""              # blank = autodetect
    chrome_max_per_run: int = 10
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
            if back is not None and back <= out:
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


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load(path: str | Path = DEFAULT_CONFIG_PATH, *, use_env: bool = True) -> Config:
    """Load YAML config, then overlay environment variables."""
    data: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if loaded and not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping")
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
