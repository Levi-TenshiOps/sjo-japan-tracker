"""Two failures that used to pass in silence.

The trip owner asked whether there is an email if Google blocks us and if
the sweeper stops. There was one for the block and **not** for the stop:
a scheduled run noticed - it reads the store's `last_active` - and wrote a
line to `tracker.log`, which is the one place nobody looks.

The second is worse and is what the README means by "can break without
warning": Google changes its markup, the pages still arrive, the parser
cannot read some rows, and those fares simply never exist as far as the
tracker is concerned. No error, no block - just a quieter market.

Only the scheduled runs can report either. The sweep cannot announce its
own death.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import alarm as alarm_mod
from tracker import cli
from tracker.sweeper import SweepStore
from tracker.throttle import ThrottleState


class Cfg:
    alert_email = "x@y.z"
    smtp_host = "h"
    smtp_port = 587
    smtp_user = ""
    smtp_password = ""

    def __init__(self, tmp):
        self.throttle_file = str(tmp / "t.json")


def spy(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "_send_alarm",
                        lambda content, cfg: sent.append(content))
    return sent


class TestTheSweepStopping:
    def test_it_emails_when_the_sweep_goes_quiet(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        st = ThrottleState()
        cli._watch_the_sweep(Cfg(tmp_path), None, st,
                             store=SweepStore(), idle=6.0)
        assert [c.subject for c in sent] == ["⚠ The background flight sweep has stopped"]
        assert st.sweep_idle_alarm_sent is True

    def test_a_healthy_sweep_sends_nothing(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        cli._watch_the_sweep(Cfg(tmp_path), None, ThrottleState(),
                             store=SweepStore(), idle=0.5)
        assert sent == []

    def test_it_does_not_send_six_copies_a_day(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        st = ThrottleState()
        for _ in range(6):
            cli._watch_the_sweep(Cfg(tmp_path), None, st,
                                 store=SweepStore(), idle=6.0)
        assert len(sent) == 1

    def test_it_re_arms_once_the_sweep_returns(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        st = ThrottleState()
        cli._watch_the_sweep(Cfg(tmp_path), None, st, store=SweepStore(), idle=6.0)
        cli._watch_the_sweep(Cfg(tmp_path), None, st, store=SweepStore(), idle=0.2)
        assert st.sweep_idle_alarm_sent is False
        cli._watch_the_sweep(Cfg(tmp_path), None, st, store=SweepStore(), idle=6.0)
        assert len(sent) == 2

    def test_a_store_that_has_never_run_says_nothing(self, tmp_path, monkeypatch):
        """`idle` is None before the sweep has ever priced a window."""
        sent = spy(monkeypatch)
        cli._watch_the_sweep(Cfg(tmp_path), None, ThrottleState(),
                             store=SweepStore(), idle=None)
        assert sent == []

    def test_the_email_says_how_to_restart_it(self):
        m = alarm_mod.sweep_stopped_email(hours=5.0, cursor="window 1/2", pending=3)
        assert "python sweep_forever.py" in m.text
        assert "does not restart itself" in m.text
        assert "Nothing is lost" in m.text


class TestTheParserGoingBlind:
    def _store(self, missed):
        s = SweepStore()
        s.rows_missed_by_parser = missed
        s.windows_priced = 2800
        return s

    def test_zero_unreadable_rows_is_silence(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        cli._watch_the_sweep(Cfg(tmp_path), None, ThrottleState(),
                             store=self._store(0), idle=0.1)
        assert sent == []

    def test_a_handful_is_not_worth_waking_anyone(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        cli._watch_the_sweep(Cfg(tmp_path), None, ThrottleState(),
                             store=self._store(3), idle=0.1)
        assert sent == []

    def test_a_material_number_emails(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        st = ThrottleState()
        cli._watch_the_sweep(Cfg(tmp_path), None, st,
                             store=self._store(cli.PARSER_ALARM_ROWS), idle=0.1)
        assert len(sent) == 1
        assert "cannot read" in sent[0].subject
        assert st.parser_alarm_sent is True

    def test_it_is_sent_once(self, tmp_path, monkeypatch):
        sent = spy(monkeypatch)
        st = ThrottleState()
        for _ in range(4):
            cli._watch_the_sweep(Cfg(tmp_path), None, st,
                                 store=self._store(500), idle=0.1)
        assert len(sent) == 1

    def test_the_email_says_it_is_not_a_block(self):
        m = alarm_mod.parser_broken_email(missed=180, windows=2800)
        assert "not a block" in m.text
        assert "Waiting will not fix it" in m.text
        assert "tracker/browser.py" in m.text


class TestItNeverCostsTheEmail:
    def test_the_watchdog_call_is_guarded(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "tracker" / "cli.py").read_text(encoding="utf-8")
        # The call, not the def - they differ only by leading whitespace.
        i = src.find("        _watch_the_sweep(cfg, prefs, throttle_state")
        assert i > 0, "the watchdog is gone"
        assert "try:" in src[max(0, i - 600):i]
        assert "sweep watchdog failed" in src[i:i + 300]
