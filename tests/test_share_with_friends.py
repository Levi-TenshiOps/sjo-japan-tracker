"""Friends get the fare emails. They never get the alarms.

Asked for 2026-08-27: let one or more friends receive the same deal
emails. The whole risk is the second sentence. "The background sweep has
stopped", "Google has started returning empty pages", "results are
arriving in a format we cannot read" are operational messages addressed
to whoever maintains this - sending them to a friend is confusing at best
and alarming at worst, and this project has already sent three false ones.

The separation is structural rather than remembered: `bcc` is an opt-in
parameter of `send_email`, and `alarm.send` simply does not pass it. To
leak an alarm to a friend somebody would have to go and add the argument.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import alarm, cli, notify
from tracker.preferences import Preferences, PreferencesError


def prefs(**kw):
    base = dict(alert_email="me@example.com", search_months=8,
                min_lead_days=21, departure_step_days=1, trip_weeks=[3],
                extra_nights=[], destinations=["TYO"], priority_months=[])
    base.update(kw)
    return Preferences(**base)


class TestTheAddressList:
    def test_it_defaults_to_nobody(self):
        assert prefs().share_with == []

    def test_a_good_address_validates(self):
        prefs(share_with=["ana@x.com", "luis@y.com"]).validate()

    @pytest.mark.parametrize("bad", ["nope", "a b@x.com", " a@x.com",
                                     "a@x.com "])
    def test_junk_is_refused(self, bad):
        with pytest.raises(PreferencesError):
            prefs(share_with=[bad]).validate()

    def test_it_survives_a_save_and_load(self, tmp_path):
        p = tmp_path / "prefs.json"
        prefs(share_with=["ana@x.com"]).save(p)
        assert Preferences.load(p).share_with == ["ana@x.com"]


class TestSettingItFromTheCommandLine:
    def _prefs_file(self, tmp_path, **kw):
        p = tmp_path / "prefs.json"
        prefs(**kw).save(p)
        return p

    def test_it_parses_a_comma_separated_list(self, tmp_path):
        p = self._prefs_file(tmp_path)
        assert cli.set_share_with(Preferences.load(p),
                                  "ana@x.com, luis@y.com", str(p)) == 0
        assert Preferences.load(p).share_with == ["ana@x.com", "luis@y.com"]

    def test_an_empty_string_clears_it(self, tmp_path):
        p = self._prefs_file(tmp_path, share_with=["ana@x.com"])
        cli.set_share_with(Preferences.load(p), "", str(p))
        assert Preferences.load(p).share_with == []

    def test_junk_is_rejected_without_writing(self, tmp_path):
        p = self._prefs_file(tmp_path, share_with=["ana@x.com"])
        assert cli.set_share_with(Preferences.load(p), "not-an-email",
                                  str(p)) == 2
        assert Preferences.load(p).share_with == ["ana@x.com"], (
            "a bad address wiped the good ones")

    def test_the_owner_is_never_bcc_themselves(self, tmp_path):
        """They are already the To; a second copy is just a duplicate."""
        p = self._prefs_file(tmp_path)
        cli.set_share_with(Preferences.load(p),
                           "me@example.com, ana@x.com", str(p))
        assert Preferences.load(p).share_with == ["ana@x.com"]

    def test_duplicates_collapse(self, tmp_path):
        p = self._prefs_file(tmp_path)
        cli.set_share_with(Preferences.load(p),
                           "ana@x.com, ANA@x.com, ana@x.com", str(p))
        assert Preferences.load(p).share_with == ["ana@x.com"]

    def test_listing_does_not_change_anything(self, tmp_path):
        p = self._prefs_file(tmp_path, share_with=["ana@x.com"])
        assert cli.set_share_with(Preferences.load(p), "list", str(p)) == 0
        assert Preferences.load(p).share_with == ["ana@x.com"]


class TestTheAlarmsStayPrivate:
    """The point of the whole feature."""

    def test_alarm_send_passes_no_bcc(self):
        """Structural: the alarm path cannot leak because it never offers
        the argument. Checked in source because the guarantee is the
        absence of a call, which no runtime assertion can observe."""
        import re
        from pathlib import Path
        src = Path("tracker/alarm.py").read_text(encoding="utf-8")
        i = src.index("def send(")
        nxt = src.find("\ndef ", i + 1)
        body = src[i:] if nxt == -1 else src[i:nxt]   # it is the last one
        assert "bcc" not in body, (
            "alarm.send now takes recipients - a throttle warning could "
            "reach a friend")

    def test_bcc_defaults_to_nobody(self):
        """So any caller that forgets is private, not public."""
        import inspect
        sig = inspect.signature(notify.send_email)
        assert tuple(sig.parameters["bcc"].default) == ()

    def test_an_alarm_reaches_only_the_owner(self, monkeypatch):
        seen = {}

        def spy(content, **kw):
            seen.update(kw)
            return notify.DeliveryResult(True, "ok")

        monkeypatch.setattr(alarm, "send_email", spy)
        cfg = alarm.AlarmConfig(to_addr="me@example.com",
                                smtp_host="h", smtp_port=587,
                                smtp_user="u", smtp_password="p")
        alarm.send(alarm.EmailContent("subject", "<p>x</p>", "x"), cfg)
        assert seen.get("to_addr") == "me@example.com"
        assert not seen.get("bcc"), "an alarm carried extra recipients"


class TestTheFareEmailsDoShare:
    def test_send_email_bccs_the_extras(self, monkeypatch):
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg, to_addrs=None):
                sent["to_addrs"] = to_addrs
                sent["headers"] = dict(msg.items())

        monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
        r = notify.send_email(
            notify.EmailContent("s", "<p>h</p>", "t"),
            to_addr="me@example.com", smtp_host="h", smtp_port=587,
            smtp_user="u@x.com", smtp_password="p",
            bcc=["ana@x.com", "luis@y.com"])
        assert r.ok
        assert sent["to_addrs"] == ["me@example.com", "ana@x.com",
                                    "luis@y.com"]

    def test_the_addresses_stay_blind(self, monkeypatch):
        """No Bcc header, or every friend sees every other friend."""
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg, to_addrs=None):
                sent["headers"] = dict(msg.items())

        monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
        notify.send_email(
            notify.EmailContent("s", "<p>h</p>", "t"),
            to_addr="me@example.com", smtp_host="h", smtp_port=587,
            smtp_user="u@x.com", smtp_password="p", bcc=["ana@x.com"])
        joined = " ".join(f"{k}: {v}" for k, v in sent["headers"].items())
        assert "ana@x.com" not in joined, (
            "a friend's address appeared in the headers")

    def test_both_fare_paths_pass_the_list(self):
        """`run` and `email_now` are the only two that should."""
        from pathlib import Path
        src = Path("tracker/cli.py").read_text(encoding="utf-8")
        assert src.count("bcc=prefs.share_with") == 2, (
            "one of the two fare emails is not shared, or something else is")

    def test_nobody_configured_changes_nothing(self, monkeypatch):
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg, to_addrs=None):
                sent["to_addrs"] = to_addrs

        monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
        notify.send_email(
            notify.EmailContent("s", "<p>h</p>", "t"),
            to_addr="me@example.com", smtp_host="h", smtp_port=587,
            smtp_user="u@x.com", smtp_password="p")
        assert sent["to_addrs"] == ["me@example.com"]
