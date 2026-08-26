"""`--email-now`: report what has been collected, on demand.

Asked for 2026-08-26, for the sale-day workflow: run the sweep in focus
mode, wait, then see what it found without waiting for 21:27.

Three properties carry the whole design, and each protects something that
already exists:

* it makes **no requests** - the sweep is what collects, and a reporting
  command that queried Google would contend for `gate.google()` and could
  itself be throttled;
* it does **not touch `state.json`** - the two-a-day budget and the
  reserved evening slot belong to the scheduled runs, and a manual report
  that spent a slot would silence a real alert later that day;
* it is **labelled** - an email arriving outside the usual two is
  otherwise indistinguishable from the alerting having gone wrong, and
  this project has already sent three false alarms.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import alerts, cli, notify


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


def _store_with_a_fare(tmp_path, price=1343):
    """A sweep store holding one fresh, bookable fare."""
    from datetime import date, timedelta
    from tracker import sweeper
    from tracker.schedule import Window
    s = sweeper.SweepStore()
    w = Window(date(2027, 1, 22), date(2027, 2, 21))
    s.found[w.key] = {
        "price_usd": price, "depart": w.depart.isoformat(),
        "ret": w.back.isoformat(), "seen_at": sweeper._now(),   # NOT found_at: age_hours reads seen_at
        "stops": ["ZRH"], "airlines": ["Edelweiss Air", "SWISS"],
        "total_minutes": 2780, "deep_link": "https://example.invalid/x",
        "origin": "SJO", "destination": "TYO",
    }
    s.save(tmp_path / "discoveries.json")
    return s


def _no_network(monkeypatch):
    """Any attempt to reach Google fails the test loudly."""
    def boom(*a, **k):
        raise AssertionError("--email-now made a network request")
    monkeypatch.setattr(cli, "fetch_text_query", boom)
    monkeypatch.setattr(cli.monthly, "scan_months", boom)
    monkeypatch.setattr(cli.verify_mod, "verify", boom)
    monkeypatch.setattr(cli, "collect", boom)
    monkeypatch.setattr(cli.gate, "google", boom)


class TestItMakesNoRequests:
    def test_nothing_reaches_google(self, workspace, monkeypatch):
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        sent = []
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: sent.append(c)
                            or notify.DeliveryResult(True, "ok"))
        code = cli.run(["--email-now", "-p", workspace["prefs"],
                        "-c", workspace["config"]])
        assert code == 0
        assert len(sent) == 1

    def test_it_can_be_run_twice_in_a_row(self, workspace, monkeypatch):
        """No budget, no state, so repeating it is harmless."""
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        sent = []
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: sent.append(c)
                            or notify.DeliveryResult(True, "ok"))
        for _ in range(3):
            cli.run(["--email-now", "-p", workspace["prefs"],
                     "-c", workspace["config"]])
        assert len(sent) == 3


class TestItLeavesTheDailyBudgetAlone:
    def test_state_json_is_not_written(self, workspace, monkeypatch):
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: notify.DeliveryResult(True, "ok"))
        cli.run(["--email-now", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        assert not (workspace["dir"] / "state.json").exists(), (
            "a manual report wrote the alert state")

    def test_an_existing_budget_is_untouched(self, workspace, monkeypatch):
        """The evening slot must still be there afterwards."""
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        st = alerts.AlertState()
        st.day = "2026-08-26"
        st.emails_sent_today = 1
        st.last_best_price = 1400
        st.save(workspace["dir"] / "state.json")
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: notify.DeliveryResult(True, "ok"))
        cli.run(["--email-now", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        after = alerts.AlertState.load(workspace["dir"] / "state.json")
        assert after.emails_sent_today == 1, "it spent a scheduled slot"
        assert after.last_best_price == 1400, "it rewrote the alert baseline"


class TestItIsRecognisableAsManual:
    def test_the_subject_says_on_demand(self, workspace, monkeypatch):
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        sent = []
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: sent.append(c)
                            or notify.DeliveryResult(True, "ok"))
        cli.run(["--email-now", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        assert sent[0].subject.startswith("[on demand]")

    def test_it_still_carries_the_price_and_the_greeting(self, workspace,
                                                         monkeypatch):
        from tracker.email_render import GREETING
        _store_with_a_fare(workspace["dir"], price=1343)
        _no_network(monkeypatch)
        sent = []
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: sent.append(c)
                            or notify.DeliveryResult(True, "ok"))
        cli.run(["--email-now", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        assert "1,343" in sent[0].text
        assert GREETING in sent[0].text and GREETING in sent[0].html


class TestItFailsHonestly:
    def test_an_empty_store_does_not_send(self, workspace, monkeypatch):
        """Better a non-zero exit than an email with nothing in it."""
        _no_network(monkeypatch)
        called = []
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: called.append(c)
                            or notify.DeliveryResult(True, "ok"))
        code = cli.run(["--email-now", "-p", workspace["prefs"],
                        "-c", workspace["config"]])
        assert code == 1
        assert called == [], "it emailed an empty report"

    def test_a_failed_send_exits_non_zero(self, workspace, monkeypatch):
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        monkeypatch.setattr(
            cli.notify, "send_email",
            lambda c, **k: notify.DeliveryResult(False, "smtp exploded"))
        code = cli.run(["--email-now", "-p", workspace["prefs"],
                        "-c", workspace["config"]])
        assert code == 1

    def test_dry_run_sends_nothing(self, workspace, monkeypatch):
        _store_with_a_fare(workspace["dir"])
        _no_network(monkeypatch)
        seen = {}
        monkeypatch.setattr(cli.notify, "send_email",
                            lambda c, **k: seen.update(k)
                            or notify.DeliveryResult(True, "dry"))
        cli.run(["--email-now", "--dry-run", "-p", workspace["prefs"],
                 "-c", workspace["config"]])
        assert seen.get("dry_run") is True
