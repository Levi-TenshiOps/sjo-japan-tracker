"""The monthly wide net: one text query per month, parsed offline.

Fixtures below are the real shapes seen on 2026-08-22. Both renderings
occur: one carries the year, one omits it and omits the second month too.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pytest

from tracker.monthly import (
    MonthHint, hint_window_keys, month_halves, months_in_window, parse_hint,
    scan_months, visible_text,
)

# Real page text, trimmed. Note the en dash and the missing year.
WITH_YEAR = ("Best Close dialog Looking for stops Travel Jan 29 – Feb 25, 2027 "
             "for $1,347 Change dates Date grid Price graph Other departing flights")
NO_YEAR = ("Track prices Any dates Travel Sep 30 – Oct 30 for $1,604 "
           "Change dates Date grid Price graph")
NO_HINT = ("Travel update Air traffic disruptions may affect flights. Best "
           "Close dialog Looking for stops")
HTML = ('<html><body><script>var x = "Travel Nov 1 - Nov 2 for $99";</script>'
        '<div>Travel Nov 30 – Dec 29 for $1,432</div></body></html>')


class TestParseHint:
    def test_reads_a_hint_that_carries_the_year(self):
        h = parse_hint(WITH_YEAR, month="February 2027", anchor_year=2027)
        assert h == MonthHint("February 2027", date(2027, 1, 29), date(2027, 2, 25), 1347)
        assert h.nights == 27

    def test_fills_in_a_missing_year_from_the_anchor(self):
        h = parse_hint(NO_YEAR, month="October 2026", anchor_year=2026)
        assert h.depart == date(2026, 9, 30) and h.ret == date(2026, 10, 30)
        assert h.nights == 30 and h.price_usd == 1604

    def test_no_recommendation_is_none_not_an_error(self):
        assert parse_hint(NO_HINT, month="March 2027", anchor_year=2027) is None

    def test_strips_markup_and_ignores_script_contents(self):
        """A price inside a <script> must not be mistaken for the hint."""
        h = parse_hint(HTML, month="December 2026", anchor_year=2026)
        assert h.price_usd == 1432
        assert h.depart == date(2026, 11, 30)

    def test_year_rolls_forward_across_new_year(self):
        h = parse_hint("Travel Dec 28 – Jan 20 for $1,500",
                       month="December 2026", anchor_year=2026)
        assert h.depart == date(2026, 12, 28) and h.ret == date(2027, 1, 20)
        assert h.nights == 23

    def test_impossible_date_is_rejected(self):
        assert parse_hint("Travel Feb 30 – Mar 20, 2027 for $900",
                          month="February 2027", anchor_year=2027) is None

    def test_backwards_range_is_rejected(self):
        assert parse_hint("Travel Mar 20, 2027 – Mar 2, 2027 for $900",
                          month="March 2027", anchor_year=2027) is None

    @pytest.mark.parametrize("dash", ["–", "—", "-"])
    def test_accepts_every_dash_google_uses(self, dash):
        h = parse_hint(f"Travel Sep 3 {dash} Sep 30, 2026 for $1,663",
                       month="September 2026", anchor_year=2026)
        assert h is not None and h.price_usd == 1663

    def test_visible_text_drops_tags(self):
        assert "<div>" not in visible_text(HTML)


class TestMonthsInWindow:
    def test_covers_every_month_the_window_touches(self):
        got = months_in_window(date(2026, 9, 12), date(2027, 4, 22))
        assert len(got) == 8
        assert got[0] == ("September 2026", 2026)
        assert got[-1] == ("April 2027", 2027)

    def test_single_month_window(self):
        assert months_in_window(date(2027, 2, 1), date(2027, 2, 28)) == [
            ("February 2027", 2027)]


class FakeFetch:
    """Injected in place of the network, per the project's test rule."""
    def __init__(self, pages):
        self.pages, self.queries = pages, []

    def __call__(self, query):
        self.queries.append(query)
        for needle, page in self.pages.items():
            if needle in query:
                if isinstance(page, Exception):
                    raise page
                return page
        return ""


class TestScanMonths:
    def test_one_request_per_month_and_hints_come_back(self):
        f = FakeFetch({"February 2027": WITH_YEAR, "October 2026": NO_YEAR})
        hints = scan_months(f, [("October 2026", 2026), ("February 2027", 2027)], delay_s=0)
        assert len(f.queries) == 2
        assert [h.price_usd for h in hints] == [1604, 1347]

    def test_a_month_without_a_hint_is_skipped_quietly(self):
        f = FakeFetch({"March 2027": NO_HINT, "February 2027": WITH_YEAR})
        hints = scan_months(f, [("March 2027", 2027), ("February 2027", 2027)], delay_s=0)
        assert [h.month for h in hints] == ["February 2027"]

    def test_a_failing_month_does_not_abort_the_sweep(self):
        """Five of eight months returned nothing on the first real pass."""
        f = FakeFetch({"January 2027": RuntimeError("boom"),
                       "February 2027": WITH_YEAR})
        hints = scan_months(f, [("January 2027", 2027), ("February 2027", 2027)], delay_s=0)
        assert len(hints) == 1

    def test_nights_outside_the_trip_range_are_filtered(self):
        f = FakeFetch({"October 2026": NO_YEAR})       # 30 nights
        assert scan_months(f, [("October 2026", 2026)], max_nights=28, delay_s=0) == []
        assert scan_months(f, [("October 2026", 2026)], min_nights=31, delay_s=0) == []
        assert len(scan_months(f, [("October 2026", 2026)],
                               min_nights=21, max_nights=31, delay_s=0)) == 1

    def test_query_names_origin_and_destination(self):
        f = FakeFetch({})
        scan_months(f, [("May 2027", 2027)], origin="SJO", destination="HND", delay_s=0)
        assert f.queries == ["Flights from SJO to HND in May 2027"]

    def test_keys_are_ordered_cheapest_first(self):
        a = MonthHint("a", date(2027, 1, 1), date(2027, 1, 22), 1800)
        b = MonthHint("b", date(2027, 2, 1), date(2027, 2, 22), 1200)
        assert hint_window_keys([a, b]) == [b.key, a.key]

    def test_key_matches_the_hot_list_shape(self):
        """A mismatched separator silently makes every hint a hot-list miss."""
        from tracker.schedule import Window
        h = MonthHint("x", date(2027, 1, 29), date(2027, 2, 25), 1347)
        assert h.key == "2027-01-29_2027-02-25"
        assert h.key == Window(h.depart, h.ret).key


class TestTextQueryForcesCurrency:
    """Non-negotiable #4: never let CRC reach the email.

    fast_flights.fetch_flights_html sends only {"q": ...} for a string
    query, dropping hl and curr. Google then serves CRC to a Costa Rican
    IP, and the page wording changes enough that the hint stops matching -
    which is how this was found: eight months, eight big pages, zero hints.
    """

    def _captured(self, monkeypatch):
        seen = {}

        class FakeResponse:
            text = "Travel Jan 29 – Feb 25, 2027 for $1,347"

        class FakeClient:
            def __init__(self, **kw):
                seen["client_kwargs"] = kw

            def get(self, url, params=None):
                seen["url"] = url
                seen["params"] = params
                return FakeResponse()

        import tracker.search as search
        monkeypatch.setattr(search, "Client", FakeClient)
        return seen

    def test_currency_is_forced_to_usd(self, monkeypatch):
        from tracker.search import fetch_text_query
        seen = self._captured(monkeypatch)
        fetch_text_query("Flights from SJO to NRT in February 2027")
        assert seen["params"]["curr"] == "USD"

    def test_language_is_forced_to_english(self, monkeypatch):
        from tracker.search import fetch_text_query
        seen = self._captured(monkeypatch)
        fetch_text_query("Flights from SJO to NRT in February 2027")
        assert seen["params"]["hl"] == "en"

    def test_query_is_passed_through(self, monkeypatch):
        from tracker.search import fetch_text_query
        seen = self._captured(monkeypatch)
        fetch_text_query("Flights from SJO to NRT in February 2027")
        assert seen["params"]["q"] == "Flights from SJO to NRT in February 2027"

    def test_a_failure_returns_empty_not_an_exception(self, monkeypatch):
        import tracker.search as search

        class Boom:
            def __init__(self, **kw):
                raise RuntimeError("network down")

        monkeypatch.setattr(search, "Client", Boom)
        assert search.fetch_text_query("anything") == ""


class TestMonthHalves:
    """Narrower ranges surface windows the whole-month query misses.

    Measured 2026-08-22: "January 16 to January 31 2027" named a $1,387
    window that "in January 2027" never mentioned. Additive, not a
    replacement - five of eight narrow queries returned nothing at all.
    """

    def test_two_probes_per_month(self):
        got = month_halves([("January 2027", 2027)])
        assert len(got) == 2

    def test_fragments_read_as_google_expects(self):
        got = month_halves([("January 2027", 2027)])
        assert got[0][0] == "January 1 to January 15 2027"
        assert got[1][0] == "January 16 to January 31 2027"

    def test_short_months_end_correctly(self):
        assert month_halves([("February 2027", 2027)])[1][0].endswith("February 28 2027")
        assert month_halves([("April 2027", 2027)])[1][0].endswith("April 30 2027")

    def test_leap_february_gets_29_days(self):
        assert month_halves([("February 2028", 2028)])[1][0].endswith("February 29 2028")

    def test_labels_distinguish_the_halves(self):
        got = month_halves([("March 2027", 2027)])
        assert "1st half" in got[0][1] and "2nd half" in got[1][1]

    def test_scan_without_halves_asks_once_per_month(self):
        f = FakeFetch({})
        scan_months(f, [("March 2027", 2027)], halves=False, delay_s=0)
        assert len(f.queries) == 1

    def test_scan_with_halves_asks_three_times(self):
        f = FakeFetch({})
        scan_months(f, [("March 2027", 2027)], halves=True, delay_s=0)
        assert len(f.queries) == 3
        assert "in March 2027" in f.queries[0]
        assert "March 1 to March 15 2027" in f.queries[1]

    def test_a_window_repeated_across_probes_is_reported_once(self):
        """Halves usually echo the month's own answer; do not double-count."""
        f = FakeFetch({"March": WITH_YEAR})
        hints = scan_months(f, [("March 2027", 2027)], halves=True, delay_s=0)
        assert len(hints) == 1


from datetime import date as Date          # noqa: E402
from tracker import monthly                # noqa: E402


class TestTheMonthLedger:
    """The wide net's answers must outlive the run that fetched them.

    Found 2026-08-23. The sweep walks the priority months first, so 37% into
    its first pass it had priced January, February and March and nothing
    else - five of eight months had no price data at all. Meanwhile the wide
    net had been asking about every month, six times a day, logging the
    answers and discarding them. Keeping them costs nothing.
    """

    def hint(self, month="February 2027", price=1347,
             dep=Date(2027, 1, 29), ret=Date(2027, 2, 25)):
        return MonthHint(month=month, depart=dep, ret=ret, price_usd=price)

    def test_a_missing_ledger_is_empty_not_an_error(self, tmp_path):
        led = monthly.load_ledger(tmp_path / "nope.json")
        assert led["months"] == {}

    def test_a_corrupt_ledger_is_discarded_not_raised(self, tmp_path):
        p = tmp_path / "led.json"
        p.write_text("{ not json", encoding="utf-8")
        assert monthly.load_ledger(p)["months"] == {}

    def test_a_hint_is_recorded(self, tmp_path):
        p = tmp_path / "led.json"
        monthly.record_hints(p, [self.hint()], asked=["February 2027"])
        row = monthly.load_ledger(p)["months"]["February 2027"]
        assert row["best_usd"] == 1347 and row["hits"] == 1 and row["asks"] == 1

    def test_the_best_price_survives_a_dearer_later_run(self, tmp_path):
        """The latest price alone cannot answer 'was this month ever cheap?'"""
        p = tmp_path / "led.json"
        monthly.record_hints(p, [self.hint(price=1347)], asked=["February 2027"])
        monthly.record_hints(p, [self.hint(price=2100)], asked=["February 2027"])
        row = monthly.load_ledger(p)["months"]["February 2027"]
        assert row["best_usd"] == 1347, "the cheapest ever must be kept"
        assert row["last_usd"] == 2100, "and the latest, separately"
        assert row["hits"] == 2 and row["asks"] == 2

    def test_a_cheaper_run_replaces_the_best_and_its_dates(self, tmp_path):
        p = tmp_path / "led.json"
        monthly.record_hints(p, [self.hint(price=2100)], asked=["February 2027"])
        monthly.record_hints(
            p, [self.hint(price=1347, dep=Date(2027, 2, 3), ret=Date(2027, 3, 2))],
            asked=["February 2027"])
        row = monthly.load_ledger(p)["months"]["February 2027"]
        assert row["best_usd"] == 1347
        assert row["best_depart"] == "2027-02-03"
        assert row["best_ret"] == "2027-03-02"

    def test_a_month_that_answered_nothing_is_still_recorded(self, tmp_path):
        """'Never asked' and 'asked, never answered' are different facts."""
        p = tmp_path / "led.json"
        monthly.record_hints(p, [], asked=["November 2026", "December 2026"])
        months = monthly.load_ledger(p)["months"]
        assert set(months) == {"November 2026", "December 2026"}
        assert months["November 2026"]["best_usd"] is None
        assert months["November 2026"]["asks"] == 1
        assert months["November 2026"]["hits"] == 0

    def test_asks_accumulate_while_hits_do_not(self, tmp_path):
        p = tmp_path / "led.json"
        for _ in range(4):
            monthly.record_hints(p, [], asked=["November 2026"])
        row = monthly.load_ledger(p)["months"]["November 2026"]
        assert row["asks"] == 4 and row["hits"] == 0

    def test_format_puts_the_cheapest_month_first(self, tmp_path):
        p = tmp_path / "led.json"
        monthly.record_hints(p, [
            self.hint(month="January 2027", price=1900),
            self.hint(month="February 2027", price=1347,
                      dep=Date(2027, 2, 3), ret=Date(2027, 3, 2)),
        ], asked=["January 2027", "February 2027", "March 2027"])
        lines = monthly.format_ledger(monthly.load_ledger(p), threshold=1400)
        assert "February 2027" in lines[0] and "under threshold" in lines[0]
        assert "January 2027" in lines[1]
        assert "March 2027" in lines[2] and "no hint yet" in lines[2]

    def test_an_empty_ledger_says_so(self):
        assert monthly.format_ledger({"months": {}}) == [
            "No month hints recorded yet."]


class TestTheWideNetIsPaced:
    """It must not fire 24 requests as fast as the network answers.

    Found 2026-08-23 by reading tracker.log: the 15:45 run sent 24 probes in
    37 seconds at a near-perfect 1.5s cadence, because `scan_months` had no
    delay and no jitter at all. The grid jitters and the sweep jitters; this
    - the one path that runs on every scheduled run, six times a day - did
    neither, making it the most machine-shaped traffic in the project.
    """

    def months(self, n=3):
        return [("January 2027", 2027), ("February 2027", 2027),
                ("March 2027", 2027)][:n]

    def test_it_waits_between_probes(self):
        naps = []
        monthly.scan_months(lambda q: "", self.months(3), sleep=naps.append,
                            delay_s=3.0, jitter_s=2.0)
        assert len(naps) == 2, "one wait between each pair, none before the first"
        assert all(3.0 <= n <= 5.0 for n in naps), naps

    def test_the_wait_is_not_a_metronome(self):
        """A perfectly regular cadence is a fingerprint."""
        naps = []
        months = [(f"Month {i} 2027", 2027) for i in range(30)]
        monthly.scan_months(lambda q: "", months, sleep=naps.append,
                            delay_s=3.0, jitter_s=2.0)
        assert len(set(naps)) > 1, "every wait identical is exactly the problem"

    def test_halves_are_paced_too(self):
        """Halves triple the request count, so they matter most here."""
        naps = []
        monthly.scan_months(lambda q: "", self.months(2), halves=True,
                            sleep=naps.append, delay_s=3.0, jitter_s=0.0)
        # 2 whole-month probes + 4 half-month probes = 6, so 5 waits.
        assert len(naps) == 5, naps

    def test_a_failing_probe_still_paces_the_next(self):
        """Errors must not turn into an unthrottled retry storm."""
        naps = []

        def boom(query):
            raise RuntimeError("network")

        monthly.scan_months(boom, self.months(3), sleep=naps.append,
                            delay_s=3.0, jitter_s=0.0)
        assert len(naps) == 2

    def test_pacing_can_be_switched_off_for_tests(self):
        naps = []
        monthly.scan_months(lambda q: "", self.months(3), sleep=naps.append,
                            delay_s=0.0)
        assert naps == []


class TestTheRequestCountIsReportedHonestly:
    def test_halves_triple_the_probe_count(self):
        """`cli.py` reported the month count as the request count.

        With halves on that is 8 reported against 24 sent, six times a day -
        a 96-request-a-day gap in the number the throttle notes reason from.
        """
        months = monthly.months_in_window(Date(2026, 9, 13), Date(2027, 4, 23))
        assert len(months) == 8
        assert len(monthly.probe_count(months)) == 8
        assert len(monthly.probe_count(months, halves=True)) == 24


class TestHalfMonthHintsFoldIntoTheirMonth:
    """A half-month probe is a finer look at the same month, not a new one.

    `month_halves` labels its probes "January 2027 (1st half)". Keying the
    ledger on that gave three rows per month, so a month whose whole-month
    query returned nothing read "no hint yet" while a half-month row beside
    it held a real price. Found 2026-08-23 before it could happen live -
    that day's hints all came from whole-month queries.
    """

    def hint(self, month, price):
        return MonthHint(month=month, depart=Date(2027, 1, 5),
                         ret=Date(2027, 2, 1), price_usd=price)

    def test_the_suffix_is_stripped(self):
        assert self.hint("January 2027 (1st half)", 1).base_month == "January 2027"
        assert self.hint("January 2027 (2nd half)", 1).base_month == "January 2027"

    def test_a_whole_month_label_is_untouched(self):
        assert self.hint("January 2027", 1).base_month == "January 2027"

    def test_halves_do_not_create_extra_ledger_rows(self, tmp_path):
        p = tmp_path / "led.json"
        monthly.record_hints(p, [
            self.hint("January 2027 (1st half)", 1387),
            self.hint("January 2027 (2nd half)", 1900),
        ], asked=["January 2027"])
        months = monthly.load_ledger(p)["months"]
        assert list(months) == ["January 2027"], months

    def test_a_half_month_price_counts_as_the_months_best(self):
        """The whole point of asking about halves at all."""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "led.json"
            monthly.record_hints(p, [self.hint("January 2027", 1900)],
                                 asked=["January 2027"])
            monthly.record_hints(p, [self.hint("January 2027 (2nd half)", 1387)],
                                 asked=["January 2027"])
            row = monthly.load_ledger(p)["months"]["January 2027"]
            assert row["best_usd"] == 1387


class TestTheLedgerDisplayIsScopedToSearchedMonths:
    def ledger(self, tmp_path):
        p = tmp_path / "led.json"
        monthly.record_hints(p, [
            MonthHint(month="September 2026", depart=Date(2026, 9, 1),
                      ret=Date(2026, 9, 28), price_usd=1700),
            MonthHint(month="January 2027", depart=Date(2027, 1, 5),
                      ret=Date(2027, 2, 1), price_usd=1400),
        ], asked=["September 2026", "January 2027"])
        return monthly.load_ledger(p)

    def test_months_no_longer_searched_are_hidden(self, tmp_path):
        lines = monthly.format_ledger(self.ledger(tmp_path),
                                      only=["January 2027"])
        assert len(lines) == 1 and "January 2027" in lines[0]

    def test_the_data_itself_is_kept(self, tmp_path):
        """A price seen in September is still a true fact about September."""
        assert "September 2026" in self.ledger(tmp_path)["months"]

    def test_no_filter_shows_everything(self, tmp_path):
        assert len(monthly.format_ledger(self.ledger(tmp_path))) == 2
