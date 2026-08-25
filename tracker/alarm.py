"""Tell the trip owner when Google stops answering, and what was done.

The sweep already detects a throttle and rests through it. What it could not
do was *say so*. On 2026-08-23 the address was throttled for most of a day
and the only trace was a warning in a log file nobody was reading; the trip
owner found out by asking. A background process that goes quiet looks
exactly like a background process that is working.

So this raises two emails, and only two:

* **Blocked.** Sent once when a throttle is first confirmed, with what the
  sweep is doing about it. Not once per rest - an escalating backoff can
  cycle for hours and nobody needs six identical emails.
* **Recovered.** Sent once when it clears, with how long it lasted and how
  many windows are queued for a second look, since those are the dates whose
  "no fares" answer cannot be trusted.

State lives in the sweep store, so a restart does not re-send. The whole
thing is best-effort: an alarm that cannot be delivered must never take the
sweep down with it, because the sweep surviving matters more than the
telling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .email_render import EmailContent
from .notify import send_email

log = logging.getLogger(__name__)

# Costa Rica time is what every other date in this project is stated in.
FROM_NAME = "SJO-Japan Flight Tracker"


@dataclass
class AlarmConfig:
    """Just the delivery details, so the sweep need not know about Config."""
    to_addr: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.to_addr and self.smtp_user and self.smtp_password)

    @classmethod
    def from_config(cls, cfg, prefs=None) -> "AlarmConfig":
        """SMTP credentials live in config/.env; the recipient in preferences.

        `cli.py` resolves this with `cfg.alert_email or prefs.alert_email`
        and the sweep did not, so the alarm had nowhere to send: measured
        2026-08-24, `cfg.alert_email` is empty and the real address is in
        `preferences.json`. An alarm that silently has no recipient is worse
        than no alarm, because it looks configured.
        """
        to = (getattr(cfg, "alert_email", "") or ""
              or getattr(prefs, "alert_email", "") or "")
        return cls(to_addr=to,
                   smtp_host=getattr(cfg, "smtp_host", "") or "smtp.gmail.com",
                   smtp_port=int(getattr(cfg, "smtp_port", 587) or 587),
                   smtp_user=getattr(cfg, "smtp_user", "") or "",
                   smtp_password=getattr(cfg, "smtp_password", "") or "")


def _plain(subject: str, lines: list[str]) -> EmailContent:
    """A deliberately plain message. This one is not a digest."""
    text = "\n".join(lines)
    html = ("<div style=\"font:400 14px/1.6 -apple-system,Segoe UI,Roboto,"
            "Helvetica,Arial,sans-serif;color:#202124;\">"
            + "".join(f"<p style=\"margin:0 0 10px;\">{ln}</p>"
                      for ln in lines if ln.strip())
            + "</div>")
    return EmailContent(subject=subject, html=html, text=text)


def blocked_email(*, empty_rate: float, since: str, suspect: int,
                  rest_minutes: float, rest_number: int) -> EmailContent:
    """What the trip owner needs to know, and what not to do about it."""
    return _plain(
        "⚠ Google is throttling the flight tracker",
        [
            "Google has started returning empty pages instead of fares.",
            f"{empty_rate:.0f}% of the last windows checked came back empty "
            f"in under 4.5 seconds. A page Google really has no flights for "
            f"takes about 12 seconds, so this is a refusal rather than a "
            f"quiet date. First seen at {since[11:16]} UTC.",
            "",
            "The sweep has already stopped by itself and is resting for "
            f"{rest_minutes:.0f} minutes (rest #{rest_number}). Each rest "
            "that fails to clear it doubles the next one, up to an hour.",
            "",
            f"{suspect} window(s) checked during the throttle are queued for "
            "a second look. Their 'no fares' answer is not trusted, so no "
            "date gets written off because of this.",
            "",
            "Nothing is needed from you. The one thing that makes it worse "
            "is querying Google to see whether it is still throttled - every "
            "check is another request to a host already refusing.",
            "",
            "You will get one more email when it clears.",
        ])


def recovered_email(*, minutes: float, suspect: int,
                    windows_priced: int) -> EmailContent:
    """The all-clear, with the damage report."""
    return _plain(
        "✓ Google is answering again",
        [
            f"The throttle cleared after {minutes:.0f} minutes.",
            f"The sweep has resumed and has priced {windows_priced:,} "
            "windows in total.",
            "",
            f"{suspect} window(s) are still queued for a second look - dates "
            "checked while Google was refusing, whose empty answer cannot be "
            "trusted. They are re-checked automatically.",
        ])


def sweep_stopped_email(*, hours: float, cursor: str,
                        pending: int) -> EmailContent:
    """The background sweep has stopped pricing windows.

    It is ~80% of everything this project collects and **it does not
    restart itself**. A scheduled run has always noticed - it reads the
    store's `last_active` - but until 2026-08-25 it only wrote a line to
    `tracker.log`, which is the one place nobody looks. The trip owner
    asked the right question: is there an email if the sweeper stops?
    There was not.
    """
    return _plain(
        "⚠ The background flight sweep has stopped",
        [
            f"It has not priced a window in {hours:.1f} hours.",
            "",
            "This is the part that walks every date, so while it is down "
            "you are only getting the handful of windows the scheduled "
            "runs check - a few dozen a day instead of a few hundred. "
            "Your emails will keep arriving and will still be correct; "
            "they will just stop finding anything new.",
            "",
            f"It stopped at {cursor}, with {pending:,} window(s) still "
            "waiting on an answer. Nothing is lost - it resumes exactly "
            "where it left off.",
            "",
            "To restart it:",
            "    python sweep_forever.py",
            "",
            "It does not restart itself, and a reboot only revives it if "
            "the Startup launcher is installed.",
        ])


def parser_broken_email(*, missed: int, windows: int) -> EmailContent:
    """Google's markup changed and fares are being dropped unread.

    This is the failure the README calls "can break without warning", and
    it is far nastier than a block. A block is loud and temporary; this is
    silent and permanent - the pages arrive, the parser cannot read some
    rows, and those fares simply never exist as far as the tracker is
    concerned. It looks exactly like a quiet market.
    """
    return _plain(
        "⚠ Flight results are arriving in a format we cannot read",
        [
            f"{missed:,} result row(s) across {windows:,} window(s) could "
            "not be parsed.",
            "",
            "That number is normally zero. It climbing means Google has "
            "changed the layout of its results page, so real fares are "
            "being dropped without being read - including, possibly, cheap "
            "ones.",
            "",
            "This is not a block. The pages are arriving; we just cannot "
            "understand part of them. Waiting will not fix it.",
            "",
            "What it needs is a look at the parser in tracker/browser.py "
            "against a freshly captured page.",
        ])


def run_blocked_email(*, chrome_blank: int, chrome_attempts: int,
                      grid_rate: float, when: str) -> EmailContent:
    """A scheduled run whose every channel came back empty.

    Distinct from `blocked_email`, which the sweep raises about its own
    traffic. The sweep can be stopped for hours - it was on 2026-08-23 -
    and while it is, nothing watches the six scheduled runs at all. That
    gap is how the 12:26 run on 2026-08-24 went completely dark, both the
    HTTP grid and all nine Chrome launches, and told nobody.
    """
    return _plain(
        "⚠ A scheduled flight-tracker run got nothing from Google",
        [
            f"The {when} run came back empty on every channel it tried.",
            "",
            f"Chrome: {chrome_blank} of {chrome_attempts} launches returned "
            "a page with no flights on it at all - including windows that "
            "are known to hold fares.",
            f"HTTP grid: {grid_rate:.0f}% of its requests came back empty.",
            "",
            "One channel going quiet is ordinary; a date really can have no "
            "fares. Both going quiet in the same run, on windows already "
            "known to be cheap, is Google refusing rather than answering.",
            "",
            "Nothing is needed from you. The next run is in about four "
            "hours and will report if it clears. Do not query Google to "
            "check - every check is another request to a host already "
            "refusing, which is what turned a short throttle into an hour "
            "of one on 2026-08-23.",
            "",
            "Your email for today is not lost: the run still sends what the "
            "background sweep has found.",
        ])


def run_recovered_email(*, when: str, cheapest: str) -> EmailContent:
    """The all-clear for the scheduled runs."""
    return _plain(
        "✓ The scheduled runs are getting fares again",
        [
            f"The {when} run got answers from Google again.",
            f"Cheapest fare it could verify: {cheapest}.",
            "",
            "No action needed; this is the all-clear for the earlier "
            "warning.",
        ])


# Two emails a day means the longest legitimate gap is overnight: the
# evening digest at ~21:30 to the morning run at ~06:45, about nine hours.
# Sixteen hours is comfortably past any normal gap and still catches a
# failure the same day it happens.
SILENCE_HOURS = 16.0


def hours_since_last_email(state_path: str | Path,
                           now: datetime | None = None) -> float | None:
    """How long since an email actually went out. None if never, or unreadable.

    Read from the *state file the scheduled run writes*, not from anything
    the sweep controls. The point is to notice when the other process has
    stopped delivering, which is the one failure the sweep cannot see by
    looking at itself.
    """
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        # Guard against valid JSON that is not an object. Written
        # twenty minutes after fixing exactly this bug elsewhere,
        # and it had it too.
        if not isinstance(data, dict):
            return None
        stamp = data.get("last_email_at") or ""
        if not stamp:
            return None
        seen = datetime.fromisoformat(stamp)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - seen).total_seconds() / 3600.0


def silent_email(*, hours: float, threshold: float) -> EmailContent:
    """The alarm for the product being dead while the sweep looks healthy.

    Written after 2026-08-24, when one malformed CSV line crashed every
    scheduled run four hours before its email phase. The sweep carried on
    perfectly, --status read 15% empty, the coverage invariant reported zero
    orphans - all true, and all about the wrong thing. The trip owner found
    it because the emails stopped.
    """
    return _plain(
        "⚠ No flight email has gone out for %.0f hours" % hours,
        [
            f"The last alert email was sent {hours:.0f} hours ago. Two go out "
            f"a day, so anything past {threshold:.0f} hours means the "
            f"scheduled runs have stopped delivering.",
            "",
            "The background sweep is still running - this warning comes from "
            "it - so the fault is in the scheduled task, not the search.",
            "",
            "Worth checking, in order:",
            "  1. tracker.log - does it stop mid-run, with no error?",
            "  2. Task Scheduler - are FlightTracker1-6 still Ready, and what "
            "is their last result?",
            "  3. python -m tracker.cli --runs-per-day 6 - run one by hand "
            "and read the traceback.",
            "",
            "A crash lands after the last line a run logs, so an empty tail "
            "in tracker.log is the signature rather than an error message.",
        ])


def send(content: EmailContent, cfg: AlarmConfig, *,
         dry_run: bool = False) -> bool:
    """Best effort. A failed alarm must never stop the sweep."""
    if not cfg.usable:
        log.debug("alarm not sent (no SMTP configured): %s", content.subject)
        return False
    try:
        result = send_email(
            content, to_addr=cfg.to_addr, smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port, smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password, from_name=FROM_NAME,
            dry_run=dry_run)
    except Exception as exc:              # noqa: BLE001 - never take the sweep down
        log.warning("could not send the alarm email: %s", exc)
        return False
    if not result.ok:
        log.warning("alarm email not delivered: %s", result.detail)
    else:
        log.info("Alarm email sent: %s", content.subject)
    return bool(result.ok)
