"""Delivery: SMTP for the real email, ntfy for an optional phone push."""

from __future__ import annotations

import logging
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .email_render import EmailContent

log = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    ok: bool
    detail: str


def build_message(
    content: EmailContent, *, to_addr: str, from_addr: str, from_name: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = content.subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_addr
    msg["Message-ID"] = make_msgid(domain="flight-tracker.local")
    msg["X-Auto-Response-Suppress"] = "All"
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(content.text)
    msg.add_alternative(content.html, subtype="html")
    return msg


def send_email(
    content: EmailContent,
    *,
    to_addr: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_name: str = "Flight Tracker",
    timeout: int = 30,
    dry_run: bool = False,
) -> DeliveryResult:
    if not to_addr:
        return DeliveryResult(False, "no recipient configured")
    if not (smtp_user and smtp_password):
        return DeliveryResult(False, "SMTP credentials missing")

    msg = build_message(
        content, to_addr=to_addr, from_addr=smtp_user, from_name=from_name
    )
    if dry_run:
        return DeliveryResult(True, f"dry run: would email {to_addr}")

    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=timeout) as s:
                s.login(smtp_user, smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(smtp_user, smtp_password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return DeliveryResult(
            False,
            "SMTP auth rejected. For Gmail use a 16-character App Password, "
            "not the account password.",
        )
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(False, f"{type(exc).__name__}: {exc}")
    return DeliveryResult(True, f"emailed {to_addr}")


def send_push(topic: str, *, title: str, body: str, url: str = "",
              dry_run: bool = False) -> DeliveryResult:
    """Optional ntfy.sh push. Never carries anything sensitive."""
    if not topic:
        return DeliveryResult(False, "no ntfy topic configured")
    if dry_run:
        return DeliveryResult(True, f"dry run: would push to {topic}")
    headers = {"Title": title, "Priority": "default", "Tags": "airplane"}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                return DeliveryResult(False, f"ntfy returned {resp.status}")
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(False, f"{type(exc).__name__}: {exc}")
    return DeliveryResult(True, f"pushed to {topic}")
