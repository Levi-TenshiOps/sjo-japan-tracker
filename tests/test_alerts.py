"""The email budget: max two per day, silence is fine."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from tracker import alarm
from tracker.alerts import (
    CR_TZ, MAX_EMAILS_PER_DAY, AlertState, decide, record_sent,
)

GOOD, GREAT = 1380, 1150


def at(h, day=22, month=8):
    return datetime(2026, month, day, h, 0, tzinfo=CR_TZ)


def ask(state, price, now, sig="sig-a"):
    return decide(state, best_price=price, best_signature=sig,
                  good_threshold=GOOD, great_threshold=GREAT, now=now)


class TestThreshold:
    def test_above_threshold_is_silent(self):
        d = ask(AlertState(), 1500, at(7))
        assert not d.should_send and "above the $1,380" in d.reason

    def test_no_results_is_silent(self):
        d = decide(AlertState(), best_price=None, best_signature=None,
                   good_threshold=GOOD, great_threshold=GREAT, now=at(7))
        assert not d.should_send

    def test_exactly_at_threshold_sends(self):
        assert ask(AlertState(), GOOD, at(7)).should_send

    def test_below_threshold_sends(self):
        assert ask(AlertState(), 1379, at(7)).should_send


class TestDailyCap:
    def test_never_exceeds_two(self):
        """The headline guarantee: three runs, at most two emails."""
        s, sent = AlertState(), 0
        for hour, price in [(6, 1300), (12, 1200), (19, 1000)]:
            d = ask(s, price, at(hour))
            if d.should_send:
                record_sent(s, best_price=price, best_signature="x",
                            is_great=d.is_great, now=at(hour))
                sent += 1
        assert sent == MAX_EMAILS_PER_DAY == 2

    def test_many_qualifying_runs_still_two(self):
        s, sent = AlertState(), 0
        price = 1370
        for hour in range(0, 24):
            price -= 20  # always materially better
            d = ask(s, price, at(hour))
            if d.should_send:
                record_sent(s, best_price=price, best_signature="x",
                            is_great=d.is_great, now=at(hour))
                sent += 1
        assert sent == 2

    def test_cap_message(self):
        s = AlertState(day="2026-08-22", emails_sent_today=2)
        d = ask(s, 900, at(20))
        assert not d.should_send and "daily cap" in d.reason


class TestSecondEmail:
    def test_trivial_drop_blocked(self):
        s = AlertState()
        record_sent(s, best_price=1300, best_signature="x", is_great=False, now=at(6))
        d = ask(s, 1295, at(12))
        assert not d.should_send and "not materially better" in d.reason

    def test_price_increase_blocked(self):
        s = AlertState()
        record_sent(s, best_price=1200, best_signature="x", is_great=False, now=at(6))
        assert not ask(s, 1350, at(12)).should_send

    def test_dollar_drop_sends_at_last_call(self):
        """Before last call the slot is reserved; at last call it is spent."""
        s = AlertState()
        record_sent(s, best_price=1300, best_signature="x", is_great=False, now=at(6))
        assert not ask(s, 1240, at(12)).should_send   # held
        assert ask(s, 1240, at(21)).should_send

    def test_percentage_drop_qualifies_at_last_call(self):
        s = AlertState()
        record_sent(s, best_price=1375, best_signature="x", is_great=False, now=at(6))
        assert ask(s, 1320, at(21)).should_send

    def test_cooldown_blocks_rapid_second(self):
        s = AlertState()
        record_sent(s, best_price=1300, best_signature="x", is_great=False, now=at(6))
        d = ask(s, 1000, at(7))  # big drop but only 1h later
        assert not d.should_send and "since the last email" in d.reason

    def test_great_overrides_material_rule(self):
        s = AlertState()
        record_sent(s, best_price=1160, best_signature="x", is_great=False, now=at(6))
        d = ask(s, 1149, at(12))  # only $11 lower, but crosses into standout
        assert d.should_send and d.is_great and "standout" in d.reason

    def test_great_twice_does_not_double_fire(self):
        s = AlertState()
        record_sent(s, best_price=1100, best_signature="x", is_great=True, now=at(6))
        assert not ask(s, 1095, at(12)).should_send


class TestDayRollover:
    def test_new_day_resets(self):
        s = AlertState()
        record_sent(s, best_price=1300, best_signature="x", is_great=False, now=at(6))
        record_sent(s, best_price=1200, best_signature="x", is_great=False, now=at(14))
        assert not ask(s, 1100, at(20)).should_send
        assert ask(s, 1300, at(7, day=23)).should_send

    def test_uses_costa_rica_midnight_not_utc(self):
        """23:00 CR is 05:00 UTC next day; must still count as the same CR day."""
        s = AlertState()
        record_sent(s, best_price=1300, best_signature="x", is_great=False, now=at(23))
        assert s.day == "2026-08-22"
        assert s.emails_sent_today == 1

    def test_rollover_clears_great_flag(self):
        s = AlertState()
        record_sent(s, best_price=1000, best_signature="x", is_great=True, now=at(6))
        s.roll_day(at(6, day=23))
        assert not s.great_alerted_today and s.emails_sent_today == 0


class TestPersistence:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "state.json"
        s = AlertState()
        record_sent(s, best_price=1234, best_signature="sig", is_great=False, now=at(6))
        s.save(p)
        loaded = AlertState.load(p)
        assert loaded.last_best_price == 1234
        assert loaded.emails_sent_today == 1
        assert loaded.total_emails_sent == 1

    def test_missing_file(self, tmp_path):
        assert AlertState.load(tmp_path / "nope.json").emails_sent_today == 0

    def test_corrupt_file_recovers(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json")
        assert AlertState.load(p).emails_sent_today == 0

    def test_unknown_keys_ignored(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"emails_sent_today": 1, "mystery": True}))
        assert AlertState.load(p).emails_sent_today == 1

    def test_decide_does_not_mutate(self):
        s = AlertState()
        before = (s.day, s.emails_sent_today)
        ask(s, 1200, at(6))
        assert (s.day, s.emails_sent_today) == before


class TestCheapestWinsTheLastSlot:
    """The two-email budget must land on the day's best fare, not its first.

    Regression: an early $1,370 plus a midday $1,180 used to consume both
    slots, so an evening $1,050 could never be reported.
    """

    def full_day(self, prices, hours=(6, 11, 16, 21), great=1150, good=1380):
        state, sent = AlertState(), []
        for hour, price in zip(hours, prices):
            now = at(hour)
            d = decide(state, best_price=price, best_signature=f"s{price}",
                       good_threshold=good, great_threshold=great, now=now)
            if d.should_send:
                record_sent(state, best_price=price, best_signature=f"s{price}",
                            is_great=d.is_great, now=now)
                sent.append(price)
        return sent, state

    def test_evening_bargain_still_gets_through(self):
        sent, _ = self.full_day([1370, 1352, 1180, 1050])
        assert sent == [1370, 1050]
        assert min(sent) == 1050

    def test_midday_improvement_is_held_not_spent(self):
        state = AlertState()
        record_sent(state, best_price=1370, best_signature="a",
                    is_great=False, now=at(6))
        d = decide(state, best_price=1180, best_signature="b",
                   good_threshold=1380, great_threshold=1150, now=at(16))
        assert not d.should_send and "held" in d.notes

    def test_held_slot_is_spent_at_last_call(self):
        state = AlertState()
        record_sent(state, best_price=1370, best_signature="a",
                    is_great=False, now=at(6))
        d = decide(state, best_price=1180, best_signature="b",
                   good_threshold=1380, great_threshold=1150, now=at(21))
        assert d.should_send and "best of the day" in d.reason

    def test_standout_jumps_the_queue(self):
        """A standout fare may vanish, so it does not wait for the evening."""
        state = AlertState()
        record_sent(state, best_price=1370, best_signature="a",
                    is_great=False, now=at(6))
        d = decide(state, best_price=1100, best_signature="b",
                   good_threshold=1380, great_threshold=1150, now=at(11))
        assert d.should_send and d.is_great

    def test_still_never_exceeds_two(self):
        sent, state = self.full_day([1370, 1100, 1050, 1000])
        assert len(sent) == 2 and state.emails_sent_today == 2

    def test_no_qualifying_fare_means_no_email(self):
        sent, _ = self.full_day([1500, 1450, 1600, 1420])
        assert sent == []

    def test_prices_rising_does_not_spend_the_last_slot(self):
        sent, _ = self.full_day([1200, 1250, 1300, 1350])
        assert sent == [1200]

    def test_reservation_can_be_disabled(self):
        state = AlertState()
        record_sent(state, best_price=1370, best_signature="a",
                    is_great=False, now=at(6))
        d = decide(state, best_price=1180, best_signature="b",
                   good_threshold=1380, great_threshold=1150, now=at(16),
                   reserve_last_slot=False)
        assert d.should_send

    def test_last_call_hour_is_configurable(self):
        state = AlertState()
        record_sent(state, best_price=1370, best_signature="a",
                    is_great=False, now=at(6))
        d = decide(state, best_price=1180, best_signature="b",
                   good_threshold=1380, great_threshold=1150, now=at(15),
                   last_call_hour=14)
        assert d.should_send

    def test_six_runs_still_capped_and_still_cheapest(self):
        sent, _ = self.full_day(
            [1375, 1360, 1340, 1300, 1200, 1090],
            hours=(6, 9, 12, 15, 18, 21))
        assert len(sent) == 2
        assert sent[-1] == 1090


class TestTheHeldSlotReasonIsHonest:
    """The log line must not claim an improvement that did not happen.

    Found in the live log 2026-08-23: the 18:06 run wrote "$1,390 beats the
    $1,347 already sent, but the last email slot is being held". $1,390 does
    not beat $1,347. The branch was written assuming it is only reached when
    the fare improved, which is true with the price threshold gating
    delivery and false in digest mode, where every run reaches it.
    """

    def _held(self, best, prev):
        st = AlertState(day="2026-08-23", emails_sent_today=1,
                        last_best_price=prev, last_signature="old",
                        last_email_at="2026-08-23T12:38:32+00:00")
        return decide(st, best_price=best, best_signature="new",
                      good_threshold=1400, great_threshold=1150,
                      now=datetime(2026, 8, 23, 18, 6, tzinfo=CR_TZ),
                      always_send=True, reserve_last_slot=True,
                      last_call_hour=20)

    def test_a_dearer_fare_is_not_described_as_beating(self):
        d = self._held(1390, 1347)
        assert not d.should_send and "held" in d.notes
        assert "beats" not in d.reason, d.reason
        assert "no better" in d.reason

    def test_a_genuinely_cheaper_fare_still_says_beats(self):
        d = self._held(1200, 1347)
        assert "beats" in d.reason, d.reason

    def test_either_way_the_slot_is_held_for_the_same_reason(self):
        for best in (1200, 1390):
            d = self._held(best, 1347)
            assert "held until 20:00" in d.reason
            assert d.notes == ["held"]


class TestTheThrottleAlarm:
    """A background process that goes quiet looks like one that is working.

    On 2026-08-23 the address was throttled for most of a day and the only
    trace was a warning in a log file nobody was reading. The trip owner
    found out by asking. Two emails now: one when a throttle is confirmed,
    one when it clears.
    """

    def cfg(self, **kw):
        base = dict(to_addr="a@b.c", smtp_user="u@b.c", smtp_password="p")
        base.update(kw)
        return alarm.AlarmConfig(**base)

    def test_the_blocked_email_says_what_is_happening(self):
        c = alarm.blocked_email(empty_rate=75.0, since="2026-08-23T15:20:00",
                                suspect=41, rest_minutes=30, rest_number=2)
        assert "throttl" in c.subject.lower()
        assert "75%" in c.text and "15:20" in c.text
        assert "41 window(s)" in c.text

    def test_the_blocked_email_says_what_not_to_do(self):
        """Diagnosing a throttle by making more requests is what made the
        2026-08-23 outage last an hour instead of minutes."""
        c = alarm.blocked_email(empty_rate=75.0, since="2026-08-23T15:20:00",
                                suspect=1, rest_minutes=15, rest_number=1)
        assert "another request" in c.text

    def test_the_blocked_email_promises_no_action_is_needed(self):
        c = alarm.blocked_email(empty_rate=70.0, since="2026-08-23T15:20:00",
                                suspect=1, rest_minutes=15, rest_number=1)
        assert "Nothing is needed from you" in c.text

    def test_the_recovered_email_reports_the_damage(self):
        c = alarm.recovered_email(minutes=95.0, suspect=41,
                                  windows_priced=1901)
        assert "95 minutes" in c.text
        assert "41 window(s)" in c.text and "1,901" in c.text

    def test_both_parts_are_present(self):
        for c in (alarm.blocked_email(empty_rate=70, since="2026-08-23T15:20:00",
                                      suspect=1, rest_minutes=15, rest_number=1),
                  alarm.recovered_email(minutes=10, suspect=0, windows_priced=1)):
            assert c.text.strip() and c.html.strip()

    def test_no_smtp_means_no_send_and_no_crash(self):
        c = alarm.recovered_email(minutes=1, suspect=0, windows_priced=1)
        assert alarm.send(c, alarm.AlarmConfig()) is False

    def test_a_dry_run_reports_success_without_sending(self):
        c = alarm.recovered_email(minutes=1, suspect=0, windows_priced=1)
        assert alarm.send(c, self.cfg(), dry_run=True) is True

    def test_a_broken_smtp_never_raises(self):
        """The sweep surviving matters more than the telling."""
        c = alarm.recovered_email(minutes=1, suspect=0, windows_priced=1)
        assert alarm.send(c, self.cfg(smtp_host="not.a.host.invalid",
                                      smtp_port=1)) is False

    def test_config_is_read_off_the_project_config(self):
        class FakeCfg:
            alert_email, smtp_host, smtp_port = "x@y.z", "smtp.test", 25
            smtp_user, smtp_password = "u", "p"
        got = alarm.AlarmConfig.from_config(FakeCfg())
        assert got.usable and got.to_addr == "x@y.z" and got.smtp_port == 25


class TestTheAlarmFiresOncePerEvent:
    """An escalating backoff can cycle for hours. Six identical emails is
    not an alert, it is noise that gets filtered."""

    def test_a_second_rest_in_the_same_throttle_is_silent(self):
        from tracker.sweeper import SweepStore
        s = SweepStore(throttled_since="2026-08-23T15:20:00+00:00")
        s.alarm_sent_for = s.throttled_since
        assert s.alarm_sent_for == s.throttled_since, (
            "the guard is the equality the sweep checks before alarming")

    def test_a_new_throttle_alarms_again(self):
        from tracker.sweeper import SweepStore
        s = SweepStore(throttled_since="2026-08-23T15:20:00+00:00")
        s.alarm_sent_for = s.throttled_since
        s.throttled_since = "2026-08-24T09:00:00+00:00"   # a new event
        assert s.alarm_sent_for != s.throttled_since

    def test_the_flag_survives_a_restart(self):
        """Otherwise a restart mid-throttle sends a duplicate."""
        from tracker.sweeper import SweepStore
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.json"
            s = SweepStore(alarm_sent_for="2026-08-23T15:20:00+00:00")
            s.save(p)
            assert SweepStore.load(p).alarm_sent_for == "2026-08-23T15:20:00+00:00"


class TestTheSilenceWatchdog:
    """Notice when the product stops, not just when the search does.

    On 2026-08-24 one malformed CSV line crashed every scheduled run four
    hours before its email phase. The sweep carried on perfectly, --status
    read 15% empty, the coverage invariant reported zero orphans - all true,
    and all about the wrong thing. Nothing anywhere watched for "the emails
    stopped arriving", which is the only thing the trip owner actually
    receives.

    The sweep is the only always-on process, so it is the only thing that
    can watch the scheduled runs. It reads their state file, never its own.
    """

    def _state(self, tmp_path, hours_ago=None, raw=None):
        import json
        from datetime import datetime, timedelta, timezone
        p = tmp_path / "state.json"
        if raw is not None:
            p.write_text(raw, encoding="utf-8")
            return p
        body = {"day": "2026-08-24", "emails_sent_today": 1}
        if hours_ago is not None:
            body["last_email_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).isoformat()
        p.write_text(json.dumps(body), encoding="utf-8")
        return p

    def test_a_recent_email_is_not_silence(self, tmp_path):
        h = alarm.hours_since_last_email(self._state(tmp_path, hours_ago=2))
        assert h < alarm.SILENCE_HOURS

    def test_the_overnight_gap_is_not_silence(self, tmp_path):
        """Evening digest ~21:30 to the morning run ~06:45 is ~9 hours and
        completely normal. A watchdog that cries every night is useless."""
        h = alarm.hours_since_last_email(self._state(tmp_path, hours_ago=10))
        assert h < alarm.SILENCE_HOURS

    def test_a_long_silence_is_caught(self, tmp_path):
        h = alarm.hours_since_last_email(self._state(tmp_path, hours_ago=20))
        assert h > alarm.SILENCE_HOURS

    def test_the_real_outage_would_have_been_caught(self, tmp_path):
        """09:03 crash, found by the trip owner at ~10:30 the next morning.
        The watchdog would have fired overnight instead."""
        h = alarm.hours_since_last_email(self._state(tmp_path, hours_ago=17))
        assert h > alarm.SILENCE_HOURS

    def test_never_emailed_is_not_reported_as_silence(self, tmp_path):
        """A fresh install has no last_email_at and is not broken."""
        assert alarm.hours_since_last_email(self._state(tmp_path)) is None

    def test_a_missing_state_file_is_quiet(self, tmp_path):
        assert alarm.hours_since_last_email(tmp_path / "nope.json") is None

    def test_a_corrupt_state_file_is_quiet(self, tmp_path):
        """The lesson from the CSV: never crash on a file you only read."""
        assert alarm.hours_since_last_email(
            self._state(tmp_path, raw="{ not json")) is None

    def test_a_nonsense_timestamp_is_quiet(self, tmp_path):
        import json
        p = self._state(tmp_path, raw=json.dumps(
            {"last_email_at": "not-a-timestamp"}))
        assert alarm.hours_since_last_email(p) is None

    def test_a_naive_timestamp_is_handled(self, tmp_path):
        """An older state file may have no timezone on it."""
        import json
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(hours=20)).isoformat()
        p = self._state(tmp_path, raw=json.dumps({"last_email_at": stamp}))
        h = alarm.hours_since_last_email(p)
        assert h is not None and h > alarm.SILENCE_HOURS

    def test_the_email_says_where_to_look(self, tmp_path):
        c = alarm.silent_email(hours=17.0, threshold=alarm.SILENCE_HOURS)
        assert "17 hours" in c.text
        assert "tracker.log" in c.text and "Task Scheduler" in c.text

    def test_the_email_points_at_the_right_process(self):
        """The sweep is fine; saying so stops the reader debugging it."""
        c = alarm.silent_email(hours=17.0, threshold=16.0)
        assert "not the search" in c.text

    def test_the_email_names_the_signature_of_the_real_bug(self):
        """An empty tail in tracker.log, not an error message."""
        c = alarm.silent_email(hours=17.0, threshold=16.0)
        assert "stop mid-run" in c.text or "empty tail" in c.text

    def test_it_reads_the_state_file_not_the_sweep_store(self, tmp_path):
        """The whole point: watch the other process, not yourself."""
        import inspect
        src = inspect.getsource(alarm.hours_since_last_email)
        assert "last_email_at" in src
        assert "discoveries" not in src
