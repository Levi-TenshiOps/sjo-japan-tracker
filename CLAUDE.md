# Notes for Claude Code

Read this before changing anything. The project is built and tested; what
remains is live verification, not a rewrite.

## Non-negotiables

1. **`tracker/airports.py` is safety-critical.** Never widen
   `BANNED_AIRPORTS`. US and Canadian airports are banned because a
   consular transit visa is required — an airside connection is not a
   loophole. New hubs need a researched tier and a test.
2. **Never trust Google's `connecting_airports` filter alone.** It is an
   include-hint. `itinerary.validate()` re-checks every leg. Do not remove
   that second pass as a redundant optimisation; it is the guarantee.
3. **Two emails per day is a hard cap, and the second is reserved.**
   `alerts.decide()` owns both rules.
   `tests/test_alerts.py::TestCheapestWinsTheLastSlot` must keep passing.
6. **The priority quota shapes membership, never order.** `ranking.py`
   guarantees priority months a share of the 20 slots, then sorts by price.
   The displayed list must always be strictly cheapest-first, and the single
   cheapest fare found must always appear regardless of its month.
4. **Prices are USD.** Google serves CRC to a Costa Rican IP. Currency is
   forced in `search.build_query`. Never let CRC into the email.
5. **No personal data in tracked files.** `config.yaml` holds search
   parameters only; `preferences.json` and `.env` are gitignored. The
   repository must stay publishable as-is.

## Layout

```
tracker/airports.py      visa allow/deny sets                <- safety-critical
tracker/preferences.py   the four setup answers
tracker/schedule.py      rolling window, hot list, dual rotation
tracker/ranking.py       top-N with priority-month quota
tracker/throttle.py      adaptive request budget
tracker/itinerary.py     normalise, duration maths, validate
tracker/pricing.py       cheap/typical/expensive bands
tracker/alerts.py        email budget state machine
tracker/search.py        fast-flights wrapper, deep links
tracker/email_render.py  HTML + text email
tracker/history.py       CSV log and baseline
tracker/notify.py        SMTP and ntfy
tracker/cli.py           orchestration
setup_tracker.py         one-time wizard
install_schedule.py      launchd / systemd / cron installer
```

## Four subtleties that look like bugs but are not

**Duration.** Leg durations are true elapsed minutes; local clock times
carry no timezone. Total = sum(legs) + sum(layovers), each layover computed
at a single airport so subtraction is valid. Do not simplify to last arrival
minus first departure — it will be wrong by the timezone offset. The test
asserting 46 hr 20 min is checked against a real Google screenshot.

**History baseline.** Needs 25+ observations across 5+ distinct days. One
run can log 25 rows, and percentiles over a single snapshot describe that
snapshot. Found in integration testing; do not relax it.

**The held email slot.** When the price improves before `last_call_hour`,
`decide()` deliberately returns `should_send=False` with `notes=["held"]`.
That is not a missed alert. Spending the slot at 16:00 means a cheaper fare
at 21:00 could never be reported. Found in a full-day simulation.

**Request budget is per run, not per day.** `throttle.budget` is what one
run may spend. Multiply by runs/day for the daily figure. Runs/day is only
passed in for reporting.

**Two rotation cursors.** `RotationState` tracks `priority_index` and
`slice_index` separately. One shared cursor would make the smaller priority
pool wrap constantly while the larger general pool crawled.

**Fewer than 20 rows is not a bug.** If only 16 itineraries survived
filtering, the email shows 16. The selector cannot invent options. Likewise
`priority_count` below half is correct when fewer priority-month options
exist than slots reserved — the reservation is released, not left empty.

## Verify on the first live run

The offline suite is complete, but the build container had no access to
Google, so these need real traffic:

1. **Throttling.** Does ~24 requests/run stay clean? Watch the empty rate in
   the log; the throttle adapts on its own but confirm it settles rather
   than collapsing to the floor of 8.
2. **`extract_google_bands`.** Written blind against an undocumented
   payload; almost certainly needs adjusting. It returns `None` safely
   today, so seed bands are used and everything still works. Capture one
   real HTML payload, save it as a fixture, write a test, then fix it.
3. **Email rendering.** Send one to yourself. Check Gmail mobile, Gmail dark
   mode, and Outlook. Layout is table-based and inline-styled for this.
4. **Field mapping.** Confirm `Flights.airlines` holds display names, not
   IATA codes; the email prints them directly.
5. **Scheduler.** After `install_schedule.py`, confirm the job actually
   fires (`systemctl --user list-timers`, or `launchctl list | grep
   flighttracker`) and that `tracker.log` grows.

## Tasks, in order

- [ ] `python -m pytest tests/ -q` — expect 365 passing
- [ ] `python setup_tracker.py`, then `--status`, then `--dry-run`
- [ ] One real run; inspect the email in a real client
- [ ] `python install_schedule.py`; confirm it fires
- [ ] Watch a week; check `throttle.json` settled somewhere sensible
- [ ] Tune thresholds toward $1,250 / $1,100
- [ ] Fix `extract_google_bands` against a captured payload
- [ ] Add `plan_open_jaw()` once real dates are set (Tokyo in, Seoul out)

## Deployment

Recommend a home machine, not the cloud. A datacenter IP is the single most
likely cause of blocking, which is why the GitHub Actions scraping workflow
was deliberately removed. `.github/workflows/test.yml` stays — running the
offline test suite in CI is fine.

## Style

Type hints, dataclasses, no new runtime dependencies without a reason. Every
bugfix gets a regression test. Tests stay offline — inject a fake through
`Searcher(fetch=...)`, never hit the network.
