"""The endless sweep: resumability, bounded storage, and never dying.

No browser is launched and no network is touched - `fetch` is injected.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tracker.browser import BrowserOption
from tracker.schedule import Window
from tracker.sweeper import (
    Discovery, SweepStore, sweep_batch,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "chrome_dom_32n.html")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def opt(price=1347, depart=date(2027, 1, 29), ret=date(2027, 2, 25),
        stops=("ZRH",)):
    return BrowserOption(
        price_usd=price, origin="SJO", destination="TYO",
        depart_date=depart, return_date=ret, stops=stops,
        airlines=("Edelweiss Air", "SWISS"), total_minutes=2780,
        deep_link="https://example.com/x")


def windows(n=5):
    base = date(2027, 1, 1)
    return [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
            for i in range(n)]


class FakeChrome:
    def __init__(self, dom, fail_on=()):
        self.dom, self.calls, self.fail_on = dom, 0, set(fail_on)

    def __call__(self, url):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("chrome exploded")
        return self.dom


@pytest.fixture
def dom():
    return io.open(FIXTURE, encoding="utf-8").read()


class TestStorePersistence:
    def test_missing_file_is_a_fresh_store(self, tmp_path):
        s = SweepStore.load(tmp_path / "nope.json")
        assert s.cursor == 0 and s.found == {}

    def test_corrupt_file_does_not_raise(self, tmp_path):
        p = tmp_path / "d.json"
        p.write_text("{not json", encoding="utf-8")
        assert SweepStore.load(p).cursor == 0

    def test_wrong_version_is_discarded(self, tmp_path):
        p = tmp_path / "d.json"
        p.write_text(json.dumps({"version": 999, "cursor": 50}), encoding="utf-8")
        assert SweepStore.load(p).cursor == 0

    def test_round_trip(self, tmp_path):
        p = tmp_path / "d.json"
        s = SweepStore(cursor=7)
        s.record(opt())
        s.save(p)
        back = SweepStore.load(p)
        assert back.cursor == 7 and len(back.found) == 1

    def test_save_is_atomic_leaving_no_temp_files(self, tmp_path):
        p = tmp_path / "d.json"
        SweepStore(cursor=1).save(p)
        SweepStore(cursor=2).save(p)
        assert [f.name for f in tmp_path.iterdir()] == ["d.json"]


class TestRecording:
    def test_first_sighting_is_kept(self):
        s = SweepStore()
        assert s.record(opt(1347)) is True
        assert s.found["2027-01-29_2027-02-25"]["price_usd"] == 1347

    def test_cheaper_replaces(self):
        s = SweepStore()
        s.record(opt(1500))
        assert s.record(opt(1200)) is True
        assert s.found["2027-01-29_2027-02-25"]["price_usd"] == 1200

    def test_dearer_does_not_replace(self):
        s = SweepStore()
        s.record(opt(1200))
        assert s.record(opt(1500)) is False
        assert s.found["2027-01-29_2027-02-25"]["price_usd"] == 1200

    def test_reseeing_the_same_price_refreshes_the_timestamp(self):
        """Otherwise a live fare ages out of the email while still on sale."""
        s = SweepStore()
        s.record(opt(1200))
        s.found["2027-01-29_2027-02-25"]["seen_at"] = "2020-01-01T00:00:00+00:00"
        s.record(opt(1200))
        assert s.found["2027-01-29_2027-02-25"]["seen_at"] != "2020-01-01T00:00:00+00:00"

    def test_separate_windows_are_separate_entries(self):
        s = SweepStore()
        s.record(opt(1347))
        s.record(opt(1500, depart=date(2027, 3, 1), ret=date(2027, 3, 28)))
        assert len(s.found) == 2


class TestPruning:
    def _aged(self, hours):
        return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")

    def test_old_entries_are_dropped(self):
        s = SweepStore()
        s.record(opt(1347))
        s.found["2027-01-29_2027-02-25"]["seen_at"] = self._aged(24 * 30)
        assert s.prune(now=NOW) == 1 and s.found == {}

    def test_fresh_entries_survive(self):
        s = SweepStore()
        s.record(opt(1347))
        s.found["2027-01-29_2027-02-25"]["seen_at"] = self._aged(2)
        assert s.prune(now=NOW) == 0 and len(s.found) == 1

    def test_store_stays_bounded_keeping_the_cheapest(self):
        s = SweepStore()
        base = date(2027, 1, 1)
        for i in range(50):
            s.record(opt(3000 - i, depart=base + timedelta(days=i),
                         ret=base + timedelta(days=i + 27)))
        s.prune(max_entries=10, now=NOW)
        assert len(s.found) == 10
        assert max(v["price_usd"] for v in s.found.values()) == 2960


class TestBest:
    def test_cheapest_first(self):
        s = SweepStore()
        base = date(2027, 1, 1)
        for i, price in enumerate((2000, 1200, 1600)):
            s.record(opt(price, depart=base + timedelta(days=i),
                         ret=base + timedelta(days=i + 27)))
        assert [d.price_usd for d in s.best(now=NOW)] == [1200, 1600, 2000]

    def test_threshold_filters(self):
        s = SweepStore()
        base = date(2027, 1, 1)
        for i, price in enumerate((2000, 1200)):
            s.record(opt(price, depart=base + timedelta(days=i),
                         ret=base + timedelta(days=i + 27)))
        assert [d.price_usd for d in s.best(threshold=1400, now=NOW)] == [1200]

    def test_stale_findings_are_hidden(self):
        s = SweepStore()
        s.record(opt(1200))
        s.found["2027-01-29_2027-02-25"]["seen_at"] = (
            NOW - timedelta(hours=100)).isoformat()
        assert s.best(now=NOW) == []

    def test_limit_applies(self):
        s = SweepStore()
        base = date(2027, 1, 1)
        for i in range(9):
            s.record(opt(1000 + i, depart=base + timedelta(days=i),
                         ret=base + timedelta(days=i + 27)))
        assert len(s.best(limit=4, now=NOW)) == 4

    def test_a_malformed_entry_is_skipped_not_fatal(self):
        s = SweepStore()
        s.record(opt(1200))
        s.found["junk"] = {"unexpected": True}
        assert len(s.best(now=NOW)) == 1


class TestSweeping:
    def test_cursor_advances_by_the_batch(self, dom):
        s = SweepStore()
        sweep_batch(windows(5), s, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor == 3 and s.windows_priced == 3

    def test_cursor_wraps_and_counts_a_pass(self, dom):
        s = SweepStore(cursor=4)
        sweep_batch(windows(5), s, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.passes_completed == 1
        assert s.cursor == 2

    def test_only_visa_free_options_are_stored(self, dom):
        """The fixture's cheapest row routes through Toronto."""
        s = SweepStore()
        sweep_batch(windows(1), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.found) == 1
        assert list(s.found.values())[0]["price_usd"] == 2652

    def test_a_failing_launch_does_not_stop_the_batch(self, dom):
        s = SweepStore()
        f = FakeChrome(dom, fail_on={2})
        sweep_batch(windows(5), s, batch=4, fetch=f,
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor == 4, "the sweep must move past a failure"

    def test_empty_dom_advances_without_storing(self):
        s = SweepStore()
        sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor == 3 and s.found == {}

    def test_no_windows_is_a_no_op(self, dom):
        s = SweepStore()
        assert sweep_batch([], s, fetch=FakeChrome(dom)) == 0

    def test_delay_is_honoured_between_launches(self, dom):
        naps = []
        sweep_batch(windows(3), SweepStore(), batch=3, fetch=FakeChrome(dom),
                    sleep=naps.append, delay_s=8.0)
        assert naps == [8.0, 8.0, 8.0]

    def test_on_find_fires_only_for_a_new_best(self, dom):
        seen = []
        s = SweepStore()
        sweep_batch(windows(2), s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, on_find=seen.append)
        assert len(seen) == 2          # two distinct windows
        s2 = SweepStore(cursor=0)
        sweep_batch(windows(2), s2, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        sweep_batch(windows(2), s2, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, on_find=seen.append)
        assert len(seen) == 2, "re-seeing the same price must not re-announce"

    def test_resuming_continues_rather_than_restarting(self, dom, tmp_path):
        p = tmp_path / "d.json"
        s = SweepStore()
        sweep_batch(windows(10), s, batch=4, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        s.save(p)
        again = SweepStore.load(p)
        assert again.cursor == 4
        sweep_batch(windows(10), again, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert again.cursor == 7


class TestProgress:
    def test_reads_sensibly(self):
        s = SweepStore(cursor=50, passes_completed=2)
        text = s.progress(4000)
        assert "50/4000" in text and "1.2%" in text and "2 pass" in text

    def test_zero_total_does_not_divide_by_zero(self):
        assert "0/0" in SweepStore().progress(0)


class TestSweepOrder:
    """Priority months first, or the first useful find is hours away.

    In plain date order the sweep starts eight months before the dates the
    trip owner cares about. At ~19s a window that is most of a day before
    it reaches January.
    """

    def _mixed(self):
        base = date(2026, 9, 1)
        general = [Window(base + timedelta(days=i), base + timedelta(days=i + 27))
                   for i in range(3)]
        pri = [Window(date(2027, 1, 1) + timedelta(days=i),
                      date(2027, 1, 28) + timedelta(days=i), priority=True)
               for i in range(2)]
        return general + pri

    def test_priority_windows_come_first(self):
        from tracker.sweeper import sweep_order
        got = sweep_order(self._mixed())
        assert [w.priority for w in got] == [True, True, False, False, False]

    def test_nothing_is_lost_or_duplicated(self):
        from tracker.sweeper import sweep_order
        src = self._mixed()
        got = sweep_order(src)
        assert sorted(w.key for w in got) == sorted(w.key for w in src)

    def test_date_order_survives_within_each_group(self):
        """A stable order is what makes the persisted cursor meaningful."""
        from tracker.sweeper import sweep_order
        got = sweep_order(self._mixed())
        pri = [w.depart for w in got if w.priority]
        gen = [w.depart for w in got if not w.priority]
        assert pri == sorted(pri) and gen == sorted(gen)

    def test_all_priority_is_unchanged(self):
        from tracker.sweeper import sweep_order
        src = [w for w in self._mixed() if w.priority]
        assert [w.key for w in sweep_order(src)] == [w.key for w in src]

    def test_empty_is_empty(self):
        from tracker.sweeper import sweep_order
        assert sweep_order([]) == []
