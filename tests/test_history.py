"""CSV logging and baseline reads."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import csv
from datetime import date, datetime, timezone
import pytest

from tracker import history
from tracker.itinerary import build_itinerary
from tests import fixtures as fx


@pytest.fixture
def items():
    return [build_itinerary(fx.MEX_OPTION, origin="SJO", destination="NRT",
                            outbound_date=fx.DEPART_FEB, return_date=fx.RETURN_FEB,
                            deep_link="https://x.test/1")]


def rows(items):
    return history.rows_from(items, band_of=lambda p: "TYPICAL", band_source="SEED")


class TestAppend:
    def test_writes_header_once(self, tmp_path, items):
        p = tmp_path / "h.csv"
        history.append(p, rows(items))
        history.append(p, rows(items))
        assert p.read_text().count("checked_at_utc") == 1

    def test_row_contents(self, tmp_path, items):
        p = tmp_path / "h.csv"
        history.append(p, rows(items))
        rec = list(csv.DictReader(p.open()))[0]
        assert rec["price_usd"] == "1290"
        assert rec["origin"] == "SJO" and rec["destination"] == "NRT"
        assert rec["hubs"] == "MEX"
        assert rec["duration_min"] == "1770"   # 195+700+875
        assert rec["band"] == "TYPICAL"

    def test_empty_is_noop(self, tmp_path):
        p = tmp_path / "h.csv"
        assert history.append(p, []) == 0
        assert not p.exists()

    def test_creates_parent_dirs(self, tmp_path, items):
        p = tmp_path / "nested" / "deep" / "h.csv"
        history.append(p, rows(items))
        assert p.exists()


class TestRead:
    def test_missing_file(self, tmp_path):
        assert history.read_prices(tmp_path / "none.csv") == []

    def test_filters_by_route(self, tmp_path, items):
        p = tmp_path / "h.csv"
        history.append(p, rows(items))
        assert history.read_prices(p, origin="SJO") == [1290.0]
        assert history.read_prices(p, origin="LIR") == []
        assert history.read_prices(p, destination="KIX") == []

    def test_filters_by_date(self, tmp_path, items):
        p = tmp_path / "h.csv"
        history.append(p, history.rows_from(
            items, band_of=lambda x: "TYPICAL", band_source="SEED",
            checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        assert history.read_prices(p, since=date(2026, 6, 1)) == []
        assert history.read_prices(p, since=date(2025, 1, 1)) == [1290.0]

    def test_skips_bad_rows(self, tmp_path):
        p = tmp_path / "h.csv"
        p.write_text("checked_at_utc,origin,destination,price_usd\n"
                     "2026-01-01,SJO,NRT,abc\n"
                     "2026-01-01,SJO,NRT,0\n"
                     "2026-01-01,SJO,NRT,1300\n")
        assert history.read_prices(p) == [1300.0]


class TestDistinctDays:
    def test_counts_unique_days(self, tmp_path, items):
        p = tmp_path / "h.csv"
        for day in (1, 1, 2, 3, 3, 3):
            history.append(p, history.rows_from(
                items, band_of=lambda x: "TYPICAL", band_source="SEED",
                checked_at=datetime(2026, 8, day, 12, tzinfo=timezone.utc)))
        assert history.distinct_days(p) == 3

    def test_missing_file(self, tmp_path):
        assert history.distinct_days(tmp_path / "none.csv") == 0

    def test_filters_by_origin(self, tmp_path, items):
        p = tmp_path / "h.csv"
        history.append(p, rows(items))
        assert history.distinct_days(p, origin="SJO") == 1
        assert history.distinct_days(p, origin="LIR") == 0
