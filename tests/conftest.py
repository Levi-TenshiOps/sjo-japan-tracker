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
