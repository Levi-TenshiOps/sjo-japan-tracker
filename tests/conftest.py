"""Keep the test suite off the running tracker's files.

Found on 2026-08-30, from the outside: the sweep started logging

    Still waiting for the Google lock (held by sweep)
    Breaking a stale Google lock held by sweep (pid 9488)

every five minutes, and priced nothing for twenty. pid 9488 was a pytest
run. `sweep_batch`'s `lock_path` defaults to `gate.DEFAULT_LOCK`, which is
the relative path "google.lock", and around 130 test call sites do not
override it - so a suite run from the repository directory takes the
*production* lock, and the live sweep waits behind it, exactly as it is
designed to.

Nothing was broken. The tests injected a fake fetch, so no request reached
Google and the lock did its job perfectly. But a test run must not be able
to stall the thing it is testing, and on a public repository the first
thing anyone does is clone it and run the suite - possibly on the machine
already running the tracker.

Redirecting the default here covers every call site, including ones
written later, which is the point: 130 individual `lock_path=` arguments
would be one forgotten argument away from the same bug.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import gate, sweeper


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_lock(tmp_path_factory):
    """Point the default Google lock at a throwaway directory.

    Session-scoped and autouse: a test that means to exercise locking still
    passes its own `path`, and those are unaffected.
    """
    sandbox = str(tmp_path_factory.mktemp("locks") / "google.lock")
    real = gate.DEFAULT_LOCK
    gate.DEFAULT_LOCK = sandbox

    # Rebinding the module constant is not enough. Every function that took
    # it as a default argument captured the *string* at import time, so
    # each one has to be rewritten too: `gate.google` and `sweep_batch`
    # hold it keyword-only, `gate.holder` positionally.
    patched = []
    for fn in (gate.google, gate.holder, sweeper.sweep_batch):
        target = getattr(fn, "__wrapped__", fn)      # google is a contextmanager
        if target.__defaults__:
            patched.append((target, "__defaults__", target.__defaults__))
            target.__defaults__ = tuple(
                sandbox if d == real else d for d in target.__defaults__)
        if target.__kwdefaults__:
            patched.append((target, "__kwdefaults__", dict(target.__kwdefaults__)))
            target.__kwdefaults__ = {
                k: (sandbox if v == real else v)
                for k, v in target.__kwdefaults__.items()}

    yield

    for target, attr, old_val in patched:
        setattr(target, attr, old_val)
    gate.DEFAULT_LOCK = real


@pytest.fixture(autouse=True)
def _never_write_the_real_store(monkeypatch, tmp_path):
    """A test that forgets `--store` must not rewrite discoveries.json.

    Same shape as the lock: the default is a relative path, so it resolves
    against whatever directory pytest was started from. Losing the sweep's
    cursor and 400 findings to a test run would be much worse than a
    stalled lock, and nothing prevented it.
    """
    monkeypatch.setattr(sweeper, "DEFAULT_STORE",
                        str(tmp_path / "discoveries.json"), raising=False)


@pytest.fixture(autouse=True, scope="session")
def _never_send_real_email():
    """No test may open an SMTP connection. Ever.

    Found on 2026-08-31 the only way it could be found: the trip owner
    received two real alarm emails, "No flight email has gone out for
    43806 hours", timed to the minute with a test run.

    `tests/test_rate_tripwire.py` drives the real `sweep_forever.main()`.
    It redirects `--store` and `--preferences` to tmp_path but nothing
    redirects the *config*, so `cfg.state_file` was the production
    `state.json` and `alarm_cfg` carried the real credentials out of
    `.env`. The silence watchdog compared the two, believed the scheduled
    runs had died, and sent - correctly, to a real inbox, from a test.

    It normally stays quiet only because `state.json` is usually fresher
    than SILENCE_HOURS. That is not a safeguard, it is a coincidence: any
    run of the suite on a machine whose tracker has been idle overnight
    sends real mail. The 43806 hours came from a clock-shifted run, but
    the wiring was live the whole time.

    Blocking the transport is the guarantee, because it does not depend on
    anyone remembering to redirect a path. Tests that deliberately
    exercise a broken SMTP still pass - a raising socket is exactly what
    they assert is survivable.
    """
    import smtplib

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "A test tried to open an SMTP connection. Tests never send "
            "email - inject a fake, or assert on the EmailContent. See "
            "tests/conftest.py::_never_send_real_email.")

    real_smtp, real_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP, smtplib.SMTP_SSL = _blocked, _blocked
    yield
    smtplib.SMTP, smtplib.SMTP_SSL = real_smtp, real_ssl


@pytest.fixture(autouse=True)
def _never_read_the_real_state(monkeypatch, tmp_path):
    """`state_file` is a relative default, so it resolves against wherever
    pytest was started - the repository, normally. A test that reads it is
    asking the machine it runs on how many emails went out today, which
    makes its result depend on the time of day. Same shape as the lock and
    the store above, and the reason the alarm below it could fire at all.
    """
    from tracker import config as config_mod

    real_load = config_mod.load

    def _sandboxed(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.state_file = str(tmp_path / "state.json")
        return cfg

    monkeypatch.setattr(config_mod, "load", _sandboxed)
