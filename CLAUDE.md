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
3. **Two emails a day, every day, and the second is reserved.**
   `alerts.decide()` owns both rules. The cap of two is hard. With
   `daily_digest` on (the default) the price threshold no longer gates
   delivery: both emails go out whatever the market did. What must not
   change is the reservation — the second email is still held until
   `last_call_hour` so it carries the day's *cheapest* fare, not merely its
   most recent. A digest that always arrives is only worth having if it
   still ends on the best number of the day. Both
   `tests/test_alerts.py::TestCheapestWinsTheLastSlot` and
   `tests/test_digest_and_greeting.py::TestDigestAlwaysSendsTwo` must keep
   passing; the first covers threshold mode, the second covers digest mode.
4. **The priority quota shapes membership, never order.** `ranking.py`
   guarantees priority months a share of the 20 slots, then sorts by price.
   The displayed list must always be strictly cheapest-first, and the single
   cheapest fare found must always appear regardless of its month.
5. **At most three priority months.** `preferences.MAX_PRIORITY_MONTHS`.
   The quota reserves a share of 20 fixed slots; spread over more than three
   months each month's guaranteed share falls below a single row and the
   quota silently degenerates into the plain cheapest-first list it exists
   to protect against.
6. **The greeting is fixed and non-ASCII.** `email_render.GREETING` is
   `Hello Nakama (仲間),` and appears in both the HTML and the plain-text
   part. Anything that renders, stores or transports the email has to stay
   UTF-8 clean end to end — `tests/...::test_kanji_survives_the_smtp_round_trip`
   is what catches a regression to latin-1.
7. **Prices are USD.** Google serves CRC to a Costa Rican IP. Currency is
   forced in `search.build_query`. Never let CRC into the email.
8. **No personal data in tracked files.** `config.yaml` holds search
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

## What Google will and will not give you

Measured against live traffic on 2026-08-22. These are the facts that decide
how much of the 8-month window the tracker can actually see, so re-measure
before trusting any of them again.

**One request buys exactly one (depart, return) window.** The response
carries no price calendar: parsing a real payload finds only the queried
departure date and its adjacent arrival days. The date-grid and price-graph
panels do not help either — `tfu=` variants return the same four dates,
because that data arrives over a separate internal RPC, not in the HTML.
So coverage is bounded by request count, full stop. There is no clever
query that prices a month at once.

**The departure grid must be dense, not frequent.** With
`departure_step_days: 4` only 55 of 223 bookable departure days were *ever*
priced; the other 168 were permanently invisible, not merely stale. Density
beats revisit rate here: for a deal lasting D days, a step-1 grid catches it
with probability `min(1, D/sweep)` across every date, while a step-2 grid
catches `0.5 x min(1, D/(sweep/2))` — identical for short deals and strictly
worse for long ones. Hence `departure_step_days: 1`. The hot list is what
keeps a found deal fresh; the cold sweep only has to *discover* it.

**Round-trip fares have a ~30-night maximum stay.** SJO-TYO from
2026-11-10: 28n gave 18 options ($1,805), 29n gave 16 ($1,679), 30n gave 16
($1,404), 31n gave 1 ($1,722), and 32n/33n/35n gave nothing at all — on
every departure date tried. Two consequences. A 5-week round trip is not
expensive, it does not exist, so searching it burnt a quarter of every run;
`MAX_STAY_NIGHTS` now drops it. And the cheapest fares sit *just inside*
the boundary, which whole-week trip lengths step straight over — that is
what `extra_nights: [30]` is for.

**The measured effect of dropping 35n.** Two live runs, 26 requests each,
same afternoon: before, 31 usable options and a 38% empty rate; after, 43
usable options and 23%. Same request cost, 39% more usable fares, and the
30-night length turned out to be the single most productive one (13 of the
43 rows). That is the shape of the win — not more requests, fewer wasted
ones.

**SJO-OSA returns nothing, at any stop count.** Not a `max_stops` artefact:
1, 2, 3 and unlimited stops all return zero on every date tried, while TYO
returns 12-14 on the same dates. The probation machinery in `schedule.py`
demotes it automatically after `DEST_PROBATION_AFTER` fruitless searches and
re-probes every `DEST_PROBE_EVERY` runs, which is the right shape: cheap to
keep watching in case a route opens, free the rest of the time.

**`max_stops: 2` is correct and must not be tightened.** 1 stop found 5
options at $1,950; 2 stops found 14 at $1,853. Going to 3 or unlimited added
nothing. So 2 is exactly the knee — dropping to 1 would miss real fares.

**An empty response is usually real.** The discriminator is whether the
payload contains any `$` price at all: a genuine no-results page has zero,
a good one had 96. "Loading results" appears in *every* payload including
successful ones and means nothing. `fast_flights.parser` raises
`TypeError: 'NoneType' object is not subscriptable` on the no-results shape
rather than reporting it cleanly, which is why `search.py` catches it and
treats it as empty — that is correct, not a swallowed bug.

## Still to verify on real traffic

Done and no longer open: `extract_google_bands` now returns GOOGLE-sourced
bands on live payloads (`tests/payload_ds1.html` is the trimmed fixture);
the metro-code, max-stops, max-stay and OSA questions are all settled above.

1. **Throttling over a full week.** One run at 26 requests came back with a
   36-38% empty rate *before* the 35-night lengths were dropped. Confirm the
   rate falls now that a quarter of the grid is no longer structurally
   empty, and that `throttle.json` settles somewhere sensible rather than
   collapsing to the floor of 8 or pinning at the ceiling of 40.
2. **Email rendering in a real client.** Send one to yourself and check
   Gmail mobile, Gmail dark mode and Outlook. Layout is table-based and
   inline-styled for exactly this. Pay attention to the greeting: the kanji
   in `GREETING` is the thing most likely to render as boxes in a client
   with a poor font stack.
3. **Field mapping.** Confirm `Flights.airlines` holds display names, not
   IATA codes; the email prints them directly.
4. **Scheduler.** After `install_schedule.py`, confirm the job actually
   fires (`systemctl --user list-timers`, or `launchctl list | grep
   flighttracker`) and that `tracker.log` grows.
5. **Whether 4 runs/day is enough.** At 26 requests x 4 runs the general
   pool sweeps every ~19 days, which is a long time to discover a new deal.
   More runs per day is the only lever that shortens it — the grid is
   already as dense as it can be. Raise it only while watching the empty
   rate and `consecutive_bad`; a datacenter-shaped traffic pattern is what
   gets an IP blocked.

## Tasks, in order

- [x] `python -m pytest tests/ -q` — 434 passing
- [x] `python setup_tracker.py`, then `--status`, then `--dry-run`
- [x] Fix `extract_google_bands` against a captured payload
- [ ] One real run; inspect the email in a real client
- [ ] `python install_schedule.py`; confirm it fires
- [ ] Watch a week; check `throttle.json` settled somewhere sensible
- [ ] Decide the alert thresholds. `preferences.json` currently says
      $2,600 / $2,200 while `CLAUDE.md` has long said to aim at
      $1,250 / $1,100 and the sample email was built at $1,380. Live
      SJO-TYO fares are landing around $1,400-$2,200, so $2,600 is loose
      enough that almost everything "qualifies". Under `daily_digest` this
      no longer decides whether mail arrives, only how it is framed and
      which rows are highlighted — but it should still be a number that
      means something.
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
