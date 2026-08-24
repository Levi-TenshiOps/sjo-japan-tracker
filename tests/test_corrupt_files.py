"""Every file this project reads, fed garbage.

Written after 2026-08-24, when one line reading `0` in sweep_history.csv
crashed every scheduled run for four hours. Auditing for the same shape
found it in four more readers, including one written twenty minutes after
the original fix.

The shape is always the same and always surprising: a truncated write
leaves *valid* content that is not the *expected* content. `json.loads("0")`
succeeds and hands back an int; `.get()` on an int raises AttributeError
three frames from where anyone would look. `csv.DictReader` fills a short
row's fields with None; `.upper()` on None does the same.

Two policies, and which file gets which is deliberate:

* **Runtime state** - state.json, throttle.json, discoveries.json,
  month_hints.json and the history CSVs - degrades. It is regenerable, and
  a corrupt copy must never stop the tracker.
* **User configuration** - config.yaml, preferences.json - fails loudly,
  with a clean error naming the file. Silently ignoring a broken config
  would search the wrong thing and never say so.

Neither is allowed to raise something raw from deep inside a parser.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pathlib

import pytest

from tracker import alarm, config, history, monthly
from tracker.alerts import AlertState
from tracker.config import ConfigError
from tracker.preferences import Preferences, PreferencesError
from tracker.sweeper import SweepStore
from tracker.throttle import ThrottleState

# "0" is the one that actually happened. The rest are the neighbouring
# shapes a half-finished write or a stray editor can leave.
GARBAGE = [
    pytest.param("", id="empty"),
    pytest.param("0\n", id="the-line-that-broke-production"),
    pytest.param("{ not json", id="truncated-object"),
    pytest.param("null", id="valid-json-null"),
    pytest.param("[]", id="valid-json-list"),
    pytest.param('"a string"', id="valid-json-string"),
    pytest.param("]]]]", id="junk"),
    pytest.param("\x00\x01\x02binary", id="binary"),
    pytest.param("a,b,c\n1,2\n", id="short-csv-row"),
    pytest.param("\n\n\n", id="blank-lines"),
]

# Loader, and the one exception type it is allowed to raise.
DEGRADES = [
    ("state.json", lambda p: AlertState.load(p)),
    ("throttle.json", lambda p: ThrottleState.load(p)),
    ("discoveries.json", lambda p: SweepStore.load(p)),
    ("month_hints.json", lambda p: monthly.load_ledger(p)),
    ("history.csv", lambda p: history.read_prices(p, origin="SJO")),
    ("history.csv", lambda p: history.distinct_days(p, origin="SJO")),
    ("state.json", lambda p: alarm.hours_since_last_email(p)),
]
FAILS_CLEANLY = [
    ("config.yaml", lambda p: config.load(p, use_env=False), ConfigError),
    ("preferences.json", lambda p: Preferences.load(p), PreferencesError),
]


@pytest.mark.parametrize("junk", GARBAGE)
@pytest.mark.parametrize("name,loader", DEGRADES,
                         ids=[f"{n}-{i}" for i, (n, _) in enumerate(DEGRADES)])
def test_runtime_state_degrades_rather_than_raising(tmp_path, name, loader, junk):
    p = tmp_path / name
    p.write_text(junk, encoding="utf-8", errors="ignore")
    loader(str(p))          # must not raise, whatever is in the file


@pytest.mark.parametrize("junk", GARBAGE)
@pytest.mark.parametrize("name,loader,expected", FAILS_CLEANLY,
                         ids=[n for n, _, _ in FAILS_CLEANLY])
def test_user_config_fails_with_its_own_error(tmp_path, name, loader,
                                              expected, junk):
    """A broken config must stop the run - but say so in its own words."""
    p = tmp_path / name
    p.write_text(junk, encoding="utf-8", errors="ignore")
    try:
        loader(str(p))
    except expected as exc:
        assert name in str(exc) or "not found" in str(exc), str(exc)
    except Exception as exc:                       # noqa: BLE001
        pytest.fail(f"raised {type(exc).__name__} instead of "
                    f"{expected.__name__}: {exc}")


def test_a_missing_file_is_not_a_corrupt_one(tmp_path):
    """Absent state is a fresh install, not damage."""
    for name, loader in DEGRADES:
        loader(str(tmp_path / f"absent-{name}"))


def test_the_exact_production_failure(tmp_path):
    """A CSV whose last line was cut mid-write, with good rows around it."""
    p = tmp_path / "sweep_history.csv"
    p.write_text(
        "checked_at_utc,origin,destination,depart_date,return_date,price_usd,"
        "duration_min,stops,hubs,airlines,band,band_source,deep_link\n"
        "2026-08-24T13:37:15+00:00,SJO,TYO,2026-11-03,2026-12-03,3087,1569,2,"
        "MEX+PVR,Aeromexico,TYPICAL,CHROME,\n"
        "0\n"
        "2026-08-24T13:41:03+00:00,SJO,TYO,2027-01-24,2027-02-17,1720,2805,1,"
        "FRA,Lufthansa,TYPICAL,CHROME,\n", encoding="utf-8")
    assert history.read_prices(str(p), origin="SJO",
                              band_source="CHROME") == [3087, 1720]
