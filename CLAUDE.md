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

## Why the HTTP grid is kept, though it cannot find a bargain

It was audited for removal on 2026-08-23 and survived, but only just, and
for reasons that have nothing to do with finding cheap fares.

Across the 70 windows it has priced, the grid found **zero** fares at or
under the $1,400 alert threshold. Its cheapest ever is $1,635 against
Chrome's $1,347. It wins occasionally on a shared window - 7 of 31 - but
every one of those wins landed between $2,133 and $2,398, far above
anything that could trigger an alert, and most likely reflects the two
methods pricing at different moments rather than a real advantage.

So its *coverage* role is finished: the sweep prices every window through
Chrome, which sees the European routings the grid structurally cannot.

Two things keep it alive:

* It is the only thing that produces an email when the sweep is down. The
  run aborts on an empty `accepted` list, so without the grid a stopped
  sweeper means silence rather than a thinner email.
* Its rows are real, bookable fares. They are never the cheapest, but they
  give the market some shape beside the verified block.

The compromise is `grid_budget_when_swept`. When the sweep has at least
`swept_enough` fresh findings the grid is cut to roughly what filling the
email table needs (16 requests at a measured ~1.4 usable options each);
when the sweep is quiet it keeps its full budget and carries the email
alone. The trim only ever lowers the budget, so an already-throttled run
is not pushed back up.

Combined effect of the tiering and this trim: **~7,570 requests a day down
to ~1,260, an 83% reduction**, while the fares that can actually alert are
checked more often than before.

## The sweep is tiered, because most windows can never matter

Measured 2026-08-23 across 400 priced windows:

    at or under $1,400 (the alert threshold)     5 windows   1%
    at or under $1,600                          15 windows   4%
    at or under $2,000                         186 windows  46%

**Read that sample carefully - re-audited 2026-08-23 and it is not what it
looks like.** Two biases, both mine:

* Those 400 are not a sample of the search space. `MAX_ENTRIES = 400` and
  `SweepStore.prune` keeps the *cheapest* 400 of everything seen, so the
  distribution is the cheap tail of 1,604 priced windows, not 400 typical
  ones. Against all 1,604, the sub-$1,400 share is 0.3%, not 1%.
* An earlier version of this note added "and every one of the fifteen sat
  in January or February", and used it to argue the other months are dear.
  That is circular. `sweep_order` walks the priority months first, and at
  cursor 1486 of 4014 the sweep had priced January, February and March and
  **nothing else** - September to December 2026 and April 2027 had never
  been priced at all. Every window looked at was in January to March, so of
  course every cheap one was too.

This is the same mistake as the max-stay rule, in a new costume: concluding
something about dates that were never queried. The honest statement is that
January and February are the cheapest months *among the three looked at*.

The tiering itself is unaffected, because the hot list is chosen per window
by observed price (within 1.3x of the best, or under the threshold) and
never by month. It is only the justification that was overstated.

What actually closes the gap is `monthly.record_hints`: the wide net asks
about all eight months, six times a day, and until 2026-08-23 it logged the
answers and threw them away. They are now kept in `month_hints.json` and
printed by `cli.py --status`, so the 8-month picture arrives in hours
instead of waiting ~3.5 days for the sweep's cold half.

**It paid off on the first run.** The 15:45 pass on 2026-08-23 recorded:

    February 2027   $1,347   2027-01-29 -> 2027-02-25
    December 2026   $1,432   2026-11-30 -> 2026-12-29
    October  2026   $1,604   2026-09-30 -> 2026-10-30

December is the point. $1,432 is cheaper than every window the sweep had
found outside its top five, Chrome confirmed it on the spot (14 options, 11
visa-rejected, cheapest usable $1,432) - and the sweep had never priced a
single December window and would not have for another 3.5 days. So the
cheap fares are *not* confined to January and February. Do not re-derive
that claim from whatever the sweep happens to have covered.

Sweeping all 4,014 windows at equal priority still spent about 96% of its
requests on dates that were not producing alerts - roughly 5,800 requests a
day to one address, which is what got it throttled.

One launch in four now goes to a window already known to be cheap (within
1.3x of the best fare seen, or under the alert threshold); the rest
continue the cold rotation. At the default 90-second delay that is ~900
requests a day: each hot window re-priced every ~4 hours, and a full sweep
of everything else every ~6 days. Both better and six times lighter than
what came before.

A quarter was an upper bound, and it is now a *cap* rather than a setting.
Re-audited 2026-08-23: a quarter is also far more than freshness needs. 41
hot windows against a 10-hour limit (`sweep_max_age_hours`, past which the
email drops the row) need 4.1 launches an hour; a 90-second delay supplies
37.5, so a quarter of them was 9.4 an hour. More than half the hot budget
was re-pricing windows nowhere near going stale, and every one of those was
a cold window not covered.

`needed_hot_share` derives it per launch instead, capped at `HOT_SHARE` so
it can only ever spend less than the old behaviour:

    delay   requests/day   hot share   full pass (3,690 windows)
      90s            899         11%     5.5d -> 4.6d
      60s          1,307          8%     3.8d -> 3.1d
      45s          1,691          6%     2.9d -> 2.3d
      30s          2,393          4%     2.1d -> 1.6d

It is derived, not tuned, because the inputs move: the hot list grows as
cheap windows are found, and the rate changes with `--delay`.

One trap here, found by a property test rather than by reading: the share
becomes an integer launch interval in `next_window`, and rounding that
interval *up* silently spends less on freshness than was asked for. It
truncates now, which can only make the interval shorter.

And the cold half is not optional - chasing only known bargains would never
notice a new one appearing somewhere cold. A window that has never been
priced is always taken when the cursor reaches it, for the same reason.

## The months searched are named, not a side-effect of the horizon

`included_months: [1, 2, 3, 10, 11, 12]`. Only those six are searched.

This started as `excluded_months: [9]` to drop September, and the trip
owner immediately found the flaw: "why April?" Nobody had chosen April. It
was there because it was the tail of an 8-month rolling horizon, and
September had been there for the same reason at the other end. Filtering
the horizon meant the horizon was still deciding the search.

So `search_months` is now **only the horizon** - how far ahead to look for
the months actually named - and `included_months` is the search. Any count
works: one month or twelve. `excluded_months` remains as the inverse
spelling, applied afterwards, for when naming what to skip is shorter.

Do not implement either by pinning `earliest_departure`/`latest_departure`.
That silently switches the window from rolling to fixed
(`Preferences.is_rolling`), so the horizon stops moving forward and quietly
goes stale.

Both filter on the *departure* date. A trip leaving in October and
returning in November is an October trip; dropping November must not delete
it.

**A named month the horizon cannot reach searches nothing, and that looks
exactly like a month with no cheap fares.** From August an 8-month horizon
reaches only to April, so asking for June would silently find nothing.
`unreachable_months` detects it, `--status` prints `NOT REACHED`, and the
setup wizard warns at the point of asking.

The sweep order then falls out of `sweep_order` for free - priority months
first, then the rest in date order:

    January -> February -> March -> October -> November -> December
      558        504        558       558        540        558

**3,276 windows, down from 4,014.** With the derived hot share that is a
full exhaustive pass in 4.1 days at 90s, 2.1 days at 45s, 1.4 days at 30s.

`setup_tracker.py` asks for this, so re-running setup no longer silently
resets it to "every month" and brings April and September back.

### Seven defects the first version of this shipped

Audited on request the same day. All seven passed the suite as it stood,
which is the lesson: the tests covered the feature's happy path and none of
the seams around it.

1. **The wide net kept querying excluded months.** It was driven by the raw
   horizon, not by what is searched. Six wasted requests a run is the small
   half - a hint goes on the *front* of the hot list and is Chrome-verified,
   so a fare in an excluded month could have reached the email. `cli.py` had
   no tests at all, which is how it got through; `wide_net_months` is now a
   named function with its own test file.
2. **An excluded month was reported as "outside the horizon".** It is
   reachable and deliberately skipped. The message sent the reader off to
   raise `search_months`, which would change nothing. `unreachable_months`
   now measures against `months_in_horizon`, before any filtering.
3. **Half-month hints fragmented the ledger.** `month_halves` labels probes
   "January 2027 (1st half)", so the ledger grew three rows per month and a
   month could read "no hint yet" beside a half-month row holding a real
   price. `MonthHint.base_month` folds them.
4. **`--status` showed months no longer searched.** September and April sat
   in the ledger with old prices next to hours-old ones. The data is kept -
   a price seen in September is still true - but the display is scoped.
5. **A config that searches nothing spun.** Every named month can be outside
   the horizon (`included_months: [6]` in August), and `sweep_batch` then
   returns immediately. The outer loop has no sleep of its own, because the
   pacing is per-window, so the process rewrote the store at full speed.
   It exits 2 with an explanation now, and an empty batch pauses regardless.
6. **`describe()` lost an article** - "Depart in next 8 months".
7. **`included_label` was dead code**, duplicated inline in the wizard.

## Raising the sweep rate

Agreed with the trip owner 2026-08-23: **raise the rate, but only after one
clean day at 90s.** The table above is the payoff; the risk is that the IP
is still recovering from the 2026-08-23 throttle, and stepping up into an
active throttle is what caused it.

The order is 90s -> 45s -> 30s, one step at a time, checking `--status`
after each. Do not jump straight to 30s, and do not raise it at all while
the health line reports an empty rate above ~20%.

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

**4. Bursts, and a perfectly regular cadence.** Added 2026-08-23, found by
reading `tracker.log` rather than by reasoning. The sweep jitters every wait
and `search.Searcher` jitters too - but `monthly.scan_months`, the wide net,
had no delay and no jitter *at all*. It fired every probe back to back as
fast as Google answered:

    15:45:01 ... 15:45:38   24 requests in 37 seconds, ~1.5s apart

That was the most machine-shaped traffic in the project, on the one path
that runs on every scheduled run, six times a day. It is now paced by
`monthly_scan_delay_seconds` (3.0) with the same jitter as everywhere else.

The same audit found the count misreported: `cli.py` logged the *month*
count as the request count, so "Wide net: 8 request(s)" actually meant 24.
With `monthly_scan_halves` on that understated the daily footprint by 96
requests. `monthly.probe_count` now reports what is really sent.

The lesson generalises: **every new path that reaches Google needs pacing
and jitter, not just the lock.** Taking `gate.google()` only stops two
callers overlapping; it says nothing about how fast one of them goes.

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

1. **Throttling over a full week.** ~~Confirm `throttle.json` settles
   somewhere sensible rather than collapsing to the floor of 8.~~ Answered
   2026-08-23, and the answer is the bad one: **it collapsed to the floor.**
   `budget: 8`, `consecutive_bad: 4`, with per-run empty rates walking
   0.25 -> 0.46 -> 0.61 -> 0.75 across the day.

   At 8 requests a run the grid needs ~300 days for a full pass, so it now
   contributes nothing to coverage. That does not by itself argue for
   removing it - the section above already concluded the grid earns its keep
   only as the fallback that produces an email when the sweep is down, and a
   floored budget still does that. But the floor should be read as the
   adaptive throttle working, not as a setting to raise. Do not raise it
   while the sweep is also running; they share one IP.
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
- [x] `python install_schedule.py`; confirm it fires. Six tasks
      (FlightTracker1-6) are installed and Ready; FlightTracker2 fired at
      09:03 on 2026-08-23 with exit code 0. The others showed result
      `267011` = `SCHED_S_TASK_HAS_NOT_RUN`, which is "not yet", not a
      failure - they were installed after their slot had passed.
- [ ] Confirm a scheduled run's *contents*, now that `tracker.log` exists.
      Until 2026-08-23 `tracker.cli` wrote no log file at all, so a run's
      only trace was its exit code and there was no way to tell a run that
      emailed from one that silently found nothing.
- [ ] Watch a week; check `throttle.json` settled somewhere sensible
- [x] Decide the alert thresholds. Done: `preferences.json` now holds
      `good_price_usd: 1400`, `great_price_usd: 1150`, which matches the
      market (the cheapest fare found anywhere so far is $1,347) and makes
      the highlight mean something. This file previously said $2,600/$2,200
      was still live; that was stale.
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
