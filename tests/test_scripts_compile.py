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
