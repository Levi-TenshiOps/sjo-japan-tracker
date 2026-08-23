"""Regressions for the three behaviour changes requested after live testing.

1. The email greets the trip owner by name, in both parts, in UTF-8.
2. Priority months are capped at three.
3. Two emails go out every day regardless of price (daily_digest), while
   the two-per-day cap and the held second slot survive intact.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

import pytest

from tracker.alerts import CR_TZ, AlertState, decide, record_sent
from tracker.email_render import GREETING, render, render_html
from tracker.itinerary import build_itinerary
from tracker.notify import build_message
from tracker.preferences import MAX_PRIORITY_MONTHS, Preferences, PreferencesError
from tracker.pricing import SEED_BANDS
from tests import fixtures as fx

GOOD, GREAT = 1380, 1150
KANJI = "仲間"


def at(h, day=22):
    return datetime(2026, 8, day, h, 0, tzinfo=CR_TZ)


def itin(raw, dest="NRT", link="https://www.google.com/travel/flights?tfs=ABC"):
    return build_itinerary(raw, origin="SJO", destination=dest,
                           outbound_date=fx.DEPART, return_date=fx.RETURN,
                           deep_link=link)


@pytest.fixture
def items():
    return [itin(fx.MEX_OPTION), itin(fx.ZRH_OPTION)]


# --------------------------------------------------------------- greeting ---
class TestGreeting:
    def test_greeting_text_is_exact(self):
        assert GREETING == "Hello Nakama (" + KANJI + "),"

    def test_html_and_text_both_greet(self, items):
        c = render(items, SEED_BANDS, threshold=GOOD, is_great=False,
                   generated_at="Aug 22, 2026 at 21:00 Costa Rica time")
        assert GREETING in c.html
        assert GREETING in c.text

    def test_bare_hello_is_gone(self, items):
        html = render_html(items, SEED_BANDS, threshold=GOOD, is_great=False,
                           generated_at="now")
        assert ">Hello,</p>" not in html

    def test_kanji_survives_the_smtp_round_trip(self, items):
        """The kanji is non-ASCII; a latin-1 part would mangle or crash it."""
        c = render(items, SEED_BANDS, threshold=GOOD, is_great=False,
                   generated_at="now")
        msg = build_message(c, to_addr="a@b.com", from_addr="c@d.com",
                            from_name="Flight Tracker")
        seen = 0
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            seen += 1
            assert KANJI in part.get_content(), part.get_content_subtype()
        assert seen == 2, "expected both a plain-text and an HTML part"


# -------------------------------------------------------- priority months ---
class TestPriorityMonthCap:
    def _prefs(self, months):
        p = Preferences(alert_email="a@b.com")
        p.priority_months = months
        return p

    def test_cap_is_three(self):
        assert MAX_PRIORITY_MONTHS == 3

    @pytest.mark.parametrize("months", [[1], [1, 2], [1, 2, 3], [11, 12, 1]])
    def test_one_to_three_is_accepted(self, months):
        self._prefs(months).validate()

    @pytest.mark.parametrize("months", [[1, 2, 3, 4], list(range(1, 8))])
    def test_more_than_three_is_rejected(self, months):
        with pytest.raises(PreferencesError, match="at most 3 priority months"):
            self._prefs(months).validate()

    def test_duplicates_do_not_count_toward_the_cap(self):
        self._prefs([1, 1, 2, 2, 3, 3]).validate()

    def test_empty_is_still_allowed(self):
        self._prefs([]).validate()

    def test_default_preferences_are_within_the_cap(self):
        Preferences(alert_email="a@b.com").validate()


# ----------------------------------------------------------- daily digest ---
def ask(state, price, now, *, digest=True):
    return decide(state, best_price=price, best_signature="s%d" % price,
                  good_threshold=GOOD, great_threshold=GREAT, now=now,
                  always_send=digest)


def run_day(prices, hours=(6, 11, 16, 21), digest=True):
    state, sent = AlertState(), []
    for hour, price in zip(hours, prices):
        d = ask(state, price, at(hour), digest=digest)
        if d.should_send:
            record_sent(state, best_price=price, best_signature="s%d" % price,
                        is_great=d.is_great, now=at(hour))
            sent.append(price)
    return sent, state


class TestDigestAlwaysSendsTwo:
    def test_expensive_day_still_gets_two_emails(self):
        """The whole point: nothing beat the threshold, mail arrives anyway."""
        sent, _ = run_day([1500, 1520, 1490, 1470])
        assert len(sent) == 2

    def test_that_same_day_was_silent_before(self):
        sent, _ = run_day([1500, 1520, 1490, 1470], digest=False)
        assert sent == []

    def test_flat_day_gets_two_emails(self):
        sent, _ = run_day([1300, 1300, 1300, 1300])
        assert len(sent) == 2

    def test_rising_day_gets_two_emails(self):
        sent, _ = run_day([1200, 1250, 1300, 1350])
        assert len(sent) == 2

    def test_never_more_than_two(self):
        sent, state = run_day([1500, 1100, 1050, 1000])
        assert len(sent) == 2 and state.emails_sent_today == 2

    def test_second_email_still_carries_the_days_cheapest(self):
        """The reservation must survive digest mode, or it is just noise."""
        sent, _ = run_day([1500, 1452, 1380, 1275])
        assert sent[-1] == 1275

    def test_midday_is_still_held(self):
        state = AlertState()
        record_sent(state, best_price=1500, best_signature="a",
                    is_great=False, now=at(6))
        d = ask(state, 1490, at(16))
        assert not d.should_send and "held" in d.notes

    def test_standout_still_jumps_the_queue(self):
        state = AlertState()
        record_sent(state, best_price=1500, best_signature="a",
                    is_great=False, now=at(6))
        d = ask(state, 1100, at(11))
        assert d.should_send and d.is_great

    def test_min_gap_between_emails_is_respected(self):
        state = AlertState()
        record_sent(state, best_price=1500, best_signature="a",
                    is_great=False, now=at(20))
        d = ask(state, 1490, at(21))
        assert not d.should_send

    def test_no_fares_is_still_no_email(self):
        d = decide(AlertState(), best_price=None, best_signature=None,
                   good_threshold=GOOD, great_threshold=GREAT, now=at(7),
                   always_send=True)
        assert not d.should_send

    def test_rollover_gives_the_next_day_two_more(self):
        state = AlertState()
        for hour in (6, 21):
            d = ask(state, 1500, at(hour))
            assert d.should_send
            record_sent(state, best_price=1500, best_signature="a",
                        is_great=False, now=at(hour))
        assert not ask(state, 1500, at(22)).should_send
        assert ask(state, 1500, at(6, day=23)).should_send


class TestOverThresholdEmailReadsWell:
    def test_headline_leads_with_the_cheapest_price(self, items):
        """On a digest day nothing clears the bar; 'Found 0' helps nobody."""
        html = render_html(items, SEED_BANDS, threshold=900, is_great=False,
                           generated_at="now")
        assert "Nothing under $900 today" in html
        assert "Found 0 visa-free" not in html

    def test_normal_day_keeps_the_found_headline(self, items):
        """Fixture prices are $1,290 and $1,658, so exactly one clears."""
        html = render_html(items, SEED_BANDS, threshold=GOOD, is_great=False,
                           generated_at="now")
        assert "Found 1 visa-free option from" in html
        assert "Nothing under" not in html

    def test_footer_no_longer_promises_silence(self, items):
        c = render(items, SEED_BANDS, threshold=GOOD, is_great=False,
                   generated_at="now")
        for part in (c.html, c.text):
            assert "quiet inbox" not in part
            assert "nothing beat" not in part
            assert "silence means" not in part


# --------------------------------------------------------------- coverage ---
class TestCoverageReporting:
    """A demoted destination must not inflate the reported search space.

    SJO->OSA returns nothing from Google at any stop count, so it sits on
    probation and is not searched. Counting it anyway doubled the reported
    "searches for a complete pass" and made coverage look half as good as
    it is.
    """

    def _prefs(self):
        from tracker.preferences import Preferences
        p = Preferences(alert_email="a@b.com")
        p.destinations = ["TYO", "OSA"]
        p.departure_step_days = 4
        return p

    def test_default_counts_every_destination(self):
        from tracker.schedule import estimate_requests
        combos, full = estimate_requests(self._prefs())
        assert full == combos * 2

    def test_active_only_counts_what_is_searched(self):
        from tracker.schedule import estimate_requests
        combos, full = estimate_requests(self._prefs(), destinations=["TYO"])
        assert full == combos

    def test_step_one_prices_every_departure_day(self):
        """The whole point of step=1: no date is permanently invisible."""
        from datetime import date, timedelta
        from tracker.schedule import generate_windows
        p = self._prefs()
        p.departure_step_days = 1
        today = date(2026, 8, 22)
        _, late = p.window_on(today)
        floor = today + timedelta(days=p.min_lead_days)
        w = generate_windows(p, today=today)
        assert len({x.depart for x in w}) == (late - floor).days + 1

    def test_step_four_leaves_most_days_unpriced(self):
        """Documents what we moved away from, so a revert is a visible choice."""
        from datetime import date
        from tracker.schedule import generate_windows
        p = self._prefs()
        today = date(2026, 8, 22)
        days = {x.depart for x in generate_windows(p, today=today)}
        assert len(days) < 70
