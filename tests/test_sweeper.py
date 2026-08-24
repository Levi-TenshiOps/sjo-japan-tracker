"""The endless sweep: resumability, bounded storage, and never dying.

No browser is launched and no network is touched - `fetch` is injected.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collections
import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tracker.browser import BrowserOption
from tracker.schedule import Window
from tracker import sweeper
from tracker.sweeper import (
    EMPTY_ALARM_WINDOW, Discovery, SweepStore, looks_throttled,
    next_window, sweep_batch,
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
        """A failure is re-queued once, so the cursor lags by that window."""
        s = SweepStore()
        f = FakeChrome(dom, fail_on={2})
        sweep_batch(windows(5), s, batch=4, fetch=f,
                    sleep=lambda _: None, delay_s=0)
        assert f.calls == 4, "the sweep must keep working after a failure"
        assert s.cursor >= 3

    def test_empty_dom_advances_and_stores_nothing(self):
        s = SweepStore()
        sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.found == {}
        assert s.cursor >= 1, "an all-empty run must still make progress"

    def test_an_all_empty_sweep_still_makes_progress(self):
        """A window must never be able to trap the sweep in a retry loop."""
        s = SweepStore()
        for _ in range(6):
            sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(""),
                        sleep=lambda _: None, delay_s=0)
        assert len(s.suspect) <= 3, "at most one entry per window"
        assert s.windows_priced >= 12

    def test_no_windows_is_a_no_op(self, dom):
        s = SweepStore()
        assert sweep_batch([], s, fetch=FakeChrome(dom)) == 0

    def test_delay_is_honoured_between_launches(self, dom):
        """Jittered around the configured value, never a metronome."""
        naps = []
        sweep_batch(windows(3), SweepStore(), batch=3, fetch=FakeChrome(dom),
                    sleep=naps.append, delay_s=8.0)
        assert len(naps) == 3
        assert all(6.0 <= n <= 10.0 for n in naps), naps

    def test_the_delay_is_not_a_metronome(self, dom):
        """A request every N seconds on a perfect clock is a fingerprint."""
        naps = []
        sweep_batch(windows(30), SweepStore(), batch=12, fetch=FakeChrome(dom),
                    sleep=naps.append, delay_s=8.0)
        assert len(set(naps)) > 1, "identical waits every time would stand out"

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


class TestSweepFeedsTheBaseline:
    """The price baseline should describe fares you can actually book.

    Google's own insights cover every routing it sells, including the US and
    Canadian transits this traveller cannot use: on 2026-08-23 it called
    $1,800 the usual price while the median across 259 verified visa-free
    observations was $2,346. The sweep sees far more of the market than the
    scheduled runs, so its rows are most of the corrective evidence.
    """

    def test_every_visa_free_option_is_logged_not_just_the_cheapest(self, dom, tmp_path):
        """A baseline is a distribution, so it needs the dear ones too."""
        import csv
        csv_path = tmp_path / "sweep_history.csv"
        sweep_batch(windows(1), SweepStore(), batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, history_csv=str(csv_path))
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        assert len(rows) == 1, "the fixture holds one visa-free option"
        assert int(rows[0]["price_usd"]) == 2652

    def test_rows_are_marked_as_chrome_sourced(self, dom, tmp_path):
        """read_prices filters on this to exclude the inflated HTTP rows."""
        import csv
        csv_path = tmp_path / "sweep_history.csv"
        sweep_batch(windows(1), SweepStore(), batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, history_csv=str(csv_path))
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        assert rows[0]["band_source"] == "CHROME"

    def test_no_history_file_means_no_logging_and_no_crash(self, dom):
        s = SweepStore()
        sweep_batch(windows(2), s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, history_csv=None)
        assert s.cursor == 2

    def test_an_unwritable_path_does_not_stop_the_sweep(self, dom, tmp_path):
        """Logging is a side benefit; it must never cost coverage."""
        bad = tmp_path / "nope" / "deep" / "sweep.csv"
        s = SweepStore()
        sweep_batch(windows(2), s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, history_csv=str(bad))
        assert s.cursor == 2

    def test_rows_accumulate_across_batches(self, dom, tmp_path):
        import csv
        csv_path = tmp_path / "sweep_history.csv"
        s = SweepStore()
        for _ in range(3):
            sweep_batch(windows(3), s, batch=1, fetch=FakeChrome(dom),
                        sleep=lambda _: None, delay_s=0, history_csv=str(csv_path))
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        assert len(rows) == 3


class TestResumingSurvivesTheListShifting:
    """The window list is rebuilt daily, so an index is not a stable address.

    Each day the earliest departure falls out of the rolling 8-month span
    and a new one appears at the end - measured at 18 off the front and 18
    onto the back. Everything after the removed ones shifts down by 18, so a
    numeric cursor silently skips 18 windows on that pass.
    """

    def _list(self, start_day, n=12):
        return [Window(start_day + timedelta(days=i),
                       start_day + timedelta(days=i + 27)) for i in range(n)]

    def test_resumes_at_the_window_after_the_last_finished(self):
        from tracker.sweeper import resume_index
        ws = self._list(date(2027, 1, 1))
        s = SweepStore(cursor=99, last_key=ws[4].key)
        assert resume_index(ws, s) == 5

    def test_a_shifted_list_still_resumes_at_the_right_window(self):
        """The whole point: same window, different index."""
        from tracker.sweeper import resume_index
        today = self._list(date(2027, 1, 1))
        target = today[6]
        # Tomorrow: three windows dropped off the front.
        tomorrow = today[3:] + self._list(date(2027, 1, 13), 3)
        s = SweepStore(cursor=7, last_key=target.key)
        i = resume_index(tomorrow, s)
        assert tomorrow[i - 1].key == target.key, "must land after the same window"
        assert i != 7, "a raw index would have skipped three windows"

    def test_falls_back_to_the_index_when_the_window_expired(self):
        from tracker.sweeper import resume_index
        ws = self._list(date(2027, 1, 1))
        s = SweepStore(cursor=4, last_key="1999-01-01_1999-01-28")
        assert resume_index(ws, s) == 4

    def test_no_key_yet_uses_the_index(self):
        from tracker.sweeper import resume_index
        assert resume_index(self._list(date(2027, 1, 1)), SweepStore(cursor=3)) == 3

    def test_finishing_the_last_window_wraps_to_the_start(self):
        from tracker.sweeper import resume_index
        ws = self._list(date(2027, 1, 1))
        s = SweepStore(last_key=ws[-1].key)
        assert resume_index(ws, s) == 0

    def test_an_index_past_the_end_is_clamped(self):
        from tracker.sweeper import resume_index
        ws = self._list(date(2027, 1, 1), 5)
        assert resume_index(ws, SweepStore(cursor=900)) == 4

    def test_sweeping_records_the_key_it_finished(self, dom):
        s = SweepStore()
        ws = self._list(date(2027, 1, 1))
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.last_key == ws[2].key

    def test_the_key_survives_a_save_and_reload(self, dom, tmp_path):
        p = tmp_path / "d.json"
        s = SweepStore()
        ws = self._list(date(2027, 1, 1))
        sweep_batch(ws, s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        s.save(p)
        assert SweepStore.load(p).last_key == ws[1].key


class TestThrottleDetection:
    """A run of empty windows is throttling, not an absence of fares.

    Measured 2026-08-23: pricing windows from a second process at the same
    time took the hit rate from 87% to 24%, and the throttled responses came
    back in 3-4s where a real page takes ~6s. The sweep could not tell the
    two apart, so it recorded a false "no fares", advanced, and did not look
    at that window again for a whole pass.
    """

    def test_a_normal_empty_rate_is_not_an_alarm(self):
        from tracker.sweeper import looks_throttled
        # 13% empty is the measured, genuine rate.
        assert not looks_throttled([0] * 17 + [1] * 3)

    def test_a_run_of_empties_is_an_alarm(self):
        from tracker.sweeper import looks_throttled
        assert looks_throttled([1] * 20)

    def test_too_little_data_never_alarms(self):
        """Three empty windows at startup must not trigger a backoff."""
        from tracker.sweeper import looks_throttled
        assert not looks_throttled([1, 1, 1])

    def test_the_threshold_sits_between_the_measured_rates(self):
        from tracker.sweeper import looks_throttled
        healthy = [1] * 3 + [0] * 17          # 15% empty, like the real sweep
        throttled = [1] * 15 + [0] * 5        # 75% empty, like the incident
        assert not looks_throttled(healthy)
        assert looks_throttled(throttled)

    def test_only_the_recent_sample_counts(self):
        """Recovery must clear the alarm, not be outvoted by old history."""
        from tracker.sweeper import looks_throttled
        assert not looks_throttled([1] * 100 + [0] * 20)

    def test_suspicious_empties_are_queued_for_a_second_look(self):
        s = SweepStore()
        sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.suspect, "a fast empty is not trustworthy and must be re-checked"

    def test_a_window_with_fares_is_never_queued(self, dom):
        s = SweepStore()
        sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.suspect == []

    def test_the_delay_grows_while_throttled(self, dom):
        naps = []
        s = SweepStore(recent=[1] * 40)
        sweep_batch(windows(2), s, batch=1, fetch=FakeChrome(""),
                    sleep=naps.append, delay_s=6.0)
        assert naps and naps[0] > 12.0, "a throttled sweep must slow down hard"

    def test_the_delay_is_normal_when_healthy(self, dom):
        naps = []
        sweep_batch(windows(2), SweepStore(), batch=1, fetch=FakeChrome(dom),
                    sleep=naps.append, delay_s=6.0)
        assert len(naps) == 1 and 4.5 <= naps[0] <= 7.5

    def test_recent_does_not_grow_without_bound(self, dom):
        s = SweepStore()
        for _ in range(20):
            sweep_batch(windows(5), s, batch=5, fetch=FakeChrome(dom),
                        sleep=lambda _: None, delay_s=0)
        assert len(s.recent) <= 40


class TestRecoveryAfterThrottling:
    """A throttled window is unverified, not empty, and must be re-checked.

    A plain retry counter was not enough: if the throttle lasts an hour, the
    retry is throttled too and the window is written off on the strength of
    a second false answer.
    """

    def test_suspects_are_not_re_checked_while_still_throttled(self):
        """Re-checking mid-throttle just collects another false empty."""
        s = SweepStore(recent=[1] * 40, suspect=["2027-01-01_2027-01-28"])
        before = list(s.suspect)
        sweep_batch(windows(3), s, batch=1, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.suspect[:1] == before[:1], "must wait for a healthy stretch"

    def test_suspects_are_re_checked_once_healthy(self, dom):
        ws = windows(5)
        s = SweepStore(recent=[0] * 40, suspect=[ws[4].key])
        sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert ws[4].key not in s.suspect, "healthy re-check clears the doubt"

    def test_a_re_check_does_not_advance_the_cursor(self, dom):
        """Replays must not consume progress through the main list."""
        ws = windows(5)
        s = SweepStore(recent=[0] * 40, suspect=[ws[3].key], cursor=1,
                       last_key=ws[0].key)
        sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor == 1

    def test_a_re_check_that_finds_fares_stores_them(self, dom):
        ws = windows(5)
        s = SweepStore(recent=[0] * 40, suspect=[ws[2].key])
        sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.found, "the fare that throttling hid must end up recorded"

    def test_an_expired_suspect_is_dropped_not_looped_on(self):
        s = SweepStore(recent=[0] * 40, suspect=["1999-01-01_1999-01-28"])
        sweep_batch(windows(3), s, batch=2, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert "1999-01-01_1999-01-28" not in s.suspect

    def test_throttle_events_are_counted_for_reporting(self):
        s = SweepStore(recent=[1] * 40)
        sweep_batch(windows(3), s, batch=1, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.throttle_events == 1 and s.throttled_since

    def test_health_says_when_it_is_throttled(self):
        s = SweepStore(recent=[1] * 20, throttled_since="2026-08-23T14:00:00+00:00")
        assert "THROTTLED NOW" in s.health()

    def test_health_is_quiet_when_all_is_well(self):
        s = SweepStore(recent=[0] * 20)
        text = s.health()
        assert "THROTTLED" not in text and "empty rate 0%" in text


class TestSweepRespectsTheLock:
    """The sweep must never query Google while a scheduled run is doing so."""

    def test_the_lock_is_taken_per_window(self, dom, tmp_path):
        """Per window, not per run: a scheduled run waits ~12s, not 14 hours."""
        lock = tmp_path / "google.lock"
        seen = []

        def watching_fetch(url):
            seen.append(lock.exists())
            return dom

        sweep_batch(windows(3), SweepStore(), batch=3, fetch=watching_fetch,
                    sleep=lambda _: None, delay_s=0, lock_path=str(lock))
        assert seen == [True, True, True], "every fetch must hold the lock"

    def test_the_lock_is_released_between_windows(self, dom, tmp_path):
        lock = tmp_path / "google.lock"
        sweep_batch(windows(2), SweepStore(), batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, lock_path=str(lock))
        assert not lock.exists(), "a held lock would block the scheduled runs"

    def test_a_fetch_failure_still_releases_it(self, dom, tmp_path):
        lock = tmp_path / "google.lock"
        sweep_batch(windows(3), SweepStore(), batch=2,
                    fetch=FakeChrome(dom, fail_on={1}),
                    sleep=lambda _: None, delay_s=0, lock_path=str(lock))
        assert not lock.exists()


class TestEscalatingRest:
    """A fixed rest just cycles: rest, resume, get throttled, rest again.

    Observed 2026-08-23 doing exactly that for forty minutes. Each rest that
    fails to clear the throttle now doubles the next, to an hour.
    """

    def test_the_ladder_doubles_then_caps(self):
        from tracker.sweeper import THROTTLE_REST_MAX, THROTTLE_REST_SECONDS
        rests = [min(THROTTLE_REST_SECONDS * (2 ** n), THROTTLE_REST_MAX)
                 for n in range(5)]
        assert rests == [900, 1800, 3600, 3600, 3600]

    def test_a_healthy_stretch_resets_the_ladder(self, dom):
        """Otherwise one bad afternoon leaves it resting an hour forever."""
        s = SweepStore(recent=[0] * 40, throttled_since="2026-08-23T14:00:00+00:00",
                       consecutive_rests=3)
        sweep_batch(windows(2), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.consecutive_rests == 0
        assert s.throttled_since == ""

    def test_rests_are_counted(self):
        s = SweepStore(recent=[1] * 40,
                       throttled_since="2020-01-01T00:00:00+00:00")
        sweep_batch(windows(3), s, batch=1, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.consecutive_rests >= 1


class TestPersistentChromeProfile:
    """A fresh profile per launch is a louder bot signal than the rate.

    Without --user-data-dir, a 4,000-window sweep looks like four thousand
    brand-new browsers from one address, each running one search and never
    returning. No pacing fixes that.
    """

    def test_the_profile_flag_is_passed(self, tmp_path, monkeypatch):
        import tracker.browser as browser
        seen = {}

        class FakeRun:
            stdout = "<html></html>"

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return FakeRun()

        monkeypatch.setattr(browser.subprocess, "run", fake_run)
        browser.fetch_dom("https://x", chrome="chrome.exe",
                          profile_dir=str(tmp_path / "prof"))
        assert any(a.startswith("--user-data-dir=") for a in seen["cmd"])

    def test_the_profile_directory_is_created(self, tmp_path, monkeypatch):
        import tracker.browser as browser

        class FakeRun:
            stdout = ""

        monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: FakeRun())
        prof = tmp_path / "prof"
        browser.fetch_dom("https://x", chrome="chrome.exe", profile_dir=str(prof))
        assert prof.exists()

    def test_opting_out_sends_no_profile_flag(self, monkeypatch):
        import tracker.browser as browser
        seen = {}

        class FakeRun:
            stdout = ""

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return FakeRun()

        monkeypatch.setattr(browser.subprocess, "run", fake_run)
        browser.fetch_dom("https://x", chrome="chrome.exe", profile_dir=None)
        assert not any(a.startswith("--user-data-dir=") for a in seen["cmd"])

    def test_the_url_stays_last(self, monkeypatch):
        """Chrome takes the URL positionally; a flag after it is ignored."""
        import tracker.browser as browser
        seen = {}

        class FakeRun:
            stdout = ""

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return FakeRun()

        monkeypatch.setattr(browser.subprocess, "run", fake_run)
        browser.fetch_dom("https://example.com/q", chrome="chrome.exe",
                          profile_dir=None)
        assert seen["cmd"][-1] == "https://example.com/q"


class TestHotAndColdTiering:
    """Most windows can never produce an alert; spend the budget accordingly.

    Measured 2026-08-23 over 400 priced windows: 1% at or under the $1,400
    alert threshold, 4% at or under $1,600, all of them in January and
    February. Treating all 4,014 equally spent ~96% of the requests on dates
    that cannot matter - and that spend is what got the address throttled.
    """

    def _stocked(self, ws):
        s = SweepStore()
        for n, w in enumerate(ws):
            s.record(opt(1300 + n * 400, depart=w.depart, ret=w.back))
        return s

    def test_no_history_means_everything_is_cold(self):
        from tracker.sweeper import hot_keys
        assert hot_keys(SweepStore()) == []

    def test_hot_is_anchored_on_the_best_seen(self):
        """Relative to the cheapest fare, so it survives the market moving."""
        from tracker.sweeper import hot_keys
        ws = windows(4)
        s = self._stocked(ws)          # 1300, 1700, 2100, 2500
        # 1300 * 1.3 = 1690, so 1700 misses by ten dollars.
        assert len(hot_keys(s, multiple=1.3)) == 1
        # 1300 * 1.4 = 1820, which takes in 1700 but not 2100.
        assert len(hot_keys(s, multiple=1.4)) == 2

    def test_the_threshold_widens_the_net(self):
        """A fare under the alert price is hot even if the best is far below."""
        from tracker.sweeper import hot_keys
        ws = windows(4)
        s = self._stocked(ws)
        assert len(hot_keys(s, multiple=1.0, threshold=2200)) == 3

    def test_hot_is_ordered_cheapest_first(self):
        from tracker.sweeper import hot_keys
        s = self._stocked(windows(4))
        prices = [s.found[k]["price_usd"] for k in hot_keys(s, threshold=9999)]
        assert prices == sorted(prices)

    def test_an_unpriced_window_is_never_skipped(self):
        """It might be the cheapest there is; skipping it would blind us.

        Weakened deliberately on 2026-08-23 from "taken immediately" to
        "never skipped". A hot or warm launch may now come first, but the
        cold cursor does not advance on those, so the unpriced window is
        still the next cold pick. Delayed by a launch or two; never lost.

        The immediate version was what blocked the warm tier: it returned
        early whenever the cold window was unpriced, which 37% into a first
        pass is nearly every launch.
        """
        from tracker.sweeper import next_window
        ws = windows(5)
        s = self._stocked(ws[:2])
        s.cursor = 4                     # points at an unpriced window
        seen = set()
        for _ in range(12):
            w, special = next_window(ws, s)
            seen.add(w.key)
            if not special:
                s.cursor += 1
            s.windows_priced += 1
        assert ws[4].key in seen, "the unpriced window must still be reached"

    def test_the_cursor_does_not_advance_past_an_unpriced_window(self):
        """What makes 'never skipped' true rather than hopeful."""
        from tracker.sweeper import next_window
        ws = windows(5)
        s = self._stocked(ws[:2])
        s.cursor = 4
        w, special = next_window(ws, s)
        if special:
            assert s.cursor == 4, "a hot/warm pick must leave the cursor alone"

    def test_the_cold_rotation_still_advances(self, dom):
        """Only chasing cheap windows would never find a new bargain."""
        ws = windows(12)
        s = self._stocked(ws)
        s.cursor = 0
        before = s.cursor
        sweep_batch(ws, s, batch=8, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor > before, "coverage must keep moving"

    def test_hot_windows_do_not_consume_the_cold_cursor(self, dom):
        from tracker.sweeper import next_window
        ws = windows(12)
        s = self._stocked(ws)
        s.cursor = 3
        s.windows_priced = 0             # forces a hot pick on the interleave
        w, was_hot = next_window(ws, s, hot_share=0.5)
        if was_hot:
            assert w.key != ws[3].key or len(ws) == 1

    def test_the_hot_set_is_rotated_not_repeated(self):
        """Always taking the cheapest would leave the rest of it stale."""
        from tracker.sweeper import next_window
        ws = windows(8)
        s = self._stocked(ws)
        picked = set()
        for n in range(0, 40, 4):
            s.windows_priced = n
            w, was_hot = next_window(ws, s, threshold=9999, hot_share=0.25)
            if was_hot:
                picked.add(w.key)
        assert len(picked) > 1, "the whole hot set should come round"

    def test_empty_window_list_is_handled(self):
        from tracker.sweeper import next_window
        assert next_window([], SweepStore()) == (None, False)


class TestTheStoreIsReadableWhileTheSweepRuns:
    """`--status` reads the store file, so the file must not lag the sweep.

    Found 2026-08-23. Asked "are we still throttled?" mid-throttle, --status
    answered from a 91-minute-old snapshot: the store was written once per
    batch of 25, which is ~40 minutes of pricing and far longer once the
    throttle rests kick in. The health line was therefore stalest exactly
    when it mattered, and reported a throttle that the rests had already
    started clearing.
    """

    def test_the_store_is_written_during_a_batch_not_only_after(self, dom):
        """The cursor on disk must move while the batch is still running."""
        seen = []

        class Watcher(SweepStore):
            def save(self, path=None):        # noqa: D102
                seen.append(self.cursor)

        s = Watcher()
        sweep_batch(windows(5), s, batch=5, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, save_to="ignored.json")
        assert seen == [1, 2, 3, 4, 5], seen

    def test_without_save_to_nothing_is_written(self, dom):
        """Callers that manage their own saving must not be surprised."""
        seen = []

        class Watcher(SweepStore):
            def save(self, path=None):        # noqa: D102
                seen.append(self.cursor)

        sweep_batch(windows(3), Watcher(), batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert seen == []

    def test_the_file_on_disk_tracks_the_sweep(self, dom, tmp_path):
        store_path = tmp_path / "discoveries.json"
        s = SweepStore()
        sweep_batch(windows(4), s, batch=4, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, save_to=store_path)
        assert SweepStore.load(store_path).cursor == 4

    def test_a_read_only_store_path_does_not_kill_the_sweep(self, dom):
        """Losing a status update is survivable; stopping the sweep is not."""
        class Broken(SweepStore):
            def save(self, path=None):        # noqa: D102
                raise OSError("disk full")

        s = Broken()
        priced = sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(dom),
                             sleep=lambda _: None, delay_s=0,
                             save_to="anywhere.json")
        assert priced == 3


class TestCtrlCWorksDuringAThrottleRest:
    """Ctrl-C must stop the sweep even mid-rest.

    Found 2026-08-23 by the trip owner. The sweep was resting 30 minutes
    after a throttle; they pressed Ctrl-C twice, saw "Stop requested;
    finishing the current window then saving." twice, and the process kept
    running. `sweep_forever` installs a SIGINT handler that only sets a
    flag, so Python ran the handler and went straight back to sleeping the
    remainder of one flat `sleep(1800)` - and the flag is not read until
    `sweep_batch` returns, which it would not do for another 20 minutes.
    """

    def test_a_rest_is_slept_in_slices(self):
        naps = []
        stopped = sweeper.rest_in_slices(naps.append, 60.0, lambda: False,
                                         step=5.0)
        assert stopped is False
        assert sum(naps) == pytest.approx(60.0)
        assert len(naps) == 12, "one flat sleep cannot notice a stop request"

    def test_a_rest_ends_early_when_asked_to_stop(self):
        naps = []
        flag = {"stop": False}

        def should_stop():
            # Ask to stop once a little time has passed, as a signal would.
            if sum(naps) >= 10.0:
                flag["stop"] = True
            return flag["stop"]

        assert sweeper.rest_in_slices(naps.append, 3600.0, should_stop,
                                      step=5.0) is True
        assert sum(naps) < 3600.0, "it must not sleep the whole hour"
        assert sum(naps) <= 15.0

    def test_no_stop_callback_still_sleeps_the_whole_rest(self):
        naps = []
        assert sweeper.rest_in_slices(naps.append, 900.0, None) is False
        assert naps == [900.0]

    def test_the_batch_stops_between_windows_when_asked(self, dom):
        s = SweepStore()
        priced = sweep_batch(windows(10), s, batch=10, fetch=FakeChrome(dom),
                             sleep=lambda _: None, delay_s=0,
                             should_stop=lambda: True)
        assert priced == 0, "a stop before the first window must price none"

    def test_the_batch_runs_normally_without_a_stop_callback(self, dom):
        """The default must not change behaviour for existing callers."""
        s = SweepStore()
        assert sweep_batch(windows(4), s, batch=4, fetch=FakeChrome(dom),
                           sleep=lambda _: None, delay_s=0) == 4

    def test_stopping_partway_keeps_what_was_already_priced(self, dom):
        seen = {"n": 0}

        def should_stop():
            seen["n"] += 1
            return seen["n"] > 3      # let a couple through, then stop

        s = SweepStore()
        priced = sweep_batch(windows(10), s, batch=10, fetch=FakeChrome(dom),
                             sleep=lambda _: None, delay_s=0,
                             should_stop=should_stop)
        assert 0 < priced < 10
        assert s.cursor == priced, "the cursor must still be saveable"


class TestARestartJudgesTheConnectionFresh:
    """Health samples describe a moment, and the moment passes.

    The trip owner is told to stop the sweep when Google throttles and
    restart it later. Without this, doing exactly that is punished: `recent`
    persists, `looks_throttled` reads it before a single request is made,
    and the sweep starts in 4x backoff - one window every six minutes - on a
    connection that has had all night to recover. Clearing the verdict then
    takes 20 fresh windows, i.e. two hours.
    """

    def _stale(self, hours):
        when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # 1 means "came back empty" - that is the throttle signal.
        return SweepStore(recent=[1] * 20, throttled_since=when,
                          consecutive_rests=3, last_active=when)

    def test_old_samples_are_forgotten(self):
        s = self._stale(12)
        assert s.forget_stale_health() is True
        assert s.recent == [] and s.throttled_since == ""
        assert s.consecutive_rests == 0

    def test_a_sweep_that_only_just_stopped_keeps_its_verdict(self):
        """A restart seconds later must not wipe a real, current throttle."""
        s = self._stale(0.01)
        assert s.forget_stale_health() is False
        assert len(s.recent) == 20 and s.throttled_since

    def test_findings_are_never_touched(self, dom):
        """A price is still a price; only the connection verdict goes stale."""
        s = self._stale(12)
        sweep_batch(windows(1), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        before = dict(s.found)
        s.last_active = (datetime.now(timezone.utc)
                         - timedelta(hours=12)).isoformat()
        s.forget_stale_health()
        assert s.found == before and before != {}

    def test_a_clean_store_is_left_alone(self):
        s = SweepStore()
        assert s.forget_stale_health() is False

    def test_forgetting_makes_the_sweep_look_healthy_again(self):
        """The point of the exercise."""
        from tracker.sweeper import looks_throttled
        s = self._stale(12)
        assert looks_throttled(s.recent) is True
        s.forget_stale_health()
        assert looks_throttled(s.recent) is False


class TestTheHotShareIsDerivedNotFixed:
    """Spend on freshness what freshness needs, and no more.

    HOT_SHARE was a flat 0.25, chosen as an upper bound - CLAUDE.md argues
    only that *more* than a quarter buys nothing. Measured 2026-08-23 it is
    also far more than is needed: 41 hot windows at a 10-hour freshness
    limit need 4.1 launches an hour, and a 90-second delay supplies 37.5, so
    a quarter of them was 9.4 an hour. More than half the hot budget was
    re-pricing windows nowhere near going stale, and every one of those was
    a cold window not covered.
    """

    def test_the_measured_case(self):
        """41 hot windows at 90s: 11%, not 25%."""
        share = sweeper.needed_hot_share(41, cycle_s=96.1, freshness_hours=10.0)
        assert 0.10 <= share <= 0.12, share

    def test_a_faster_sweep_needs_a_smaller_share(self):
        """The need is a rate, so more launches means a smaller fraction."""
        slow = sweeper.needed_hot_share(41, cycle_s=96.1)
        fast = sweeper.needed_hot_share(41, cycle_s=36.1)
        assert fast < slow

    def test_more_hot_windows_need_a_bigger_share(self):
        assert (sweeper.needed_hot_share(80, cycle_s=96.1)
                > sweeper.needed_hot_share(20, cycle_s=96.1))

    def test_it_never_exceeds_the_cap(self):
        """It may only ever spend less than the old fixed behaviour."""
        assert sweeper.needed_hot_share(10_000, cycle_s=96.1) == sweeper.HOT_SHARE
        assert sweeper.needed_hot_share(500, cycle_s=3600.0) <= sweeper.HOT_SHARE

    def test_no_hot_windows_means_no_hot_budget(self):
        assert sweeper.needed_hot_share(0, cycle_s=96.1) == 0.0

    def test_degenerate_inputs_do_not_explode(self):
        assert sweeper.needed_hot_share(41, cycle_s=0) == 0.0
        assert sweeper.needed_hot_share(41, cycle_s=96.1, freshness_hours=0) == 0.0

    @pytest.mark.parametrize("n_hot", [5, 20, 41, 60, 90])
    @pytest.mark.parametrize("delay", [90.0, 60.0, 45.0, 30.0])
    def test_every_hot_window_is_refreshed_before_it_goes_stale(self, n_hot, delay):
        """The property that actually matters, including the quantisation.

        `next_window` turns the share into an integer launch interval, so
        the check has to be done on that interval, not on the raw fraction -
        rounding it up would silently under-spend on freshness.
        """
        cycle, fresh = delay + sweeper.LAUNCH_SECONDS, 10.0
        share = sweeper.needed_hot_share(n_hot, cycle_s=cycle,
                                         freshness_hours=fresh)
        every = max(int(1 / max(share, 0.01)), 2)      # as next_window does
        launches = fresh * 3600 / cycle
        refreshed = launches / every
        assert refreshed >= n_hot, (
            f"{n_hot} hot windows, 1-in-{every} launches: only {refreshed:.0f} "
            f"re-priced in {fresh}h - some would go stale")


class TestTheWarmTier:
    """Most of the search space cannot hold a cheap fare, and we know which.

    Measured 2026-08-23 over 1,165 observations: every fare at or under
    $1,600 departed Friday or Monday, and the Edelweiss/SWISS Zurich
    routing that carries all of them appears only on Monday, Wednesday and
    Friday departures - 371 Tuesday and Thursday windows priced, not one
    Zurich routing among them. That is a flight schedule.

    Only ~10% of windows carry a weekday pair that has ever produced a
    cheap fare, and the sweep was spending 90% of its launches elsewhere.
    """

    def _store_with(self, price, depart, back):
        s = SweepStore()
        s.found[f"{depart}_{back}"] = {
            "depart": depart, "ret": back, "price_usd": price,
            "origin": "SJO", "destination": "TYO", "stops": ["ZRH"],
            "airlines": ["SWISS"], "total_minutes": 2780, "deep_link": "",
            "seen_at": datetime.now(timezone.utc).isoformat()}
        return s

    def test_a_cheap_fare_teaches_its_weekday_pair(self):
        # 2027-01-29 is a Friday, 2027-02-25 a Thursday.
        s = self._store_with(1347, "2027-01-29", "2027-02-25")
        assert sweeper.promising_weekday_pairs(s, threshold=1400) == {(4, 3)}

    def test_a_dear_fare_teaches_nothing(self):
        s = self._store_with(3200, "2027-01-29", "2027-02-25")
        assert sweeper.promising_weekday_pairs(s, threshold=1400) == set()

    def test_a_fare_just_over_the_line_still_counts(self):
        """WARM_PAIR_MULTIPLE: a $1,550 pair is worth watching at a $1,400
        target, because the pair is the signal, not the exact price."""
        s = self._store_with(1550, "2027-01-29", "2027-02-25")
        assert sweeper.promising_weekday_pairs(s, threshold=1400) == {(4, 3)}

    def test_no_threshold_means_no_warm_tier(self):
        s = self._store_with(1347, "2027-01-29", "2027-02-25")
        assert sweeper.promising_weekday_pairs(s, threshold=None) == set()

    def test_it_is_derived_not_hardcoded(self):
        """If the schedule moves to Tuesdays this must follow it.

        A literal {Mon, Wed, Fri} would be the same circular reasoning that
        produced the 'all cheap fares are in January' claim.
        """
        # 2027-01-26 is a Tuesday, 2027-02-23 a Tuesday.
        s = self._store_with(1347, "2027-01-26", "2027-02-23")
        assert sweeper.promising_weekday_pairs(s, threshold=1400) == {(1, 1)}

    def test_a_corrupt_row_does_not_break_the_derivation(self):
        s = self._store_with(1347, "2027-01-29", "2027-02-25")
        s.found["junk"] = {"depart": "not-a-date", "ret": "x", "price_usd": 1}
        assert sweeper.promising_weekday_pairs(s, threshold=1400) == {(4, 3)}

    def test_launches_concentrate_on_the_plausible_windows(self):
        """The whole point: 10% of the space, far more than 10% of the work."""
        ws = windows(140)
        s = self._store_with(1347, ws[0].depart.isoformat(),
                             ws[0].back.isoformat())
        pairs = sweeper.promising_weekday_pairs(s, threshold=1400)
        warm = {w.key for w in ws
                if (w.depart.weekday(), w.back.weekday()) in pairs}
        assert 0 < len(warm) < len(ws)
        hits = 0
        for _ in range(400):
            w, special = next_window(ws, s, threshold=1400, hot_share=0.11)
            if w.key in warm:
                hits += 1
            if not special:
                s.cursor += 1
            s.windows_priced += 1
        space = len(warm) / len(ws)
        assert hits / 400 > space * 2, (
            f"warm windows are {space:.0%} of the space but took only "
            f"{hits/400:.0%} of the launches")

    def test_the_cold_rotation_is_not_starved(self):
        """Chasing only plausible dates would never find a new pattern."""
        ws = windows(140)
        s = self._store_with(1347, ws[0].depart.isoformat(),
                             ws[0].back.isoformat())
        for _ in range(400):
            w, special = next_window(ws, s, threshold=1400, hot_share=0.11)
            if not special:
                s.cursor += 1
            s.windows_priced += 1
        assert s.cursor > 150, f"cold cursor barely moved: {s.cursor}"


class TestNoWindowIsQuietlyWrittenOff:
    """An empty answer must leave a trace, or a throttle erases evidence.

    Found 2026-08-23. A window that returned nothing wrote nothing to
    `sweep_history.csv` and nothing to `found`, so afterwards a genuine
    empty and a throttled one were indistinguishable - which is exactly the
    question that matters once a throttle clears. 1,440 of 1,673 walked
    windows were in that state, ~960 of them in January and February, the
    months holding every cheap fare found so far. None were queued for a
    second look.
    """

    def _ws(self, n=6):
        return windows(n)

    def test_every_check_is_recorded_even_when_empty(self, dom):
        ws = self._ws(3)
        s = SweepStore()
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.checked) == 3
        assert all(v["empty"] for v in s.checked.values())

    def test_a_find_is_recorded_as_not_empty(self, dom):
        ws = self._ws(1)
        s = SweepStore()
        sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        rec = s.checked[ws[0].key]
        assert rec["empty"] is False and rec["at"]

    def test_a_window_with_a_fare_never_needs_rechecking(self, dom):
        ws = self._ws(1)
        s = SweepStore()
        sweep_batch(ws, s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert sweeper.unverified_windows(ws, s) == []

    def test_an_empty_checked_while_unhealthy_is_unverified(self):
        ws = self._ws(2)
        s = SweepStore(cursor=2)
        s.checked = {ws[0].key: {"at": "2026-08-23T12:00:00+00:00",
                                 "empty": True, "healthy": False},
                     ws[1].key: {"at": "2026-08-23T12:00:00+00:00",
                                 "empty": True, "healthy": True}}
        assert sweeper.unverified_windows(ws, s) == [ws[0].key]

    def test_a_window_never_recorded_at_all_counts_as_unverified(self):
        """The pre-existing backlog: walked before the ledger existed."""
        ws = self._ws(3)
        s = SweepStore(cursor=3)
        assert sweeper.unverified_windows(ws, s) == [w.key for w in ws]

    def test_windows_beyond_the_cursor_are_not_claimed(self):
        """They have not been walked yet; they are not missing, just future."""
        ws = self._ws(6)
        s = SweepStore(cursor=2)
        assert len(sweeper.unverified_windows(ws, s)) == 2

    def test_queueing_adds_them_and_does_not_duplicate(self):
        ws = self._ws(3)
        s = SweepStore(cursor=3)
        assert sweeper.queue_unverified(ws, s) == 3
        assert sweeper.queue_unverified(ws, s) == 0, "must not re-add"
        assert len(s.suspect) == 3

    def test_the_recheck_drain_cannot_starve_cold_coverage(self, dom):
        """A 1,440-window backlog must not stop the rotation for a day.

        Draining one per launch would trade one blind spot for another.
        """
        ws = self._ws(40)
        s = SweepStore(cursor=0)
        s.suspect = [w.key for w in ws[:20]]
        s.found[ws[0].key] = {"depart": ws[0].depart.isoformat(),
                              "ret": ws[0].back.isoformat(), "price_usd": 1347,
                              "seen_at": datetime.now(timezone.utc).isoformat()}
        sweep_batch(ws, s, batch=20, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.cursor > 5, (
            f"cold cursor only reached {s.cursor}; the re-check backlog "
            f"starved the rotation")

    def test_the_ledger_stays_bounded(self, dom):
        s = SweepStore()
        s.checked = {f"k{i}": {"at": f"2026-08-23T00:00:{i % 60:02d}+00:00",
                               "empty": True, "healthy": True}
                     for i in range(sweeper.MAX_CHECKED + 50)}
        sweep_batch(windows(1), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.checked) <= sweeper.MAX_CHECKED


class TestEveryWindowIsAccountedFor:
    """The invariant the trip owner actually asked for.

    "Never leave a possible cheap flight untracked." Every window must be in
    exactly one of four states:

        1. beyond the cursor - not walked yet
        2. has a fare recorded in `found`
        3. checked on a healthy connection and genuinely empty
        4. queued for a re-check

    A window in none of them has been silently written off. That was the
    state of 1,440 windows on 2026-08-23 - ~960 of them in January and
    February - because an empty answer left no trace to reason about.
    """

    def _audit(self, ws, s):
        """(buckets, orphans) - orphans must always be empty."""
        queued = set(s.suspect)
        buckets = collections.Counter()
        orphans = []
        for i, w in enumerate(ws):
            if i >= s.cursor:
                buckets["future"] += 1
            elif w.key in s.found:
                buckets["found"] += 1
            elif w.key in queued:
                buckets["queued"] += 1
            elif s.checked.get(w.key, {}).get("healthy"):
                buckets["verified empty"] += 1
            else:
                orphans.append(w.key)
        return buckets, orphans

    def test_a_clean_sweep_leaves_nothing_unaccounted(self, dom):
        ws = windows(12)
        s = SweepStore()
        sweep_batch(ws, s, batch=12, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        _, orphans = self._audit(ws, s)
        assert orphans == []

    def test_an_all_empty_sweep_leaves_nothing_unaccounted(self, dom):
        """Empties are the dangerous case: they used to vanish."""
        ws = windows(12)
        s = SweepStore()
        sweep_batch(ws, s, batch=12, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        _, orphans = self._audit(ws, s)
        assert orphans == [], f"{len(orphans)} windows written off silently"

    def test_a_throttled_sweep_leaves_nothing_unaccounted(self, dom):
        """The real scenario: a long run of empties, then recovery.

        This is what happened on 2026-08-23 and what silently lost ~960
        January and February windows.
        """
        ws = windows(60)
        s = SweepStore()
        # A throttle: everything empty for a long stretch.
        for _ in range(3):
            sweep_batch(ws, s, batch=20, fetch=FakeChrome(""),
                        sleep=lambda _: None, delay_s=0)
        buckets, orphans = self._audit(ws, s)
        assert orphans == [], (
            f"{len(orphans)} of {len(ws)} windows silently written off after "
            f"a throttle; buckets={dict(buckets)}")

    def test_recovery_puts_the_throttled_ones_back_in_line(self, dom):
        ws = windows(40)
        s = SweepStore()
        sweep_batch(ws, s, batch=40, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        before = len(s.suspect)
        added = sweeper.queue_unverified(ws, s)
        assert added + before > 0, "nothing was queued after an all-empty pass"
        _, orphans = self._audit(ws, s)
        assert orphans == []

    def test_a_window_with_a_fare_is_never_queued(self, dom):
        """Re-checking a window we already priced would be wasted budget."""
        ws = windows(6)
        s = SweepStore()
        sweep_batch(ws, s, batch=6, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        sweeper.queue_unverified(ws, s)
        assert not (set(s.suspect) & set(s.found))

    def test_windows_past_the_cursor_are_never_queued(self):
        """They are not missing; they simply have not been reached."""
        ws = windows(20)
        s = SweepStore(cursor=5)
        sweeper.queue_unverified(ws, s)
        assert all(k in {w.key for w in ws[:5]} for k in s.suspect)


class TestTheBlindSpotAfterAThrottleRest:
    """The exact shape of the 2026-08-23 loss.

    Each throttle rest calls `store.recent.clear()` so the next stretch is
    judged fresh. That is deliberate - but it means `looks_throttled` reads
    False for the following 20 windows, so empties arriving in that gap were
    never flagged suspect. Most of that day fell into those gaps, and ~960
    January and February windows were written off as "no fares" while Google
    was in fact refusing to answer.

    The `checked` ledger closes it: a window is only trusted as empty when
    the check behind it was healthy, and a blind-spot check is not.
    """

    def test_blind_spot_empties_are_not_trusted(self, dom):
        ws = windows(30)
        s = SweepStore()
        # Simulate the gap directly: recent cleared, so nothing "looks"
        # throttled, and a run of empties goes straight through.
        s.recent.clear()
        sweep_batch(ws, s, batch=10, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        trusted = [k for k, v in s.checked.items()
                   if v["empty"] and v["healthy"]]
        assert trusted == [], (
            "an empty answer was recorded as trustworthy while the sweep "
            "could not tell whether the connection was healthy")

    def test_they_are_recoverable_afterwards(self, dom):
        ws = windows(30)
        s = SweepStore()
        s.recent.clear()
        sweep_batch(ws, s, batch=10, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert sweeper.unverified_windows(ws, s), (
            "the blind-spot windows must still be findable for a re-check")

    def test_a_healthy_empty_is_trusted_and_not_re_queued(self):
        """Precision: without this the sweep would re-check everything for
        ever and never make forward progress."""
        ws = windows(3)
        s = SweepStore(cursor=3)
        s.checked = {w.key: {"at": "2026-08-24T06:00:00+00:00",
                             "empty": True, "healthy": True} for w in ws}
        assert sweeper.unverified_windows(ws, s) == []
        assert sweeper.queue_unverified(ws, s) == 0


class TestTheBacklogDoesNotDuplicateTheColdPass:
    """The re-check queue and the cold rotation were doing the same job.

    Measured 2026-08-24: all 1,256 queued windows sat *behind* the cursor,
    so the pass was going to re-price every one of them anyway. Running both
    meant visiting them twice, and the quarter-share it took pushed a full
    cold pass from 4.9 days out to 8.2 - while the backlog was mostly
    January and February dates that have never produced a cheap fare, and
    October to December were still unexplored.
    """

    def test_pricing_a_window_clears_its_re_check(self, dom):
        """Whoever priced it, it is no longer owed a second look."""
        ws = windows(4)
        s = SweepStore()
        s.suspect = [w.key for w in ws]
        sweep_batch(ws, s, batch=4, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert not (set(s.suspect) & {w.key for w in ws[:s.cursor]}), (
            "a window was priced and still left in the re-check queue")

    def test_the_queue_shrinks_as_the_pass_proceeds(self, dom):
        ws = windows(12)
        s = SweepStore()
        s.suspect = [w.key for w in ws]
        before = len(s.suspect)
        sweep_batch(ws, s, batch=12, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.suspect) < before

    def test_the_backlog_no_longer_outranks_the_frontier(self):
        """One launch in eight, not one in four."""
        assert sweeper.RECHECK_EVERY == 8


class TestCoverageIsReportable:
    """The trip owner's question, answerable without reading the code.

    "A cheap price that lasts a day or two - do we get it?" For a fare
    persisting D days on a window revisited every R days the chance is
    ~min(1, D/R), so the answer is about R, and R differs per tier.
    """

    def _report(self):
        ws = windows(200)
        s = SweepStore()
        s.found[ws[0].key] = {
            "depart": ws[0].depart.isoformat(), "ret": ws[0].back.isoformat(),
            "price_usd": 1347, "origin": "SJO", "destination": "TYO",
            "stops": ["ZRH"], "airlines": ["SWISS"], "total_minutes": 2780,
            "deep_link": "", "seen_at": datetime.now(timezone.utc).isoformat()}
        return sweeper.coverage_report(ws, s, threshold=1400, delay_s=90.0)

    def test_it_names_every_tier(self):
        text = "\n".join(self._report())
        for tier in ("hot", "warm", "re-check backlog", "cold"):
            assert tier in text

    def test_it_answers_the_persistence_question(self):
        text = "\n".join(self._report())
        assert "a fare that lasts this long is caught" in text
        assert "1 day(s)" in text and "7 day(s)" in text

    def test_a_faster_sweep_reports_shorter_revisits(self):
        ws = windows(200)
        s = SweepStore()
        slow = sweeper.coverage_report(ws, s, threshold=1400, delay_s=90.0)
        fast = sweeper.coverage_report(ws, s, threshold=1400, delay_s=30.0)
        assert "899" in slow[0] and "2393" in fast[0].replace(",", "")

    def test_it_survives_an_empty_store(self):
        assert sweeper.coverage_report(windows(10), SweepStore(),
                                       threshold=1400)


class TestTheAlarmIsActuallyWiredUp:
    """The email builders had tests; the wiring did not.

    That is the half that breaks - `AlarmConfig.from_config` was already
    found sending to nobody because the sweep never passed `prefs`. These
    drive the real `sweep_batch` path rather than the pieces.
    """

    def _throttled_store(self):
        s = SweepStore()
        # Long enough ago that the rest branch is taken immediately.
        s.throttled_since = "2020-01-01T00:00:00+00:00"
        s.recent = [1] * 40
        return s

    def test_a_throttle_raises_the_blocked_alarm(self):
        fired = []
        s = self._throttled_store()
        sweep_batch(windows(40), s, batch=30, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0,
                    on_alarm=lambda k, f: fired.append((k, f)))
        assert [k for k, _ in fired].count("blocked") == 1

    def test_it_carries_the_facts_the_email_needs(self):
        """A missing key would raise inside the alarm and be swallowed."""
        from tracker import alarm
        fired = []
        s = self._throttled_store()
        sweep_batch(windows(40), s, batch=30, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0,
                    on_alarm=lambda k, f: fired.append((k, f)))
        kind, facts = fired[0]
        alarm.blocked_email(**facts)          # must not raise

    def test_it_does_not_re_alarm_for_the_same_throttle(self):
        """An escalating backoff cycles for hours; one email, not six."""
        fired = []
        s = self._throttled_store()
        for _ in range(4):
            sweep_batch(windows(40), s, batch=25, fetch=FakeChrome(""),
                        sleep=lambda _: None, delay_s=0,
                        on_alarm=lambda k, f: fired.append((k, f)))
        assert [k for k, _ in fired].count("blocked") == 1, [k for k, _ in fired]

    def test_recovery_raises_the_all_clear(self, dom):
        from tracker import alarm
        fired = []
        s = SweepStore()
        s.throttled_since = "2020-01-01T00:00:00+00:00"
        s.alarm_sent_for = s.throttled_since       # already told them
        s.recent = [0] * 40                        # healthy again
        sweep_batch(windows(5), s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0,
                    on_alarm=lambda k, f: fired.append((k, f)))
        kinds = [k for k, _ in fired]
        assert "recovered" in kinds, kinds
        alarm.recovered_email(**dict(fired[0][1]))     # must not raise
        assert s.alarm_sent_for == "", "the flag must reset for the next event"

    def test_a_second_throttle_alarms_again(self, dom):
        """Resetting the flag is what makes the next outage reportable."""
        s = SweepStore()
        s.throttled_since = "2020-01-01T00:00:00+00:00"
        s.alarm_sent_for = s.throttled_since
        s.recent = [0] * 40
        sweep_batch(windows(5), s, batch=2, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0,
                    on_alarm=lambda k, f: None)
        fired = []
        s2 = self._throttled_store()
        sweep_batch(windows(40), s2, batch=25, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0,
                    on_alarm=lambda k, f: fired.append((k, f)))
        assert [k for k, _ in fired].count("blocked") == 1

    def test_an_exploding_alarm_never_stops_the_sweep(self):
        """The sweep surviving matters more than the telling."""
        def boom(kind, facts):
            raise RuntimeError("smtp on fire")
        s = self._throttled_store()
        priced = sweep_batch(windows(40), s, batch=10, fetch=FakeChrome(""),
                             sleep=lambda _: None, delay_s=0, on_alarm=boom)
        assert priced > 0, "an alarm failure took the sweep down with it"

    def test_no_callback_is_simply_silent(self):
        s = self._throttled_store()
        assert sweep_batch(windows(40), s, batch=10, fetch=FakeChrome(""),
                           sleep=lambda _: None, delay_s=0) > 0


class TestNoInjectedCallbackCanStopTheSweep:
    """None of them does anything the sweep depends on.

    Found 2026-08-24 by an integration test on the alarm, then found twice
    more by asking the obvious follow-up: `on_find` and `should_stop` were
    unguarded too. Either would abort the batch, and the outer loop then
    pauses 60 seconds and loses the remaining windows.

    `on_find` is the realistic one: it formats a Discovery built from
    scraped data, so a malformed date is all it takes.
    """

    def boom(self, *args):
        raise RuntimeError("callback on fire")

    def test_on_find_cannot_stop_it(self, dom):
        s = SweepStore()
        assert sweep_batch(windows(6), s, batch=4, fetch=FakeChrome(dom),
                           sleep=lambda _: None, delay_s=0,
                           on_find=self.boom) == 4

    def test_should_stop_cannot_stop_it_by_raising(self, dom):
        """A raising stop-check must not read as 'stop', nor as fatal."""
        s = SweepStore()
        assert sweep_batch(windows(6), s, batch=4, fetch=FakeChrome(dom),
                           sleep=lambda _: None, delay_s=0,
                           should_stop=self.boom) == 4

    def test_on_alarm_cannot_stop_it(self):
        s = SweepStore()
        s.throttled_since = "2020-01-01T00:00:00+00:00"
        s.recent = [1] * 40
        assert sweep_batch(windows(40), s, batch=10, fetch=FakeChrome(""),
                           sleep=lambda _: None, delay_s=0,
                           on_alarm=self.boom) > 0

    def test_findings_are_still_recorded_when_on_find_explodes(self, dom):
        """The callback is for telling somebody; the data is the point."""
        s = SweepStore()
        sweep_batch(windows(4), s, batch=4, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, on_find=self.boom)
        assert s.found, "a broken notifier lost the fares it was notifying about"

    def test_a_working_callback_still_gets_called(self, dom):
        seen = []
        s = SweepStore()
        sweep_batch(windows(4), s, batch=4, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, on_find=seen.append)
        assert seen, "the guard must not swallow the call itself"


class TestTheInvariantIsHonestAboutWhatItKnows:
    """"Orphan" needs a definition that survives prune and a new ledger.

    The final review on 2026-08-24 reported 51 orphans where the morning had
    reported 0, and neither number was wrong - the definition was. Of the 51:

    * 21 had been checked and *had* produced fares, then fell out of `found`
      when `prune` kept only the cheapest 400. Correctly handled; the fare
      was logged to sweep_history, it is simply not worth remembering.
    * 30 were walked before the `checked` ledger existed at all, so there is
      no record either way.

    A window with no record predating the ledger is unknown, not lost. The
    distinction matters, because calling it lost hides the one case that
    really is: a queued key popped without ever being priced.
    """

    def test_a_pruned_window_is_not_lost(self, dom):
        """It was checked and it produced a fare. Not remembering a dear
        fare is the store working, not a coverage hole."""
        s = SweepStore()
        ws = windows(3)
        sweep_batch(ws, s, batch=3, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.found
        s.prune(max_entries=0)          # drop everything
        assert not s.found
        for w in ws[:s.cursor]:
            assert w.key in s.checked, (
                "the check record must outlive the finding it produced")

    def test_a_dropped_recheck_is_counted_not_silent(self):
        """The only way a queued window leaves without being priced."""
        s = SweepStore()
        ws = windows(4)
        s.suspect = ["1999-01-01_1999-02-01"]      # not a live window
        s.recent = [0] * 40                        # healthy, so re-checks run
        sweep_batch(ws, s, batch=8, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.dropped_rechecks >= 1
        assert "1999-01-01_1999-02-01" not in s.suspect

    def test_a_live_key_is_never_dropped(self, dom):
        s = SweepStore()
        ws = windows(4)
        s.suspect = [ws[2].key]
        s.recent = [0] * 40
        sweep_batch(ws, s, batch=8, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert s.dropped_rechecks == 0
        assert ws[2].key in s.checked, "it was queued, so it must be checked"

    def test_every_priced_window_gets_a_record(self, dom):
        """What makes the invariant exact once the ledger has a full pass."""
        s = SweepStore()
        ws = windows(10)
        sweep_batch(ws, s, batch=10, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.checked) == s.windows_priced


class TestReChecksDoNotPoisonTheHealthSample:
    """The sweep must not diagnose a throttle it caused itself.

    Windows land in the re-check queue *because* they came back empty. Feed
    those re-checks into `recent` and they raise the measured empty rate,
    which trips the throttle detector, which queues more windows, which
    raises it further.

    Live on 2026-08-24: the alarm emailed "70% empty" while Google was
    answering 15-16 options on most windows, the independent HTTP grid sat
    steady at 25%, and the sweep was logging fares the whole time. A
    1,262-window backlog draining at one launch in eight was most of the
    difference.

    `recent` measures the connection, so only a fresh pick is evidence.
    """

    def test_hot_and_warm_picks_still_count_as_evidence(self, dom):
        """They are fresh queries to Google, so they are evidence about it.

        The first version of this guard keyed off `replay`, which is also
        true for hot and warm picks, so the health sample collapsed to cold
        picks alone. With the cold cursor grinding through November
        Saturdays that read as 100% empty while hot picks in the very same
        minutes were logging "Google claims 17 results".
        """
        s = SweepStore()
        ws = windows(40)
        # Seed a finding so a hot list exists and hot picks actually happen.
        s.found[ws[0].key] = {
            "depart": ws[0].depart.isoformat(), "ret": ws[0].back.isoformat(),
            "price_usd": 1347, "origin": "SJO", "destination": "TYO",
            "stops": ["ZRH"], "airlines": ["SWISS"], "total_minutes": 2780,
            "deep_link": "", "seen_at": datetime.now(timezone.utc).isoformat()}
        sweep_batch(ws, s, batch=20, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, hot_share=0.25)
        assert len(s.recent) == s.windows_priced, (
            "with no re-checks queued, every priced window is fresh evidence "
            "and all of them should be in the health sample")

    def test_a_recheck_does_not_count_as_evidence(self):
        """Fewer health samples than windows priced: the difference is the
        re-checks, which are not evidence about the connection."""
        s = SweepStore()
        ws = windows(6)
        s.suspect = [ws[0].key, ws[1].key]
        s.recent = [0] * 20                    # healthy, so re-checks run
        before = len(s.recent)
        sweep_batch(ws, s, batch=8, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        grew = len(s.recent) - before
        assert grew < s.windows_priced, (
            f"all {s.windows_priced} priced windows fed the health sample; "
            f"re-checks should have been left out")

    def test_fresh_picks_still_count(self, dom):
        """The guard must not disable the detector altogether."""
        s = SweepStore()
        sweep_batch(windows(6), s, batch=6, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.recent) == 6
        assert sum(s.recent) == 6

    def test_a_real_throttle_is_still_detected(self):
        """Nothing about this weakens the case it exists for."""
        s = SweepStore()
        sweep_batch(windows(40), s, batch=30, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert looks_throttled(s.recent), "a genuine all-empty run must trip it"

    def test_an_all_recheck_batch_cannot_trip_it(self):
        """The feedback loop, reproduced: drain a backlog of known-empty
        windows and the detector must stay quiet."""
        s = SweepStore()
        ws = windows(30)
        s.suspect = [w.key for w in ws]
        s.recent = [0] * 20
        sweep_batch(ws, s, batch=20, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert not looks_throttled(s.recent[-EMPTY_ALARM_WINDOW:]) or \
            sum(s.recent[-EMPTY_ALARM_WINDOW:]) < EMPTY_ALARM_WINDOW, (
            "draining a backlog convinced the sweep it was throttled")


class TestTheDetectorMeasuresTheConnectionNotTheVisaRule:
    """A page full of US routings is not a page Google refused to send.

    The fixture's five rows are all real options and only one survives the
    visa rule, so it is exactly the shape that was being misread: Google
    answered fully, the visa filter emptied the list, and the sweep called
    that a throttle.

    Live on 2026-08-24: the alarm fired at 70% while the cold cursor was
    walking November *Saturdays* - which carry no Zurich routing at all, 0
    of 58 measured - and the warm picks in the same minutes were getting 16
    results each. CLAUDE.md had recorded the right discriminator since
    2026-08-22 and the detector was not using it.
    """

    def test_a_page_of_visa_rejected_options_is_not_empty(self, dom):
        """Four of the five fixture rows are US or Canadian routings."""
        s = SweepStore()
        sweep_batch(windows(1), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, max_total_hours=1)
        # max_total_hours=1 rejects every option, so nothing survives the
        # filters - but Google still answered.
        assert s.recent == [0], (
            "a full page was recorded as a connection failure")

    def test_a_genuinely_empty_page_still_counts_as_empty(self):
        s = SweepStore()
        sweep_batch(windows(3), s, batch=3, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert s.recent == [1, 1, 1]

    def test_a_barren_stretch_no_longer_trips_the_alarm(self, dom):
        """November Saturdays, reproduced: every option visa-rejected."""
        s = SweepStore()
        sweep_batch(windows(30), s, batch=25, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0, max_total_hours=1)
        assert not looks_throttled(s.recent), (
            "a barren but perfectly answered stretch read as a throttle")

    def test_a_real_outage_still_trips_it(self):
        """The fix must not become a silencer."""
        s = SweepStore()
        sweep_batch(windows(30), s, batch=25, fetch=FakeChrome(""),
                    sleep=lambda _: None, delay_s=0)
        assert looks_throttled(s.recent)

    def test_findings_are_unaffected(self, dom):
        """Only the health sample changed; the visa rule still decides what
        is stored."""
        s = SweepStore()
        sweep_batch(windows(1), s, batch=1, fetch=FakeChrome(dom),
                    sleep=lambda _: None, delay_s=0)
        assert len(s.found) == 1
        assert list(s.found.values())[0]["price_usd"] == 2652
