"""A whole run, with every path to Google faked.

`cli.py` had no tests at all until 2026-08-23, which is how "the wide net
kept querying excluded months" shipped. The unit tests since then cover the
pieces; this covers the seam - that `run()` actually completes and returns
0 after the wide net, the grid, Chrome verification and the email decision
have all been wired together.

Nothing here touches the network. Every fetch is injected.
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import cli

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A complete, isolated set of the files a run reads and writes."""
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
    return {"prefs": str(p), "config": str(c)}


def _fake_everything(monkeypatch, *, dom: str, grid_hits: bool = True):
    """Silence every route to Google, and the clock-driven sleeps."""
    monkeypatch.setattr(cli.time, "sleep", lambda *_a, **_k: None)

    # The wide net. Patched whole rather than at the fetch: `scan_months`
    # paces itself at 3s a probe with its own `time.sleep`, so letting it
    # run would make this test a minute long for no extra coverage.
    monkeypatch.setattr(cli, "fetch_text_query", lambda *a, **k: "")
    monkeypatch.setattr(cli.monthly, "scan_months", lambda *a, **k: [])

    # The HTTP grid.
    class FakeSearcher:
        requests_made = 0

        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(cli, "Searcher", FakeSearcher)
    monkeypatch.setattr(
        cli, "collect",
        lambda *a, **k: ([], [], [], (0, 0), None) if not grid_hits
        else ([], [], [], (0, 0), None))

    # Chrome.
    monkeypatch.setattr(cli.verify_mod, "verify",
                        lambda *a, **k: (k.get("stats", {}).update(
                            {"attempts": 0, "blank": 0}) or []))


class TestAWholeRunCompletes:
    def test_it_exits_cleanly_when_nothing_is_usable(self, workspace, monkeypatch):
        """The empty case must return, not raise - it is the common one."""
        dom = io.open(FIXTURE, encoding="utf-8").read()
        _fake_everything(monkeypatch, dom=dom)
        code = cli.run(["--dry-run", "--no-history",
                        "-p", workspace["prefs"], "-c", workspace["config"]])
        assert code == 0

    def test_the_new_wide_net_filter_does_not_break_the_run(
            self, workspace, monkeypatch):
        """`departures` and the hint guard were added 2026-08-24."""
        dom = io.open(FIXTURE, encoding="utf-8").read()
        _fake_everything(monkeypatch, dom=dom)
        seen = {}

        def spy(*a, **k):
            seen.update(k)
            return []

        monkeypatch.setattr(cli.monthly, "scan_months", spy)
        cli.run(["--dry-run", "--no-history",
                 "-p", workspace["prefs"], "-c", workspace["config"]])
        assert "departures" in seen, "the wide net lost its date filter"
        assert seen["departures"], "the filter was passed but empty"
