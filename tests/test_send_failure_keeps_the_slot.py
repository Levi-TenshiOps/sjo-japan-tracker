"""A send that fails must not spend the email slot.

The budget is two emails a day and the second is reserved, so a slot
burned on a message that never arrived is a slot the trip owner cannot
get back: the day ends with one email delivered and the state file
claiming two were sent.

`cli.run` gets this right - `record_sent` is called only under
`result.ok`, and a failure calls `roll_day` instead and returns 1 - but
nothing pinned it, and it is one careless edit away from being wrong.
Found unpinned during an email-system audit on 2026-08-26.

This is also the one failure mode CLAUDE.md says is deliberately *not*
alarmed ("you cannot be emailed that email is broken"), so the retry on
the next run is the entire recovery mechanism. If the slot is burned,
there is no recovery at all.
"""
import io
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import alerts, cli, notify

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prefs = json.loads(io.open(os.path.join(root, "preferences.example.json"),
                               encoding="utf-8").read())
    prefs["alert_email"] = "nobody@example.com"
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps(prefs), encoding="utf-8")
    cfg = io.open(os.path.join(root, "config.yaml"), encoding="utf-8").read()
    c = tmp_path / "config.yaml"
    c.write_text(cfg, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return {"prefs": str(p), "config": str(c), "dir": tmp_path}


def _silence_google(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "fetch_text_query", lambda *a, **k: "")
    monkeypatch.setattr(cli.monthly, "scan_months", lambda *a, **k: [])

    class FakeSearcher:
        requests_made = 0

        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(cli, "Searcher", FakeSearcher)
    monkeypatch.setattr(cli, "collect",
                        lambda *a, **k: ([], [], [], (0, 0), None))
    # Chrome must return *something* or the run exits before the send and
    # the test proves nothing - it passed on "Nothing usable from any
    # source this run" the first time round.
    from datetime import date
    from tracker.browser import BrowserOption
    fare = BrowserOption(
        price_usd=1343, origin="SJO", destination="TYO",
        depart_date=date(2027, 1, 22), return_date=date(2027, 2, 21),
        stops=("ZRH",), airlines=("SWISS",), total_minutes=2780,
        deep_link="https://example.invalid/x")
    monkeypatch.setattr(cli.verify_mod, "verify",
                        lambda *a, **k: (k.get("stats", {}).update(
                            {"attempts": 1, "blank": 0}) or [fare]))


class TestTheSlotSurvivesAFailedSend:
    def test_the_source_only_records_on_success(self):
        """The guard itself, so a refactor cannot quietly drop it."""
        import re
        from pathlib import Path
        src = re.sub(r"\s+", " ",
                     Path("tracker/cli.py").read_text(encoding="utf-8"))
        assert "if result.ok and not args.dry_run: alerts.record_sent(" in src, \
            "record_sent is no longer guarded by a successful send"
        assert "else: state.roll_day(now)" in src, \
            "a failed send must roll the day rather than record a send"

    def test_a_failed_send_exits_non_zero(self, workspace, monkeypatch):
        _silence_google(monkeypatch)
        monkeypatch.setattr(
            notify, "send_email",
            lambda *a, **k: notify.DeliveryResult(False, "smtp exploded"))
        monkeypatch.setattr(
            cli.notify, "send_email",
            lambda *a, **k: notify.DeliveryResult(False, "smtp exploded"))
        code = cli.run(["--no-history", "-p", workspace["prefs"],
                        "-c", workspace["config"]])
        assert code != 0, "a failed send reported success"

    def test_state_does_not_claim_an_email_was_sent(self, workspace,
                                                    monkeypatch):
        _silence_google(monkeypatch)
        monkeypatch.setattr(
            cli.notify, "send_email",
            lambda *a, **k: notify.DeliveryResult(False, "smtp exploded"))
        cli.run(["--no-history", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        state_file = workspace["dir"] / "state.json"
        if not state_file.exists():
            return                      # nothing was sendable; nothing to burn
        st = alerts.AlertState.load(state_file)
        assert st.emails_sent_today == 0, (
            "the slot was burned on an email that never arrived")


class TestRecordSentIsWhatSpendsTheBudget:
    """Unit-level, so the property holds even if `run` is restructured."""

    def _state(self):
        return alerts.AlertState()

    def test_recording_spends_one(self):
        st = self._state()
        alerts.record_sent(st, best_price=1343, best_signature="x",
                           is_great=False,
                           now=datetime(2026, 8, 26, 9, tzinfo=timezone.utc))
        assert st.emails_sent_today == 1

    def test_rolling_the_day_spends_nothing(self):
        st = self._state()
        st.roll_day(datetime(2026, 8, 26, 9, tzinfo=timezone.utc))
        assert st.emails_sent_today == 0

    def test_rolling_twice_still_spends_nothing(self):
        """Six failed runs in a day must still leave both slots open."""
        st = self._state()
        for h in (6, 9, 12, 15, 18, 21):
            st.roll_day(datetime(2026, 8, 26, h, tzinfo=timezone.utc))
        assert st.emails_sent_today == 0
