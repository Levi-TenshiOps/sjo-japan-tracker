"""The scheduled runs read a file the sweeper is still writing.

`sweep_history.csv` is appended to roughly every ninety seconds, for ever,
while six scheduled runs a day read it for the price baseline. A read can
land mid-append. On 2026-08-24 the 09:03 run did exactly that and died:
exit code 1, no traceback, `tracker.log` simply stopping after the grid
phase.

This is the same lesson as the malformed row that killed every run for
four hours the day before - never crash on a file you only read - one
level up. That fix guarded the fields; a torn write breaks the *iteration*
before any field is reached.
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.history import distinct_days, read_prices

HEADER = ("checked_at_utc,origin,destination,price_usd,band_source\n")
GOOD = ("2026-08-24T09:00:00+00:00,SJO,TYO,1347,CHROME\n"
        "2026-08-23T09:00:00+00:00,SJO,TYO,1432,CHROME\n")


def write(tmp_path, body: bytes):
    p = tmp_path / "sweep_history.csv"
    p.write_bytes(body)
    return p


class TestATornAppendDoesNotKillTheRun:
    def test_nul_padded_tail(self, tmp_path):
        """Windows reads an extended-but-unflushed tail as NUL bytes.

        `csv` reports that as `_csv.Error: line contains NUL`, which is the
        exception that took the 09:03 run down.
        """
        p = write(tmp_path, (HEADER + GOOD).encode() + b"\x00" * 64)
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]
        assert distinct_days(p, origin="SJO") == 2

    def test_row_cut_mid_field(self, tmp_path):
        p = write(tmp_path, (HEADER + GOOD).encode() + b"2026-08-24T09:01:00+00:00,SJO,TY")
        # The complete rows survive; the partial one is simply not counted.
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]

    def test_row_with_an_unterminated_quote(self, tmp_path):
        p = write(tmp_path, (HEADER + GOOD).encode() + b'2026-08-24,SJO,"TYO\n')
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]

    def test_a_torn_utf8_sequence(self, tmp_path):
        """Airline names are not all ASCII; a cut can split a code point."""
        p = write(tmp_path, (HEADER + GOOD).encode() + b"2026-08-24,SJO,TYO,1500,CHR\xc3")
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]

    def test_nothing_but_a_torn_row_is_empty_not_an_error(self, tmp_path):
        p = write(tmp_path, HEADER.encode() + b"\x00\x00\x00")
        assert read_prices(p, origin="SJO") == []
        assert distinct_days(p, origin="SJO") == 0

    def test_a_missing_file_is_still_empty(self, tmp_path):
        assert read_prices(tmp_path / "nope.csv") == []
        assert distinct_days(tmp_path / "nope.csv") == 0

    def test_a_directory_where_a_file_belongs(self, tmp_path):
        d = tmp_path / "sweep_history.csv"
        d.mkdir()
        assert read_prices(d) == []
        assert distinct_days(d) == 0


class TestTheGoodPathStillWorks:
    def test_a_clean_file_reads_completely(self, tmp_path):
        p = write(tmp_path, (HEADER + GOOD).encode())
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]
        assert distinct_days(p, origin="SJO") == 2

    def test_the_band_source_filter_still_applies(self, tmp_path):
        body = HEADER + GOOD + "2026-08-24T09:00:00+00:00,SJO,TYO,2866,HTTP\n"
        p = write(tmp_path, body.encode())
        assert read_prices(p, origin="SJO", band_source="CHROME") == [1347.0, 1432.0]
