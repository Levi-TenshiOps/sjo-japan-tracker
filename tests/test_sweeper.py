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
from tracker import sweeper
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

    def test_an_unpriced_window_is_always_taken(self):
        """It might be the cheapest there is; skipping it would blind us."""
        from tracker.sweeper import next_window
        ws = windows(5)
        s = self._stocked(ws[:2])
        s.cursor = 4                     # points at an unpriced window
        w, was_hot = next_window(ws, s)
        assert w.key == ws[4].key and not was_hot

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
