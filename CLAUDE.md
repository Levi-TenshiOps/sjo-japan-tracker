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

## What makes Google block you, in order of importance

Learned the hard way on 2026-08-23, after roughly an hour of hard
throttling that a fifteen-minute rest could not clear.

**1. A fresh browser profile every launch.** This is the big one and it is
not about speed. Without `--user-data-dir`, Chrome starts blank each time:
no cookies, no history, no session. A 4,000-window sweep then looks like
four thousand brand-new browsers from one address, each running exactly one
flight search and never coming back. No amount of pacing repairs that - a
perfectly timed request from a browser that has never existed before is
still obviously not a person. `browser.fetch_dom` now reuses
`.chrome-profile`, so the cookies Google sets persist and the traffic reads
as one browser returning.

**2. Two processes at once.** Doubling the rate took the hit rate from 87%
to 24% in minutes. Enforced by `gate.py`; see the section below.

**3. Rate.** It matters, but less than the two above. At a 10s delay the
sweep is ~360 requests/hour, and it ran cleanly at 87% for hours at 6s.

**Do not diagnose a throttle by making more requests.** That was the
mistake that turned a short throttle into an hour of one: each diagnostic
probe was itself another request to a host already refusing. When the
health line says throttled, the only useful action is to stop entirely and
wait. `sweep_forever.py` now escalates its rests - 15 minutes, then 30,
then an hour - because a fixed rest simply cycles.

## Never query Google from two places at once

This is enforced by `tracker/gate.py`, not left to discipline, and the
enforcement is there because being careful demonstrably was not enough.

Measured 2026-08-23: a second process pricing windows alongside the sweep
took the hit rate from 87% to 24%. Google answered in 3-4 seconds with
empty pages, and the sweep recorded each one as "no fares on this date".
The failure is silent - no error, no exception, just windows quietly
written off - which is what makes it dangerous.

And the collision was structural, not an accident of testing. The sweep
runs continuously and the scheduled tracker runs six times a day, so they
were always going to overlap, roughly every four hours, forever.

So every path that reaches Google takes the lock first: the sweep around
each window, `cli.py` around each of its three search phases (wide net,
HTTP grid, Chrome verification), and any diagnostic script anybody ever
writes. **If you add a script that queries Google, wrap it in
`gate.google("your-name")`.** There is no exception for "just one quick
check" - that is exactly what caused this.

The granularity matters. The sweep takes and releases per *window*, so it
never holds the lock more than ~20 seconds and a scheduled run waits one
window rather than queuing behind a fourteen-hour pass.

On timeout the waiter proceeds anyway rather than raising. A scheduled run
that skips its email because a lock file was untidy is a worse outcome than
one extra concurrent request, and the throttle detection catches the
latter. Stale locks - dead PID, or no heartbeat for ten minutes - are
broken rather than waited out.

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
tracker/browser.py       Chrome headless fetch + DOM parse  <- sees what HTTP hides
tracker/verify.py        re-price the windows that matter, via Chrome
tracker/monthly.py       the wide net: Google's own cheapest-date hints
tracker/sweeper.py       endless full-coverage sweep, resumable
tracker/cli.py           orchestration
sweep_forever.py         run the sweep as a separate long-lived process
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

**There is no maximum-stay rule. That conclusion was wrong.** It looked
airtight: from several departure dates, 19-30 nights each returned 9-18
options and 31 nights upward returned nothing, every time, reproducibly.
It was read as the standard 30-day max-stay fare rule and a
`MAX_STAY_NIGHTS = 31` cap was added.

The trip owner then produced a live Google Flights result for SJO-NRT
departing 2027-02-05, returning 2027-03-09 — **32 nights, $1,390**, on
Edelweiss plus SWISS via Zurich. The fare exists. The cap had silently
excluded the exact flight they were tracking.

What is actually happening: **stays past ~30 nights are missing from the
server-rendered HTML this project scrapes.** Fetching the trip owner's own
URL — byte-identical `tfs`, same airports, no stop limit — returns a page
with zero prices in it, while that same URL in a browser shows the $1,390
fare. The plain-text query path cannot reach it either. Results are stable
and reproducible (4/4 identical across spaced retries), so this is not
throttling; it is a boundary in what Google renders without JavaScript.

The lesson is bigger than the number: **an empty response never proves a
fare does not exist.** It proves only that this fetch method did not see
one. Never infer a fare rule from silence again. `MAX_STAY_NIGHTS` is now
60 and exists only to stop a typo generating a useless grid.

**The HTTP fetch systematically misses the cheapest fares.** This is the
single most important thing in this file. It is not only long stays: on the
trip owner's own target window, SJO-Tokyo 2027-01-29 to 2027-02-25 (27
nights, comfortably inside the grid), the HTTP grid reported a cheapest of
$1,635 via Frankfurt. The real cheapest was **$1,347** on Edelweiss/SWISS
via Zurich, 46 hr 20 min, confirmed on Google's own booking page. That
routing does not appear in the server-rendered HTML at any stop limit, in
any currency or locale.

Since the alert threshold is $1,400, the fares worth emailing are precisely
the ones HTTP cannot see. A tracker built on HTTP alone is not incomplete,
it is wrong in the only direction that matters.

`tracker/browser.py` fixes it with Chrome's own `--headless --dump-dom`, so
there is no new Python dependency — `selectolax` already ships with
fast-flights. A launch costs ~25s against ~3s, so `tracker/verify.py`
spends a small fixed budget (`chrome_max_per_run`) on the windows most
likely to be cheap: wide-net hints first, then the hot list, then the
grid's best guess. Chrome's answer wins whenever it is lower, and it drives
both the alert price and a dedicated block at the top of the email.

`--dump-dom` returns the collapsed result list, so an option carries price,
airline, total duration and the connection airports but **not** per-leg
clock times — those live in a panel that only renders on click. That is why
a Chrome result is a `BrowserOption` and not an `Itinerary`: nothing is
invented to fill the gap. The connection airports are what matter, because
they are what the visa rule needs, and an option whose routing could not be
read fails closed rather than being treated as clean.

**Audited 2026-08-22: HTTP lost every window.** Four windows, both sides
visa-filtered so the comparison is fair:

    window              HTTP visa-free   Chrome    HTTP missed
    2027-01-29 +27n           $2,509     $1,347        $1,162
    2026-11-30 +29n           $2,866     $1,432        $1,434
    2027-02-12 +28n           $3,057     $2,688          $369
    2027-03-05 +28n           $3,179     $3,093           $86

An earlier version of this audit compared *unfiltered* HTTP against
filtered Chrome and made HTTP look better than it is. Filter both sides.

**The cheap fares are Lufthansa Group through Europe.** An earlier note
here claimed "Zurich is where the cheap fares are, and it is basically
alone". That was generalised from two ZRH sightings and is wrong. Priced
across 16 windows spanning all eight months, tallying every visa-free
routing by connecting hub:

    hub   seen      min   median   <=$1,600
    FRA     14    1,558    2,340          2
    MUC      2    1,558    1,694          1
    CDG      5    1,954    2,193          0
    AMS      4    1,964    1,985          0
    MAD      6    2,128    2,790          0
    MEX     17    2,303    2,993          0
    MTY      9    2,303    2,880          0
    PVR      8    2,758    3,105          0
    ICN      1    6,108    6,108          0

Zurich did not appear at all in that sample, yet ZRH carries the cheapest
fare found anywhere so far ($1,347). Both facts are true: the cheap
European routings are Lufthansa Group - Frankfurt, Munich and Zurich, with
SWISS and Edelweiss inside that group - and which of the three shows up
depends on the dates. Amsterdam and Paris are a clear second tier. Every
Latin American hub (MEX, MTY, PVR, PTY, SAL) was expensive in every window
priced, without exception.

The practical consequence is *not* to filter to a favourite hub. Forcing
`connecting_airports=[ZRH]` was measured and returns the same fare when one
exists and nothing when it does not, so a general query strictly dominates.
The lesson is about where to look when a result seems too expensive: if no
European Lufthansa-group routing appears, the search probably did not see
everything.

**The wide net cannot see Haneda.** "Flights from SJO to HND in <month>"
returns no recommendation at all, and every rephrasing tried ("to Tokyo",
"cheapest round trip", "for 4 weeks") returns exactly what "to NRT" does.
That is why `monthly_scan_destination` is a single airport while
`chrome_destination` is the metro code TYO: Chrome *can* take TYO and
returns both airports for one launch.

**How complete the sweep actually is.** Tested 2026-08-23, because
"complete" is a claim worth checking rather than asserting.

*Combinations:* 223 departure days x 18 trip lengths = 4,014, and
`generate_windows` emits exactly 4,014. Nothing inside the configured
constraints is skipped.

*Stops:* `max_stops=2` re-tested through Chrome, since the earlier answer
came from the HTTP path that cannot see the cheap European routings and so
proved nothing:

    window              1 stop     2 stops     3 stops   unlimited
    2027-01-29 +27n     $1,347      $1,347      $1,347      $1,347
    2026-11-30 +29n     $2,687      $1,432      $1,432      $1,432

Two is the knee. One costs $1,255 on the second window; three and beyond
add nothing at all.

*Truncation:* Google reported "16 results returned" while the parser found
13, and the page carries a "View more flights" control that `--dump-dom`
cannot click. That sounds alarming and is not: re-querying the same window
with `max_price` caps of $1,500, $1,900 and $2,600 surfaced **no option the
uncapped query had missed** until $2,600, where the single new row was a
$2,531 US transit the visa rule rejects anyway. The rows behind that
control are the dear ones; cheap fares are not hidden there.

What the sweep therefore does *not* guarantee: that a price is current (a
full pass takes ~21 h, so a finding can be that old), that anything outside
21-38 nights or outside NRT/HND is seen at all, or that Google itself lists
every carrier. Only the cheapest visa-free option per window is kept, by
design - the baseline gets the rest through `sweep_history.csv`.

**Coverage is the honest limit.** Chrome prices `chrome_max_per_run` (20)
windows a run, 120 a day, against a search space of ~4,000. Everything else
is priced by HTTP, whose numbers run hundreds of dollars high. So the email
leads with the Chrome block and labels the HTTP table an upper bound. Do
not quote an HTTP price as "the cheapest" anywhere.

**The wide net beats the grid at finding fares.** `tracker/monthly.py`
sends one plain-text query per month ("Flights from SJO to NRT in February
2027") and reads Google's own recommendation out of the prose:

    Travel Jan 29 - Feb 25, 2027 for $1,347

Eight requests covered the whole window and named $1,663, $1,604, $1,432
and $1,347 — while 26 grid requests the same afternoon bottomed out at
$2,197. Three months in eight returned no hint, which is normal. The hint
carries no routing, so it is a *candidate*: it goes to the front of the hot
list and the ordinary search prices it, with `itinerary.validate()` still
ruling on the visa. Use the grid to track a fare; use the net to find one.

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
