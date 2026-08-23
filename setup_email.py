#!/usr/bin/env python3
"""Write .env, and nothing else.

`setup_tracker.py` also rewrites `preferences.json`, which by now holds
carefully tuned values - the 21-38 night range, the $1,400 threshold, the
metro destination, the priority months. Re-running the full wizard to fix
one missing password risks all of that for no reason.

This asks for the mail credentials only, verifies them against the server
before saving, and never touches any other file.

    python setup_email.py

You need a Gmail **App Password**, not your normal password: Google blocks
plain-password SMTP. Create one at myaccount.google.com -> Security ->
2-Step Verification -> App passwords. It looks like `abcd efgh ijkl mnop`;
the spaces are cosmetic and are stripped here.
"""

from __future__ import annotations

import getpass
import re
import smtplib
import ssl
import sys
from pathlib import Path

ENV_PATH = Path(".env")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Google's own SMTP endpoints. 587 is STARTTLS, 465 is implicit TLS; the
# tracker picks the right handshake from the port number.
DEFAULTS = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587"}


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    got = input(f"  {prompt}{shown}: ").strip()
    return got or default


def check_login(host: str, port: int, user: str, password: str) -> str | None:
    """None if the credentials work, else a human-readable reason.

    Worth the extra ten seconds: a wrong app password otherwise fails
    silently at 6am inside a scheduled run, and the first symptom is an
    email that never arrives.
    """
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, password)
        return None
    except smtplib.SMTPAuthenticationError:
        return ("the server rejected that username and password. If this is "
                "Gmail, make sure it is an App Password (16 characters) and "
                "not your normal account password.")
    except (smtplib.SMTPException, OSError) as exc:
        return f"could not reach {host}:{port} - {exc}"


def main() -> int:
    print("\n  Mail credentials for the flight tracker.")
    print("  This writes .env only. preferences.json is not touched.\n")

    if ENV_PATH.exists():
        if ask("A .env already exists. Replace it? (y/N)", "n").lower() != "y":
            print("  Left it alone.")
            return 0

    user = ask("Gmail address to send from")
    while not EMAIL_RE.match(user):
        print("     That does not look like an email address.")
        user = ask("Gmail address to send from")

    print("\n  App Password (input is hidden; spaces are fine).")
    password = getpass.getpass("  App Password: ").replace(" ", "")
    while len(password) < 8:
        print("     Too short to be an App Password.")
        password = getpass.getpass("  App Password: ").replace(" ", "")

    host = ask("SMTP host", DEFAULTS["SMTP_HOST"])
    port = int(ask("SMTP port", DEFAULTS["SMTP_PORT"]) or 587)

    print("\n  Checking those credentials against the server...")
    problem = check_login(host, port, user, password)
    if problem:
        print(f"\n  Rejected: {problem}")
        print("  Nothing was written. Fix it and run this again.")
        return 1
    print("  Accepted.")

    ENV_PATH.write_text(
        "\n".join([
            "# Mail credentials only. Gitignored - never commit this.",
            f"SMTP_USER={user}",
            f"SMTP_PASSWORD={password}",
            f"SMTP_HOST={host}",
            f"SMTP_PORT={port}",
        ]) + "\n",
        encoding="utf-8",
    )
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass            # Windows ignores this; the file is gitignored anyway

    print(f"\n  Wrote {ENV_PATH.resolve()}")
    print("\n  Now send yourself one:")
    print("      python -m tracker.cli --save-preview test.html\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Cancelled. Nothing written.")
        sys.exit(1)
