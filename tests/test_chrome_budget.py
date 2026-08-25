"""Chrome launches are the most expensive request this project makes.

Measured 2026-08-24: 53 launches went to 18 distinct windows, and nine of
them took 44 - four or five checks each - while the background sweep was
re-pricing those same nine every ~10 hours on its hot tier. Two systems
doing the same work.

The swept price is already folded into the email and carries its own
"checked N hr ago" label, so letting it stand loses nothing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pathlib

from tracker.cli import BLOCKED_MIN_CHROME, _recently_swept

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "tracker" / "cli.py").read_text(encoding="utf-8")


class Cfg:
    def __init__(self, hours=3.0, store="nope.json"):
        self.chrome_skip_if_swept_hours = hours
        self.sweep_store = store


class TestRecentlySwept:
    def test_a_missing_store_skips_nothing(self, tmp_path):
        assert _recently_swept(Cfg(store=str(tmp_path / "none.json"))) == set()

    def test_zero_hours_disables_the_skip(self, tmp_path):
        """The escape hatch: verify everything, exactly as before."""
        store = tmp_path / "s.json"
        store.write_text("{}", encoding="utf-8")
        assert _recently_swept(Cfg(hours=0.0, store=str(store))) == set()

    def test_a_corrupt_store_skips_nothing(self, tmp_path):
        """Never let an unreadable file cost the run its verification."""
        store = tmp_path / "s.json"
        store.write_text("0", encoding="utf-8")
        assert _recently_swept(Cfg(store=str(store))) == set()

    def test_a_fresh_finding_is_returned(self, tmp_path):
        import json
        from datetime import datetime, timezone
        store = tmp_path / "s.json"
        now = datetime.now(timezone.utc).isoformat()
        store.write_text(json.dumps({
            "version": _store_version(),
            "found": {"2027-01-29_2027-02-25": {
                "depart": "2027-01-29", "ret": "2027-02-25",
                "price_usd": 1347, "origin": "SJO", "destination": "TYO",
                "stops": ["ZRH"], "airlines": ["SWISS"],
                "total_minutes": 2780, "deep_link": "", "seen_at": now}},
        }), encoding="utf-8")
        assert _recently_swept(Cfg(store=str(store))) == {
            "2027-01-29_2027-02-25"}

    def test_a_stale_finding_is_not(self, tmp_path):
        """A stopped sweep must not silently suppress verification."""
        import json
        store = tmp_path / "s.json"
        store.write_text(json.dumps({
            "version": _store_version(),
            "found": {"2027-01-29_2027-02-25": {
                "depart": "2027-01-29", "ret": "2027-02-25",
                "price_usd": 1347, "origin": "SJO", "destination": "TYO",
                "stops": ["ZRH"], "airlines": ["SWISS"],
                "total_minutes": 2780, "deep_link": "",
                "seen_at": "2020-01-01T00:00:00+00:00"}},
        }), encoding="utf-8")
        assert _recently_swept(Cfg(store=str(store))) == set()


def _store_version():
    from tracker.sweeper import STORE_VERSION
    return STORE_VERSION


class TestTheSkipCannotBlindTheBlockAlarm:
    """`run_looks_blocked` needs three launches to tell a blackout apart."""

    def test_the_floor_is_enforced_in_the_run(self):
        i = SRC.find("if fresh_swept:")
        assert i > 0, "the skip is gone"
        block = SRC[i:i + 700]
        assert "BLOCKED_MIN_CHROME" in block, (
            "the skip no longer protects the blackout sample")

    def test_the_floor_is_still_three(self):
        assert BLOCKED_MIN_CHROME == 3

    def test_it_reports_what_it_skipped(self):
        assert "Skipping %d window(s) the sweep priced within" in SRC
