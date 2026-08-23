"""One-time setup answers: email, departure window, trip lengths."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
from datetime import date
import pytest

from tracker.preferences import (
    MAX_WEEKS, Preferences, PreferencesError, default_window, parse_weeks,
)


def prefs(**kw):
    base = dict(alert_email="user@example.com",
                earliest_departure="2027-01-05",
                latest_departure="2027-03-31",
                trip_weeks=[2, 3, 4, 5],
                extra_nights=[])   # tests below are about week conversion
    base.update(kw)
    return Preferences(**base)


class TestParseWeeks:
    @pytest.mark.parametrize("raw,out", [
        ("2,3,4,5", [2, 3, 4, 5]),
        ("2-5", [2, 3, 4, 5]),
        ("2 3 4 5", [2, 3, 4, 5]),
        ("2w,5w", [2, 5]),
        ("5-2", [2, 3, 4, 5]),          # reversed range
        ("3", [3]),
        ("2,2,3", [2, 3]),              # deduped
        ("1-3,5", [1, 2, 3, 5]),        # mixed
    ])
    def test_formats(self, raw, out):
        assert parse_weeks(raw) == out

    def test_rejects_empty(self):
        with pytest.raises(PreferencesError):
            parse_weeks("   ")

    def test_rejects_nonsense(self):
        with pytest.raises(PreferencesError):
            parse_weeks("two weeks")

    def test_rejects_out_of_range(self):
        with pytest.raises(PreferencesError, match="outside"):
            parse_weeks(f"{MAX_WEEKS + 1}")


class TestNights:
    def test_weeks_to_nights(self):
        assert prefs(trip_weeks=[2, 3]).nights_options == [14, 21]

    def test_flex_expands(self):
        p = prefs(trip_weeks=[2], duration_flex_days=2)
        assert p.nights_options == [12, 13, 14, 15, 16]

    def test_flex_dedupes_overlap(self):
        p = prefs(trip_weeks=[2, 3], duration_flex_days=4)
        assert p.nights_options == list(range(10, 26))   # 10..25, no repeats

    def test_zero_flex_is_exact(self):
        assert prefs(trip_weeks=[4]).nights_options == [28]

    def test_six_weeks_is_dropped_not_searched(self):
        """No round-trip fare exists past MAX_STAY_NIGHTS.

        Measured live: 30n and 31n price fine, 32n and beyond return nothing
        on every departure date. Searching them is a guaranteed empty.
        """
        p = prefs(trip_weeks=[2, 3, 4, 5])
        p.trip_weeks = sorted(p.trip_weeks + [6])
        p.validate()
        assert 42 not in p.nights_options
        assert 35 not in p.nights_options
        assert p.dropped_nights == [35, 42]
        assert p.nights_options == [14, 21, 28]

    def test_extra_nights_reaches_the_max_stay_sweet_spot(self):
        """30n is inside the rule and was materially cheaper than 28n."""
        p = prefs(trip_weeks=[2, 3, 4], extra_nights=[30])
        assert p.nights_options == [14, 21, 28, 30]
        assert p.dropped_nights == []

    def test_extra_nights_past_the_bound_is_also_dropped(self):
        p = prefs(trip_weeks=[2], extra_nights=[30, 45])
        assert p.nights_options == [14, 30]
        assert p.dropped_nights == [45]


class TestValidation:
    def test_ok(self):
        prefs().validate()

    def test_rejects_missing_email(self):
        with pytest.raises(PreferencesError, match="alert_email"):
            prefs(alert_email="").validate()

    def test_rejects_bad_email(self):
        with pytest.raises(PreferencesError, match="alert_email"):
            prefs(alert_email="not-an-email").validate()

    def test_rejects_backwards_window(self):
        with pytest.raises(PreferencesError, match="before"):
            prefs(earliest_departure="2027-06-01",
                  latest_departure="2027-01-01").validate()

    def test_rejects_no_trip_lengths(self):
        with pytest.raises(PreferencesError, match="trip_weeks is empty"):
            prefs(trip_weeks=[]).validate()

    def test_rejects_zero_step(self):
        with pytest.raises(PreferencesError, match="step"):
            prefs(departure_step_days=0).validate()

    def test_rejects_great_above_good(self):
        with pytest.raises(PreferencesError):
            prefs(good_price_usd=1000, great_price_usd=1500).validate()


class TestPersistence:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "prefs.json"
        prefs(trip_weeks=[2, 6]).save(p)
        assert Preferences.load(p).trip_weeks == [2, 6]

    def test_no_secrets_stored(self, tmp_path):
        """Preferences are shareable; the password must never land here."""
        p = tmp_path / "prefs.json"
        prefs().save(p)
        raw = p.read_text().lower()
        assert "password" not in raw and "smtp" not in raw

    def test_missing_file_points_at_setup(self, tmp_path):
        with pytest.raises(PreferencesError, match="setup_tracker"):
            Preferences.load(tmp_path / "none.json")

    def test_corrupt_file(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{{{")
        with pytest.raises(PreferencesError):
            Preferences.load(p)

    def test_load_or_none(self, tmp_path):
        assert Preferences.load_or_none(tmp_path / "none.json") is None

    def test_unknown_keys_ignored(self, tmp_path):
        p = tmp_path / "p.json"
        data = {"alert_email": "a@b.co", "earliest_departure": "2027-01-05",
                "latest_departure": "2027-03-31", "trip_weeks": [2],
                "future_field": "whatever"}
        p.write_text(json.dumps(data))
        assert Preferences.load(p).alert_email == "a@b.co"

    def test_destinations_uppercased(self, tmp_path):
        p = tmp_path / "p.json"
        prefs(destinations=["nrt", "kix"]).save(p)
        assert Preferences.load(p).destinations == ["NRT", "KIX"]


class TestDefaults:
    def test_window_is_in_the_future(self):
        early, late = default_window(date(2026, 8, 22))
        assert early > "2026-08-22" and late > early

    def test_describe_is_readable(self):
        text = prefs().describe()
        assert "14n, 21n, 28n" in text and "$1,380" in text
        assert "35n dropped" in text, "an unsearchable length must be visible"
