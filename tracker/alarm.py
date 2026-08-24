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

import logging
from dataclasses import dataclass

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
            f"Empty rate is {empty_rate:.0f}% over the last windows checked "
            f"(13% is normal). First seen at {since[11:16]} UTC.",
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
