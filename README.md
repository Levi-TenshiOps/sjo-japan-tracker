# SJO → Japan flight tracker

A standing search for cheap round trips from **San José, Costa Rica to
Tokyo**. It prices every date combination worth considering, around the
clock, and emails you the cheapest bookable fare twice a day.

## Why not Google Flights

Google Flights prices a trip you have already chosen. This finds the trip.

| | Google Flights | This |
|---|---|---|
| **Dates** | one pair at a time | all **2,745** combinations, 21–38 nights, re-priced daily |
| **Visa** | no such filter | US, Canada, China and Russia routings removed, leg by leg |
| **Cheap fares** | hidden without JavaScript | a real browser: found **$1,347** where a plain scrape said $1,635 |
| **Is $1,500 good?** | "low" — for the dates you happened to search | only **2%** of 49,000 fares seen here have been cheaper |
| **Updates** | you check | two emails a day |

The visa row is the one that hurts. **Three quarters of what Google shows
for this route connects through the United States**, which a Costa Rican
passport cannot transit without a consular visa — even for a 60-minute
airside connection. So most of what you see, you cannot book, and you find
out at the gate.

## What this is, and what it is not

I built this quickly, with AI assistance, to solve one problem: find me a
cheap flight to Japan I can actually board.

It is **not** a learning exercise, a portfolio piece, or a demonstration of
anything. It's a personal tool that happens to be public, in case it helps
someone in the same position.

**I have not read the Python line by line.** I insisted on the part I can
judge: that it's tested hard, that the tests found real bugs rather than
confirming what was already believed, and that it works. It does — a week
of continuous running, ~50,000 real fares priced, two emails a day with
prices I can go and book.

Judge it by whether it works, because that's the standard it was held to.
Before running it, read the **caveats** at the bottom — especially that the
visa rules are specific to a Costa Rican passport, and that scraping Google
is against their terms of service.

## Setup

You need Python 3.10+, **Google Chrome** (the only thing that sees the
cheap fares), and a Gmail **app password** (not your normal password).

```bash
pip install -r requirements.txt
python setup_tracker.py     # months, trip lengths, budget
python setup_email.py       # your address and app password -> .env
python install_schedule.py  # the twice-daily emails
python sweep_forever.py     # the background search — leave it running
```

Nothing personal is stored in a tracked file, so the repo is safe to
publish as-is.

## How it works

There's no public Google Flights API, so it reads the page. Two processes:

**The sweep** (`sweep_forever.py`) runs continuously and does the finding.
It walks all 2,745 departure/return combinations one at a time, records the
cheapest bookable fare for each, and starts again — a full pass takes about
a day.

**The scheduled runs** (`tracker/cli.py`) fire six times a day, re-check
the most promising fares so prices are current, and send at most two
emails.

Four things ask Google questions:

| | What it does | Cost |
|---|---|---|
| The sweep | every date combination, forever | ~4,500 requests/day |
| The wide net | "cheapest fare in February?" — no dates given | ~100/day |
| Chrome check | re-prices the best candidates for your email | ~66/day |
| The HTTP grid | shallow scan; the fallback if the sweep stops | ~48/day |

**Why a real browser:** the Lufthansa-group routings through Zurich — where
every fare under $1,600 has been found — don't appear in the HTML Google
serves without JavaScript. A Chrome launch costs ~14s against ~3s for HTTP,
which is why the whole design spends that budget carefully. The HTTP grid
survives only because it can still produce an email if the sweep dies; its
prices are labelled an upper bound.

**Every result** is parsed, then visa-checked leg by leg against
`tracker/airports.py` (an unresearched airport is *refused*, not allowed),
then recorded. Typically 9 of 13 options on a window are thrown out.

## What it collects

Plain files in the project folder. Nothing leaves your machine but the
emails.

**`sweep_history.csv`** — every visa-free fare ever seen, appended and
never rewritten. ~46,000 rows, growing ~10,000 a day. Every number in this
file comes from it. Safe to delete; you lose the history, nothing else.

**`discoveries.json`** — the sweep's working state, rewritten every window:
the cheapest *current* fare per window, the cursor (so a restart resumes),
and a check ledger recording when each window was last looked at and
whether the connection was trustworthy at the time. That last part is what
makes "no fares on this date" distinguishable from "Google refused".

**`price_history.csv`** — same format as the first, written by the
scheduled runs. Much smaller.

Also: `config.yaml` (settings, the only one in git), `preferences.json`
(your email, months, budget, friends), `.env` (SMTP), plus small state and
log files. **`preferences.json` and `.env` are gitignored.**

## Your two emails

One in the morning, one in the evening. **The evening one is held until
20:00** so it carries the day's *cheapest* fare, not merely the latest:

```
06:00  $1,370  → email 1
11:00  $1,352  → held
16:00  $1,180  → better, but still held
21:00  $1,050  → email 2, and it's the day's best
```

Both arrive every day whatever the market did, so **a silent inbox means
something is broken** — and a watchdog emails you if the emails stop. Set
`daily_digest: false` to hear only when a fare beats your budget.

Each email shows the **20 cheapest options, cheapest first** — price,
duration, airlines, stops, hub, and a link to that exact search. Priority
months are guaranteed a share of the slots, and the single cheapest fare
always appears. A cheap/typical/expensive bar shows where the price sits.

### Sharing with friends

```bash
python -m tracker.cli --share-with "ana@example.com, luis@example.com"
python -m tracker.cli --share-with list     # who's on the list
python -m tracker.cli --share-with ""       # remove everyone
```

They get **the fare emails only**, blind copied so nobody sees anyone
else's address. They never receive the alarms — a throttle or a stopped
sweep is operational noise they can't act on. That separation is
structural: the alarm code has no way to pass extra recipients.

## What it searches

```
Months        January, February, March, October, November, December
Priority      January, February, March
Trip lengths  21–38 nights
Lead time     ignore departures within 21 days
```

**Priority months** are the ones you actually want to travel in. They are
swept first, kept fresher than the rest, and can be targeted on demand
before a sale day.

**The whole trip must fall inside the searched months**, not just its first
day — a 31 March departure comes home in late April, so it isn't searched.
That leaves **2,745 combinations** (161 departure days × up to 18 lengths).

Coverage is tiered, derived from your own data rather than hardcoded:

| Tier | Share | Revisited |
|---|---|---|
| known to be cheap | 8% | ~10 hours |
| plausible dates (weekday pattern) | 25% | ~1.3 days |
| everything else | 67% | ~0.9 days |

Every window is re-priced inside a day, so **a price drop lasting a day or
more is caught essentially always**. Check current figures with
`sweep_forever.py --coverage`.

## Sale day

For a day when fares are expected to move:

```bash
# 1. stop cleanly — this waits until it has really exited
python sweep_forever.py --stop

# 2. re-price every priority-month window once, whatever its age
python sweep_forever.py --focus 1,2,3 --focus-max-age 0 --focus-max-tries 1

# 3. watch it, in another window
python sweep_forever.py --watch

# 4. when it says complete, email yourself the results
python -m tracker.cli --email-now
```

**~7 hours** for all 1,089 January–March windows. January alone is done in
~3.7 h, February by ~6.8 h. You don't have to wait: `--email-now` reads
what's on disk and makes **no requests**, so run it at lunchtime for a
first look and again at the end.

Two ways to ask, and they differ:

| command | finishes | re-prices |
|---|---|---|
| `--focus-max-age 12` | ~4 h | only what's over 12 h old |
| `--focus-max-age 0 --focus-max-tries 1` | ~7 h | **all 1,089, once each** |

Use the second on a real sale day: a fare checked an hour ago still holds a
*pre-sale* price, and an age cut-off skips exactly those. `--focus-max-tries 1`
is what makes it stop — without it, windows go stale while it's still
running and it keeps finding more to do.

No `--delay` needed; 5s is the default and survives a reboot. If a throttle
lands mid-run the sweep slows itself and the run stretches to ~9 h — it
won't fail, it'll take longer.

## Commands

```bash
# the background search
python sweep_forever.py                 # start it; leave it running
python sweep_forever.py --stop          # stop cleanly, waits until it has
python sweep_forever.py --watch         # live progress
python sweep_forever.py --status        # findings, plus a focus ETA
python sweep_forever.py --coverage      # how often each date is re-checked
python sweep_forever.py --readiness     # safe to change the rate?

# emails
python -m tracker.cli --email-now       # email what's been collected so far
python -m tracker.cli --dry-run         # a full run that sends nothing
python -m tracker.cli --status          # settings and coverage
python -m tracker.cli --share-with list

# tests
python -m pytest tests/ -q              # 1,642 tests, entirely offline
```

Sweep flags: `--delay N` (default 5, higher is gentler), `--batch N`,
`--focus 1,2,3`, `--focus-max-age H`, `--focus-max-tries N`,
`--stop-timeout N`, `--recheck-unverified`, `--log PATH`, `--once`.

`--watch`, `--status`, `--coverage`, `--readiness` and `--email-now` **only
read files** — no requests to Google, safe to run beside the sweep.

**Always stop with `--stop`, never Ctrl-C.** A hard kill can leave half a
line in the CSV; one such line once crashed every scheduled run for four
hours.

## What this route costs

From 47,000+ browser-verified, visa-free observations:

| | |
|---|---|
| Cheapest ever found | **$1,335** — Edelweiss/SWISS via Zurich, 46 h |
| Median | **about $2,500** |
| At or under $1,400 | about 1% of everything seen |

**Every fare at or under $1,600 has been Lufthansa Group** — Edelweiss,
SWISS or Lufthansa via Zurich, Frankfurt or Munich. So a $1,400 threshold
catches roughly the top 1%. Don't lower it to $1,200 thinking the route
averages ~$1,350 — **that's the floor, not the average.**

## Visa rules

`tracker/airports.py` is the safety-critical file: 42 usable hubs, 174
banned airports. An unresearched airport is **refused**, not allowed — a
hand-kept deny list can never be complete, and silence used to read as
approval.

- **Banned** — US, Canada, mainland China, Russia. Even a 60-minute airside
  connection needs a consular appointment.
- **Free** — Mexico, all Schengen, Türkiye, Qatar, UAE, Panama, Colombia,
  Brazil, Peru, Chile, Singapore, Hong Kong, Taiwan, Malaysia, Thailand,
  and Japan itself.
- **Light** — UK (ETA) and South Korea (K-ETA). Set `hub_tier: FREE` to
  exclude these too.

## Where to run it

**A computer at home, not the cloud.** Google flags datacenter IPs on
reputation however politely you pace. A Raspberry Pi Zero costs about 28
cents of electricity a month.

What actually gets you blocked, in order: **a fresh browser profile every
launch** (2,700 windows looks like 2,700 new browsers), **two processes
searching at once** (measured: 87% → 24% success in minutes), then **rate**,
then **bursts and a regular rhythm**. All four are handled — the profile
persists, every path takes a lock, and every wait is jittered.

**Never diagnose a block by making more requests.** That turned a short
throttle into an hour-long one.

It also protects itself: if Google starts refusing it **slows down on its
own** (5s → 10 → 15 → 25 → 40 → 60 → 90) and never speeds back up, and a
restart within 12 hours of a throttle begins at 40s rather than the fast
default.

## When something breaks

**Five failures email you:** the sweep being throttled, Google going dark
on a scheduled run, the sweep stopping (3–6 h), results becoming unreadable
(a markup change), and your emails stopping (16 h of silence). The two
halves watch each other, because neither can announce its own death.

**Two it won't email you about**, stated plainly because knowing the edge
of the net matters:

- **Email delivery itself failing** — you can't be emailed that email is
  broken. A failed send exits non-zero and retries next run. Set
  `ntfy_topic` for a phone-push second channel.
- **A single run crashing** — logged with a traceback to `tracker.log`.
  Only if *every* run stops sending does the 16-hour watchdog fire.

## Caveats

- **Unofficial endpoint.** No public API; this reads the page. A block is
  loud and temporary. A **markup change** is silent and permanent — pages
  still arrive, some rows become unreadable, and those fares stop existing
  as far as the tracker knows. Both now email you.
- **An empty answer proves nothing.** It means this method saw nothing, not
  that no fare exists. A limit once inferred from silence excluded the exact
  flight being tracked.
- **A tracked price is not a held quote.** Treat an alert as "go look now".
- **Osaka is filtered, not absent.** Google does return Osaka itineraries;
  every one transits the US or Canada.
- **Transit rules are airline- and airport-dependent.** The deny list is the
  general rule for a Costa Rican passport as of August 2026. Confirm with
  the airline before you pay.
- **Scraping is against Google's Terms of Service.** This is a personal,
  low-volume tool. If you run it, you're the one making those requests.

## License

MIT — see [LICENSE](LICENSE). Use it, change it, share it; no warranty.

The visa rules are specific to a **Costa Rican** passport. If yours differs,
`tracker/airports.py` is the first thing to change.
