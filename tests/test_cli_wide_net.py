"""What the wide net is allowed to ask about.

`cli.py` had no tests at all until 2026-08-23, which is how this one got
through: after September and April were excluded from the search, the wide
net carried on querying them because it was driven by the raw 8-month
horizon rather than by the months actually being searched.

Six wasted requests a run is the small half. The real problem is that a
hint goes onto the *front* of the hot list and is verified through Chrome,
so a fare in an excluded month could have reached the email.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pytest

from tracker import monthly
from tracker.cli import wide_net_months
from tracker.preferences import Preferences

TODAY = date(2026, 8, 23)


def prefs(**kw):
    base = dict(alert_email="a@b.c", search_months=8, min_lead_days=21,
                departure_step_days=1, trip_weeks=[3], extra_nights=[],
                destinations=["TYO"], priority_months=[])
    base.update(kw)
    return Preferences(**base)


def labels(p):
    return [label for label, _year in wide_net_months(p, TODAY)]


class TestTheWideNetOnlyAsksAboutSearchedMonths:
    def test_the_trip_owners_six_months(self):
        assert labels(prefs(included_months=[1, 2, 3, 10, 11, 12])) == [
            "October 2026", "November 2026", "December 2026",
            "January 2027", "February 2027", "March 2027"]

    def test_excluded_months_are_never_asked_about(self):
        got = labels(prefs(included_months=[1, 2, 3, 10, 11, 12]))
        assert "September 2026" not in got
        assert "April 2027" not in got

    def test_the_exclude_list_is_honoured_too(self):
        assert "November 2026" not in labels(prefs(excluded_months=[11]))

    def test_no_filtering_still_covers_the_whole_horizon(self):
        assert len(labels(prefs())) == 8

    def test_the_year_travels_with_the_label(self):
        """parse_hint needs the anchor year to fill in a date Google omits."""
        for label, year in wide_net_months(prefs(included_months=[12, 1]), TODAY):
            assert str(year) in label

    def test_a_single_month_search_asks_about_one_month(self):
        assert labels(prefs(included_months=[2])) == ["February 2027"]

    def test_labels_match_what_the_ledger_keys_on(self):
        """A mismatch here silently splits every month into two rows."""
        got = wide_net_months(prefs(included_months=[2]), TODAY)
        assert got == [("February 2027", 2027)]
        assert got[0][0] in {label for label, _y
                             in monthly.months_in_window(date(2027, 2, 1),
                                                         date(2027, 2, 28))}

    def test_the_probe_count_shrinks_with_the_month_list(self):
        """18 probes a run instead of 24, with halves on."""
        six = wide_net_months(prefs(included_months=[1, 2, 3, 10, 11, 12]), TODAY)
        assert len(monthly.probe_count(six, halves=True)) == 18
        assert len(monthly.probe_count(wide_net_months(prefs(), TODAY),
                                       halves=True)) == 24
