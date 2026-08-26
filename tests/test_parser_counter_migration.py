"""The parser alarm must not fire on a counter that changed meaning.

Found 2026-08-25 during a full review. `rows_missed_by_parser` stood at
40 against an alarm at 25, and `parser_alarm_sent` was still False - so
the next scheduled run would have emailed "results are arriving in a
format we cannot read", meaning Google's markup had moved and fares were
silently vanishing.

It had not. Measured off the live log:

    pre-fix  'unreadable' : 40   in 20 windows of exactly 2
    post-fix 'unpriced'   :  2
    post-fix 'unreadable' :  0

Twenty windows carrying exactly two is the fingerprint of Google's own
"Total price is unavailable" row, which the DOM carries twice. `_NO_PRICE`
split those out on 2026-08-25 and fixed the counting from then on - but
nothing migrated the value already banked under the old meaning.

A fix that redefines a counter has to migrate the counter, or the old
number goes on driving the alarm.
"""

import json

from tracker.cli import PARSER_ALARM_ROWS
from tracker.sweeper import STORE_VERSION, SweepStore


def _write(tmp_path, **fields):
    p = tmp_path / "store.json"
    data = {"version": STORE_VERSION}
    data.update(fields)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestTheStaleCounterIsDropped:
    def test_a_pre_fix_count_does_not_survive_the_load(self, tmp_path):
        p = _write(tmp_path, rows_missed_by_parser=40)
        assert SweepStore.load(p).rows_missed_by_parser == 0

    def test_it_drops_below_the_alarm(self, tmp_path):
        p = _write(tmp_path, rows_missed_by_parser=40)
        assert SweepStore.load(p).rows_missed_by_parser < PARSER_ALARM_ROWS

    def test_the_flag_is_set_so_it_happens_once(self, tmp_path):
        p = _write(tmp_path, rows_missed_by_parser=40)
        s = SweepStore.load(p)
        assert s.parser_counts_migrated is True

    def test_a_real_count_after_migration_is_kept(self, tmp_path):
        """The whole point is that genuine markup breakage still alarms."""
        p = _write(tmp_path, rows_missed_by_parser=31,
                   parser_counts_migrated=True)
        s = SweepStore.load(p)
        assert s.rows_missed_by_parser == 31
        assert s.rows_missed_by_parser >= PARSER_ALARM_ROWS

    def test_it_survives_a_save_and_reload(self, tmp_path):
        p = _write(tmp_path, rows_missed_by_parser=40)
        s = SweepStore.load(p)
        s.rows_missed_by_parser = 26      # genuine breakage after the reset
        s.save(p)
        again = SweepStore.load(p)
        assert again.rows_missed_by_parser == 26, (
            "migrating twice would mask a real markup change")

    def test_unpriced_is_left_alone(self, tmp_path):
        p = _write(tmp_path, rows_missed_by_parser=40, rows_unpriced=2)
        assert SweepStore.load(p).rows_unpriced == 2
