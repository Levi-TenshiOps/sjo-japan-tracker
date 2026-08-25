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


class TestNothingBetweenTheSearchAndTheEmailMayRaise:
    """Everything after the search and before the send is best-effort.

    That stretch has now cost the trip owner an email twice: a torn CSV
    read on 2026-08-24, and a crash on the throttle alarm the same day.
    Writing the history is the third thing in it - and on Windows the file
    is read by other processes constantly, so a transient PermissionError
    is ordinary rather than exotic.
    """

    def _src(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "tracker" / "cli.py").read_text(encoding="utf-8")

    def test_the_history_write_is_guarded(self):
        src = self._src()
        i = src.find("history.append(cfg.history_csv")
        assert i > 0, "the history write is gone"
        assert "try:" in src[max(0, i - 400):i], "the history write can raise"
        assert "the email is unaffected" in src[i:i + 400]

    def test_the_block_alarm_is_guarded(self):
        src = self._src()
        i = src.find("\n        _raise_block_alarm(")
        assert i > 0 and "block alarm failed" in src[i:i + 500]

    def test_the_sweep_store_read_is_guarded(self):
        src = self._src()
        i = src.find("sweeper.SweepStore.load(cfg.sweep_store)")
        assert i > 0
        assert "except" in src[i:i + 400], "a missing sweep store could raise"

    def test_a_failing_history_write_still_sends(self, workspace, monkeypatch):
        """The behaviour, not just the shape."""
        import io as _io
        from tracker import cli, history

        def boom(*a, **k):
            raise PermissionError("file is open in another process")

        monkeypatch.setattr(history, "append", boom)
        _fake_everything(monkeypatch, dom=_io.open(FIXTURE, encoding="utf-8").read())
        code = cli.run(["--dry-run", "-p", workspace["prefs"],
                        "-c", workspace["config"]])
        assert code == 0, "a failed history write killed the run"


class TestTheWeakestSourceHasNoVeto:
    """The HTTP grid must not be able to cancel the email.

    It is the weakest source in the project: floored at 8 requests, ~74% of
    what it returns is visa-rejected, and it cannot see stays over 30
    nights at all. `run()` used to abort the moment it came back empty -
    *before* Chrome ran and before the sweep's findings were read - so a
    quiet grid threw away a Chrome-verified $1,347 and 400 sweep findings
    and sent nothing.
    """

    def _src(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "tracker" / "cli.py").read_text(encoding="utf-8")

    def test_an_empty_grid_is_no_longer_an_early_return(self):
        src = self._src()
        i = src.find("if not accepted:")
        assert i > 0
        block = src[i:i + 400]
        assert "return 0" not in block, "the grid can still veto the email"
        assert "continuing on Chrome" in block

    def test_it_still_stops_when_every_source_is_empty(self):
        src = self._src()
        assert "Nothing usable from any source this run." in src

    def test_the_email_renders_from_verified_alone(self):
        """No grid rows at all, and it still produces a full message."""
        from datetime import date
        from tracker import email_render
        from tracker.browser import BrowserOption
        from tracker.pricing import PriceBands

        opt = BrowserOption(
            price_usd=1347, origin="SJO", destination="TYO",
            depart_date=date(2027, 1, 29), return_date=date(2027, 2, 25),
            stops=("ZRH",), airlines=("SWISS",), total_minutes=2780,
            deep_link="https://www.google.com/travel/flights/x")
        bands = PriceBands(low=2213, high=3202, usual=2866, source="SEED",
                           seen_low=1347, seen_high=13127)
        mail = email_render.render([], bands, threshold=1400, is_great=False,
                                   generated_at="now", verified=[opt])
        assert email_render.GREETING in mail.html
        assert email_render.GREETING in mail.text
        assert "$1,347" in mail.subject
        assert "0 option" not in mail.subject, "it counted only the grid"

    def test_it_still_refuses_when_there_is_truly_nothing(self):
        import pytest as _pytest
        from tracker import email_render
        from tracker.pricing import PriceBands
        bands = PriceBands(low=2213, high=3202, usual=2866, source="SEED")
        with _pytest.raises(ValueError):
            email_render.render([], bands, threshold=1400, is_great=False,
                                generated_at="now", verified=[])
