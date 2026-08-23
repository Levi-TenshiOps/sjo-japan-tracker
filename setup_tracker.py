#!/usr/bin/env python3
"""One-time setup. Asks for the alert email, the travel window and the trip
lengths you would accept, then writes preferences.json and .env.

Nothing personal ends up in a tracked file, so the repository can be shared
or published exactly as it is. Re-run this any time to change your answers,
or edit preferences.json directly.
"""

from __future__ import annotations

import getpass
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tracker.preferences import (  # noqa: E402
    MAX_PRIORITY_MONTHS, MONTH_NAMES, Preferences, PreferencesError,
    parse_months, parse_weeks,
)
from tracker.schedule import coverage_days, estimate_requests  # noqa: E402

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
ENV_PATH = Path(".env")
PREFS_PATH = Path("preferences.json")


def ask(prompt: str, default: str = "", *, secret: bool = False,
        allow_blank: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        value = raw or default
        if value or allow_blank:
            return value
        print("  Required.")


def ask_email(prompt: str, default: str = "") -> str:
    while True:
        value = ask(prompt, default)
        if EMAIL_RE.match(value):
            return value
        print(f"  '{value}' does not look like an email address.")


def ask_date(prompt: str, default: str) -> str:
    while True:
        value = ask(prompt, default)
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError:
            print("  Use YYYY-MM-DD, e.g. 2027-01-15.")


def ask_weeks(prompt: str, default: str) -> list[int]:
    while True:
        try:
            return parse_weeks(ask(prompt, default))
        except PreferencesError as exc:
            print(f"  {exc}")


def ask_months(prompt: str, default: str, *, limit: int | None = None) -> list[int]:
    """A list of month numbers. `limit` caps how many may be picked.

    The cap belongs to the *priority* list, where more than three months
    drops each month's reserved share below one result row. The list of
    months to search has no such limit - one or twelve are both sensible.
    """
    while True:
        try:
            months = parse_months(ask(prompt, default, allow_blank=True))
        except PreferencesError as exc:
            print(f"     {exc}")
            continue
        if limit is not None and len(set(months)) > limit:
            print(f"     Pick at most {limit} months - beyond "
                  f"that the reserved share is under one result row each.")
            continue
        return months


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        try:
            value = int(ask(prompt, str(default)))
        except ValueError:
            print("  Enter a whole number.")
            continue
        if lo <= value <= hi:
            return value
        print(f"  Must be between {lo} and {hi}.")


def main() -> int:
    print("\n  San Jose -> Japan flight tracker")
    print("  One-time setup. Re-run any time to change your answers.")
    print("  " + "-" * 54)

    existing = Preferences.load_or_none(PREFS_PATH)
    if existing:
        print(f"\n  Current settings: {existing.describe()}")
        if input("\n  Keep these and only redo mail settings? [y/N]: ").strip().lower() == "y":
            return _mail_only(existing)

    prefs = existing or Preferences()

    # -- 1. email ----------------------------------------------------------
    print("\n  1. Where should price alerts go?")
    prefs.alert_email = ask_email("     Alert email", prefs.alert_email)

    # -- 2. how far ahead to look -------------------------------------------
    print("\n  2. How far ahead should it look?")
    print("     The window rolls forward every day, so it never runs dry.")
    prefs.search_months = ask_int(
        "     Search the next N months", prefs.search_months, 1, 12)
    prefs.min_lead_days = ask_int(
        "     Ignore departures sooner than N days", prefs.min_lead_days, 0, 120)

    print("\n     Which months should it actually search?")
    print("     Leave blank for every month in that horizon. Name months and")
    print("     ONLY those are searched - the horizon above then just decides")
    print("     which year each one falls in.")
    print("     Any number of months: 'October, November, December' or '1,2,3'.")
    prefs.included_months = ask_months(
        "     Months to search",
        ",".join(MONTH_NAMES[m] for m in prefs.included_months))
    if prefs.included_months:
        missed = prefs.unreachable_months()
        if missed:
            print(f"     NOTE: {', '.join(MONTH_NAMES[m] for m in missed)} "
                  f"is outside a {prefs.search_months}-month horizon and would")
            print("     search nothing at all. Raise the horizon or drop it.")

    # -- 3. priority months ---------------------------------------------------
    print("\n  3. Any months to focus on?")
    print("     These are searched harder and are guaranteed a share of the")
    print("     results, so a cheap month elsewhere cannot crowd them out.")
    print("     Accepts 'January, February, March' or 'jan feb mar' or '1,2,3'.")
    print("     Pick 1 to 3 months, or blank for no preference.")
    if prefs.included_months:
        # Priority is a subset of what is searched, so say so here rather
        # than failing validation after every other question is answered.
        names = ", ".join(MONTH_NAMES[m] for m in sorted(prefs.included_months))
        print(f"     Must be among the months you chose to search: {names}.")
    while True:
        prefs.priority_months = ask_months(
            "     Priority months",
            ",".join(MONTH_NAMES[m] for m in prefs.priority_months),
            limit=MAX_PRIORITY_MONTHS)
        if not prefs.included_months:
            break
        orphan = set(prefs.priority_months) - set(prefs.included_months)
        if not orphan:
            break
        print(f"     {', '.join(MONTH_NAMES[m] for m in sorted(orphan))} "
              f"is not being searched, so it cannot be a priority.")
    if prefs.priority_months:
        pct = ask_int("     Minimum % of results from those months",
                      int(prefs.priority_share * 100), 0, 100)
        prefs.priority_share = pct / 100

    # -- 4. trip lengths ---------------------------------------------------
    print("\n  4. How long could the trip be? Whole weeks.")
    print("     Accepts a list (2,3,4,5), a range (2-5), or one value (3).")
    prefs.trip_weeks = ask_weeks(
        "     Trip lengths in weeks",
        ",".join(str(w) for w in prefs.trip_weeks) or "2,3,4,5")

    print("\n     Also check a few days either side of each length?")
    print("     Shifting by a day or two often finds a cheaper fare, but it")
    print("     multiplies the number of date combinations to scan.")
    prefs.duration_flex_days = ask_int(
        "     Flex in days (0 for exact weeks)", prefs.duration_flex_days, 0, 5)

    print("\n     How finely should departures be sampled inside the window?")
    print("     Every 3 days is a good balance; every 1 day is thorough but slow.")
    prefs.departure_step_days = ask_int(
        "     Sample every N days", prefs.departure_step_days or 3, 1, 14)

    # -- 5. threshold ------------------------------------------------------
    print("\n  5. Price thresholds, USD, round trip, one adult, economy.")
    print("     Google's own data puts the usual booking price near $1,329,")
    print("     so a $1,380 threshold will fire on fairly ordinary fares.")
    prefs.good_price_usd = ask_int(
        "     Email me under", prefs.good_price_usd, 200, 20_000)
    prefs.great_price_usd = ask_int(
        "     Treat as a standout under",
        min(prefs.great_price_usd, prefs.good_price_usd), 200, prefs.good_price_usd)
    prefs.result_count = ask_int(
        "     How many options should each email rank", prefs.result_count, 5, 40)

    try:
        prefs.validate()
    except PreferencesError as exc:
        print(f"\n  Those answers do not work together: {exc}")
        return 1
    prefs.save(PREFS_PATH)
    print(f"\n  Wrote {PREFS_PATH}")

    _report_coverage(prefs)

    return _mail_only(prefs, first_time=True)


def _report_coverage(prefs: Preferences, runs_per_day: int = 4,
                     per_run: int = 24) -> None:
    """Explain how long a complete pass will take, and warn if it is slow."""
    from tracker.schedule import build_plan

    combos, full = estimate_requests(prefs)
    plan = build_plan(prefs, request_budget=per_run)
    gen = coverage_days(plan.slices_total, runs_per_day)
    pri = coverage_days(plan.priority_slices_total, runs_per_day)

    print(f"\n  {combos} date combinations, {full} searches for a complete pass.")
    print(f"  At {runs_per_day} runs a day of about {per_run} searches each:")
    if prefs.priority_months:
        print(f"    priority months revisited every {pri:.1f} day(s)")
    print(f"    everything else revisited every {gen:.1f} day(s)")
    print("  The cheapest options found so far are re-checked every run.")

    if gen > 14:
        suggested = min(prefs.departure_step_days + 2, 14)
        print(f"\n  That is slow. Sampling every {suggested} days instead of "
              f"{prefs.departure_step_days} would roughly halve it;")
        print("  edit departure_step_days in preferences.json to change it.")


def _mail_only(prefs: Preferences, *, first_time: bool = False) -> int:
    print("\n  6. Which mailbox should send the alerts?")
    print("     Gmail needs a 16-character App Password, not your normal one:")
    print("     Google Account -> Security -> 2-Step Verification -> App passwords.")
    print("     The same address you used above is fine.")

    if ENV_PATH.exists() and not first_time:
        if input("\n     .env exists. Overwrite? [y/N]: ").strip().lower() != "y":
            print("     Left unchanged.")
            return 0

    smtp_user = ask_email("     Sending address", prefs.alert_email)
    smtp_password = ask("     App password", secret=True).replace(" ", "")

    host, port = "smtp.gmail.com", "587"
    if not smtp_user.endswith(("@gmail.com", "@googlemail.com")):
        host = ask("     SMTP host", host)
        port = ask("     SMTP port", port)

    print("\n  7. Optional phone push via ntfy.sh. Blank to skip.")
    print("     Topics are public by name, so pick something unguessable.")
    ntfy = ask("     ntfy topic", allow_blank=True)

    lines = [
        "# Local secrets. Never commit this file.",
        f"SMTP_USER={smtp_user}",
        f"SMTP_PASSWORD={smtp_password}",
        f"SMTP_HOST={host}",
        f"SMTP_PORT={port}",
    ]
    if ntfy:
        lines.append(f"NTFY_TOPIC={ntfy}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    print(f"\n  Wrote {ENV_PATH} (permissions 600, gitignored).")

    print("\n  Next:")
    print("    python -m tracker.cli --status")
    print("    python -m tracker.cli --dry-run --save-preview email.html")
    print("    python install_schedule.py        # run it automatically")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled. Nothing was written.")
        sys.exit(130)
