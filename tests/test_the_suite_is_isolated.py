"""Running the tests must not disturb a running tracker.

Found from the outside on 2026-08-30: the live sweep logged

    Still waiting for the Google lock (held by sweep)
    Breaking a stale Google lock held by sweep (pid 9488)

every five minutes and priced nothing for twenty. pid 9488 was a pytest
run. `sweep_batch`'s `lock_path` defaults to the relative "google.lock",
about 130 test call sites do not override it, and a suite run from the
repository directory therefore takes the *production* lock. The sweep then
waited behind it, exactly as designed.

Nothing was broken and no request reached Google - the tests inject a fake
fetch. But a test run must not be able to stall the thing it tests, and on
a public repository the first thing anyone does is clone it and run the
suite, quite possibly on the machine already running the tracker.

conftest.py redirects the defaults for the whole session. These tests are
what stop that quietly regressing.
"""
import os
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import gate, sweeper


class TestTheDefaultsPointSomewhereSafe:
    def test_the_google_lock_default_is_redirected(self):
        assert gate.DEFAULT_LOCK != "google.lock", (
            "the suite would take the production lock")
        assert "google.lock" in gate.DEFAULT_LOCK      # still named sensibly

    def test_sweep_batch_picked_up_the_redirect(self):
        """The one that mattered: it captured the string at import time, so
        rebinding the module constant alone did not reach it."""
        import inspect
        d = inspect.signature(sweeper.sweep_batch).parameters["lock_path"].default
        assert d != "google.lock", "sweep_batch still defaults to the real lock"

    def test_gate_google_picked_up_the_redirect(self):
        import inspect
        target = getattr(gate.google, "__wrapped__", gate.google)
        d = inspect.signature(target).parameters["path"].default
        assert d != "google.lock"

    def test_the_store_default_is_redirected(self):
        """Losing the sweep's cursor and findings to a test run would be
        worse than a stalled lock."""
        assert sweeper.DEFAULT_STORE != "discoveries.json"


class TestNothingIsWrittenWhereItShouldNotBe:
    def test_a_sweep_batch_leaves_the_repo_alone(self, tmp_path):
        """The end-to-end claim: run the real thing, touch nothing real."""
        from datetime import date, timedelta
        from tracker.schedule import Window

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        watched = [os.path.join(root, n) for n in
                   ("google.lock", "discoveries.json", "sweep.stop", "sweep.pid")]
        before = {p: (os.path.exists(p), os.path.getmtime(p)
                      if os.path.exists(p) else 0) for p in watched}

        w = [Window(date(2027, 1, 1) + timedelta(days=i),
                    date(2027, 1, 1) + timedelta(days=i + 27)) for i in range(3)]
        s = sweeper.SweepStore()
        dom = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "chrome_dom_32n.html"), encoding="utf-8").read()
        sweeper.sweep_batch(w, s, batch=2, fetch=lambda u: dom, delay_s=0,
                            sleep=lambda *_: None,
                            save_to=str(tmp_path / "d.json"),
                            history_csv=str(tmp_path / "h.csv"))

        after = {p: (os.path.exists(p), os.path.getmtime(p)
                     if os.path.exists(p) else 0) for p in watched}
        changed = [os.path.basename(p) for p in watched if before[p] != after[p]]
        assert changed == [], f"a test run touched {changed}"


class TestTheSuiteCannotSendEmail:
    """The suite emailed the trip owner twice on 2026-08-31.

    Not a near miss and not a simulation - two real messages, real
    credentials, real inbox, sent by `pytest`. The subject said the
    scheduled runs had been silent for 43806 hours, which was the alarm
    reading production `state.json` while a test drove the sweep's main
    loop.
    """

    def test_smtp_is_blocked(self):
        import smtplib
        with pytest.raises(RuntimeError, match="never send email"):
            smtplib.SMTP("localhost", 25)
        with pytest.raises(RuntimeError, match="never send email"):
            smtplib.SMTP_SSL("localhost", 465)

    def test_the_alarm_path_cannot_deliver(self):
        """The exact path that fired: alarm.send with a usable config.

        It must come back False rather than delivering. `send` swallows
        the failure by design - a failed alarm must never stop the sweep -
        so the assertion is that nothing left the machine, not that it
        raised.
        """
        from tracker import alarm as alarm_mod

        cfg = alarm_mod.AlarmConfig(
            to_addr="nobody@example.invalid", smtp_host="smtp.example.invalid",
            smtp_port=465, smtp_user="u", smtp_password="p")
        assert cfg.usable, "the test needs a config that would really send"
        content = alarm_mod.silent_email(hours=43806.0, threshold=16.0)
        assert alarm_mod.send(content, cfg) is False

    def test_config_load_does_not_hand_out_the_real_state_file(self):
        """A test reading production state.json is asking the machine it
        runs on what time it is, which is how the alarm came to fire."""
        from tracker import config as config_mod

        cfg = config_mod.load()
        assert cfg.state_file != "state.json", (
            "config.load() returned the production state file; a test that "
            "reads it depends on when this machine last sent an email")
        assert not os.path.samefile(
            os.path.dirname(os.path.abspath(cfg.state_file)) or ".", os.getcwd()), \
            "the sandboxed state file still resolves inside the repository"
