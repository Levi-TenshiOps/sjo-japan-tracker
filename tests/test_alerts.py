"""The email budget: max two per day, silence is fine."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

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
