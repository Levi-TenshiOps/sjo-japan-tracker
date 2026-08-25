"""Every shipped script must at least parse, and its --help must work.

Written after `sweep_forever.py` shipped with a syntax error - a help
string containing raw newlines - and 653 tests passed anyway. Nothing
imported it, because the entry points are scripts rather than modules and
the suite only ever exercised `tracker/`. The trip owner found it by
running the command I had just told them to run.

Compiling is a low bar. It is also exactly the bar that was missed.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pathlib
import py_compile
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Scripts the user is told to run directly.
ENTRY_POINTS = sorted(p.name for p in ROOT.glob("*.py"))

# Of those, the ones driven by argparse, whose --help must actually render.
# The others prompt interactively and must not be executed here.
ARGPARSE_SCRIPTS = ["sweep_forever.py", "install_schedule.py"]


def test_there_are_entry_points_to_check():
    """Guard against this file silently testing nothing."""
    assert len(ENTRY_POINTS) >= 4, ENTRY_POINTS


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_entry_point_compiles(name):
    try:
        py_compile.compile(str(ROOT / name), doraise=True)
    except py_compile.PyCompileError as exc:
        pytest.fail(f"{name} does not compile:\n{exc}")


@pytest.mark.parametrize(
    "name", sorted(p.name for p in (ROOT / "tracker").glob("*.py")))
def test_tracker_module_compiles(name):
    try:
        py_compile.compile(str(ROOT / "tracker" / name), doraise=True)
    except py_compile.PyCompileError as exc:
        pytest.fail(f"tracker/{name} does not compile:\n{exc}")


@pytest.mark.parametrize("name", ARGPARSE_SCRIPTS)
def test_help_renders(name):
    """A malformed help string parses fine and still breaks at runtime.

    argparse builds its help lazily, so a bad `help=` survives compilation
    and only fails when somebody asks for it - or, as happened here, when
    the module is imported at all.
    """
    done = subprocess.run(
        [sys.executable, str(ROOT / name), "--help"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert done.returncode == 0, f"{name} --help failed:\n{done.stderr}"
    assert "usage" in (done.stdout or "").lower()


def test_the_cli_help_renders():
    """`tracker.cli` is the product, and it is reached as a module.

    ENTRY_POINTS only globs top-level scripts, so the scheduled task's own
    command - `python -m tracker.cli` - was not covered by anything here
    until a new argparse option was added to it on 2026-08-23.
    """
    done = subprocess.run(
        [sys.executable, "-m", "tracker.cli", "--help"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert done.returncode == 0, f"tracker.cli --help failed:\n{done.stderr}"
    assert "usage" in (done.stdout or "").lower()


def test_the_documented_sweep_command_starts():
    """The exact command the README and I hand the trip owner.

    --status exits immediately and touches nothing, so this is safe to run
    while a real sweep is going: it never queries Google.

    It is pointed at `preferences.example.json` on purpose. The real
    `preferences.json` holds the trip owner's email address and is therefore
    gitignored, so it does not exist in a fresh checkout - the first version
    of this test used the default and failed every CI run on main while
    passing on the machine that wrote it. A test that cannot run from a
    clean clone is testing the developer's disk, not the project.
    """
    done = subprocess.run(
        [sys.executable, str(ROOT / "sweep_forever.py"), "--status",
         "--preferences", str(ROOT / "preferences.example.json")],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    assert "window" in (done.stdout or "").lower()


def test_missing_preferences_fails_with_advice_not_a_traceback():
    """The first thing a new clone hits. It must say what to do about it."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "sweep_forever.py"), "--status",
         "--preferences", "definitely-not-here.json"],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert done.returncode == 2, done.stdout
    assert "setup_tracker.py" in (done.stderr or ""), done.stderr
    assert "Traceback" not in (done.stderr or "")


def test_a_config_that_searches_nothing_is_refused(tmp_path):
    """It must exit, not sit in a loop pricing nothing.

    Found 2026-08-23. `included_months: [6]` against an 8-month horizon
    starting in August names a month the horizon never reaches, so
    `generate_windows` returns an empty list and `sweep_batch` returns
    immediately. The outer loop has no sleep of its own - the pacing lives
    per-window - so the process spun at full speed rewriting the store.
    """
    import json
    prefs = json.loads((ROOT / "preferences.example.json").read_text(
        encoding="utf-8"))
    prefs["included_months"] = [6]      # unreachable from any 8-month horizon
    prefs["priority_months"] = []
    prefs["search_months"] = 8
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps(prefs), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(ROOT / "sweep_forever.py"),
         "--preferences", str(path), "--log", "",
         "--store", str(tmp_path / "store.json")],
        capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    assert done.returncode == 2, (
        f"expected a refusal, got {done.returncode}. stdout={done.stdout[:400]}")
    assert "No travel windows" in done.stderr, done.stderr
    assert not (tmp_path / "store.json").exists(), (
        "it must not have started writing a store")


def test_the_store_is_pruned_on_startup(tmp_path):
    """`prune` runs after a batch completes, so a run stopped mid-batch
    never prunes. After a day of restarting to pick up fixes on 2026-08-24
    the live store held 459 findings against a MAX_ENTRIES of 400 - the cap
    was not binding because nothing was enforcing it at startup.
    """
    import json
    from tracker.sweeper import MAX_ENTRIES, SweepStore
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    s = SweepStore()
    for i in range(MAX_ENTRIES + 120):
        key = f"2027-01-01_2027-02-{i:04d}"
        s.found[key] = {"depart": "2027-01-01", "ret": "2027-02-01",
                        "price_usd": 1000 + i, "origin": "SJO",
                        "destination": "TYO", "stops": [], "airlines": [],
                        "total_minutes": 100, "deep_link": "", "seen_at": now}
    store_path = tmp_path / "discoveries.json"
    s.save(store_path)
    assert len(SweepStore.load(store_path).found) > MAX_ENTRIES

    prefs = json.loads((ROOT / "preferences.example.json").read_text(
        encoding="utf-8"))
    prefs_path = tmp_path / "prefs.json"
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    # `--recheck-unverified` rather than `--coverage`: the latter became
    # read-only on 2026-08-24, because a reporting command that writes the
    # store is the "two sweepers" hazard - it was being run against a live
    # sweep, whose in-memory copy would then overwrite it. This needs a
    # command that legitimately writes and still makes no request.
    done = subprocess.run(
        [sys.executable, str(ROOT / "sweep_forever.py"),
         "--recheck-unverified",
         "--preferences", str(prefs_path), "--store", str(store_path),
         "--log", ""],
        capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    assert len(SweepStore.load(store_path).found) <= MAX_ENTRIES, (
        "the store was not pruned on startup")


class TestTheSweepCanBeStoppedCleanly:
    """Killing it leaves the Google lock behind.

    Every hard kill on 2026-08-24 produced "Breaking a stale Google lock
    held by sweep (pid ...)" in the next run's log - harmless, and it reads
    exactly like a real problem. The trip owner asked about it twice.

    A stop file works from anywhere: another terminal, a script, or a
    session that no longer has the window the sweep was launched from.
    """

    def test_requesting_a_stop_is_visible_to_the_runner(self, tmp_path):
        import sweep_forever as sf
        f = str(tmp_path / "sweep.stop")
        assert sf.stop_requested(f) is False
        sf.request_stop(f)
        assert sf.stop_requested(f) is True

    def test_clearing_it_lets_the_next_run_start(self, tmp_path):
        """A leftover file must not stop the next sweep before it begins."""
        import sweep_forever as sf
        f = str(tmp_path / "sweep.stop")
        sf.request_stop(f)
        sf.clear_stop(f)
        assert sf.stop_requested(f) is False

    def test_clearing_a_file_that_is_not_there_is_fine(self, tmp_path):
        import sweep_forever as sf
        sf.clear_stop(str(tmp_path / "never-existed.stop"))

    def test_the_stop_command_writes_the_file_and_exits(self, tmp_path):
        import json
        prefs = json.loads((ROOT / "preferences.example.json").read_text(
            encoding="utf-8"))
        p = tmp_path / "prefs.json"
        p.write_text(json.dumps(prefs), encoding="utf-8")
        stop_file = ROOT / "sweep.stop"
        existed = stop_file.exists()
        try:
            done = subprocess.run(
                [sys.executable, str(ROOT / "sweep_forever.py"), "--stop",
                 "--preferences", str(p), "--log", ""],
                capture_output=True, text=True, timeout=60, cwd=str(ROOT))
            assert done.returncode == 0, done.stderr
            assert "Stop requested" in done.stdout
            assert stop_file.exists()
        finally:
            if not existed:
                stop_file.unlink(missing_ok=True)


class TestOnlyOneSweeperAtATime:
    """Two sweepers would fight over the store.

    `gate.py` stops two processes querying Google simultaneously, and
    nothing stopped two *sweepers* existing. Both hold the store in memory
    and write it after every window, so their cursors overwrite each other
    and coverage silently goes backwards - the worst shape of bug this
    project keeps producing.

    Easy to hit by accident: start it twice, or add a start-at-boot task
    while one is already running.
    """

    def test_no_lock_means_nothing_is_running(self, tmp_path):
        import sweep_forever as sf
        assert sf.another_sweeper_running(str(tmp_path / "sweep.pid")) is None

    def test_our_own_pid_is_not_another_sweeper(self, tmp_path):
        import os
        import sweep_forever as sf
        f = tmp_path / "sweep.pid"
        f.write_text(str(os.getpid()), encoding="utf-8")
        assert sf.another_sweeper_running(str(f)) is None

    def test_a_dead_pid_does_not_block_a_restart(self, tmp_path):
        """Otherwise a crash would lock the sweep out permanently."""
        import sweep_forever as sf
        f = tmp_path / "sweep.pid"
        f.write_text("999999", encoding="utf-8")
        assert sf.another_sweeper_running(str(f)) is None

    def test_a_live_other_pid_is_reported(self, tmp_path):
        import subprocess as sp
        import sweep_forever as sf
        proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            f = tmp_path / "sweep.pid"
            f.write_text(str(proc.pid), encoding="utf-8")
            assert sf.another_sweeper_running(str(f)) == proc.pid
        finally:
            proc.kill()

    def test_a_corrupt_pid_file_does_not_block(self, tmp_path):
        """Same lesson as everywhere else: never crash on a file you read."""
        import sweep_forever as sf
        f = tmp_path / "sweep.pid"
        for junk in ("", "not a pid", "0\n", "\x00"):
            f.write_text(junk, encoding="utf-8")
            assert sf.another_sweeper_running(str(f)) is None

    def test_claiming_and_releasing_round_trips(self, tmp_path):
        import os
        import sweep_forever as sf
        f = str(tmp_path / "sweep.pid")
        sf.claim_instance(f)
        assert pathlib.Path(f).read_text(encoding="utf-8") == str(os.getpid())
        sf.release_instance(f)
        assert not pathlib.Path(f).exists()

    def test_releasing_somebody_elses_lock_is_refused(self, tmp_path):
        """A second process must not clear the first one's claim."""
        import sweep_forever as sf
        f = tmp_path / "sweep.pid"
        f.write_text("999999", encoding="utf-8")
        sf.release_instance(str(f))
        assert f.exists(), "it deleted a lock it did not own"


class TestTheInstallerNeverArmsTheThrottleAgain:
    """The startup script it writes runs unattended at every boot.

    Found 2026-08-24, live on the machine: `install_schedule.py` had written
    `sweep_forever.py --batch 25 --delay 6` into the Windows Startup folder
    on 2026-08-23. Six seconds is ~14,000 requests a day and is precisely
    what threw the address into a day-long throttle that same morning.

    The code default had since been made safe (90s), and this file did not
    care - a rate written into an unattended launcher outlives every later
    fix to the default. So it now spells out no rate at all.
    """

    def test_the_installer_emits_no_dangerous_delay(self):
        src = (ROOT / "install_schedule.py").read_text(encoding="utf-8")
        emitted = [ln for ln in src.splitlines()
                   if "print(" in ln and "--delay" in ln]
        assert emitted == [], f"the installer still writes a rate: {emitted}"

    @pytest.mark.parametrize("bad", ["--delay 6", "--delay 8", "--delay 10"])
    def test_no_fast_rate_is_printed_anywhere(self, bad):
        src = (ROOT / "install_schedule.py").read_text(encoding="utf-8")
        for ln in src.splitlines():
            if "print(" in ln:
                assert bad not in ln, ln

    def test_the_safe_default_is_still_ninety(self):
        """The installer relies on this, so it must not drift."""
        done = subprocess.run(
            [sys.executable, str(ROOT / "sweep_forever.py"), "--help"],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        assert "default 90" in done.stdout, done.stdout


class TestTheStartupLauncherActuallyLogs:
    r"""The launcher's whole purpose is the run nobody is watching.

    Found 2026-08-24. The launcher read:

        start "" /min "...python.exe" -u sweep_forever.py --batch 25 ^
            >> "...\sweep.log" 2>&1

    `start` opens a new console and returns at once, so that redirection
    never captured the sweep's output at all. What it did do was leave a
    handle on sweep.log, and `logging.FileHandler` then failed to open it -
    reporting that failure to the new console, which is minimised and
    discarded.

    So the sweep ran perfectly and wrote nothing. Measured on the live
    machine: the store advanced every ~110 seconds while sweep.log sat
    frozen at the previous run's last line. After a reboot the sweep would
    have been running blind, and `sweep.log` would have looked exactly like
    a sweeper that had died - which is the diagnosis this project reaches
    for first.
    """

    def _launcher_text(self) -> str:
        """The launcher line as the installer will print it.

        Read from the source rather than by running the installer:
        `install_schedule.py` refuses to start without `preferences.json`,
        which is gitignored because it holds personal data, so executing it
        passes locally and fails in CI. That has now caught me twice.
        """
        src = (ROOT / "install_schedule.py").read_text(encoding="utf-8")
        i = src.find("-u sweep_forever.py")
        assert i > 0, "the installer no longer writes a sweep launcher"
        return src[i:i + 200].split("print(")[0]

    def test_it_passes_log_rather_than_redirecting(self):
        text = self._launcher_text()
        assert "--log" in text, text
        assert ">>" not in text, f"the redirect is back, and it does not work: {text}"
        assert "2>&1" not in text, text

    def test_the_log_path_is_absolute(self):
        """The child's cwd is not ours to assume."""
        text = self._launcher_text()
        assert "$root" in text.split("--log", 1)[1], text

    def test_sweep_forever_accepts_the_flag_the_installer_writes(self):
        """The installer is only correct if the flag actually exists."""
        done = subprocess.run(
            [sys.executable, str(ROOT / "sweep_forever.py"), "--help"],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        assert "--log" in done.stdout, done.stdout

    def test_the_rate_is_still_absent(self):
        """The other thing this launcher must never carry."""
        assert "--delay" not in self._launcher_text()
