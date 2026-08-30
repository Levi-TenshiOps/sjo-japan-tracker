# SJO → Japan flight tracker

Finds cheap round trips from **San José, Costa Rica to Tokyo**, and emails
them to you twice a day.

The catch it exists to solve: **about three quarters of what Google offers
on this route connects through the United States**, which a Costa Rican
passport cannot transit without a consular C-1 visa. Searching by hand is
therefore actively misleading — most of what you see, you cannot book.
This filters all of it out, along with Canada, mainland China and Russia.

**You need:** Python 3.10+, **Google Chrome installed** (the only thing
that sees the cheap fares), and a Gmail **app password** — not your normal
password, which Google blocks.

```bash
pip install -r requirements.txt
python setup_tracker.py        # months, trip lengths, budget
python setup_email.py          # your address and app password -> .env
python install_schedule.py     # installs the twice-daily emails
python sweep_forever.py        # the background search — leave it running
```

`setup_email.py` checks the credentials against the mail server before
saving, and touches nothing else — so you can re-run it to fix a password
without disturbing settings you have tuned.

Nothing personal is stored in a tracked file, so the repository is safe to
publish as-is.

---

## How it works

There is no public Google Flights API, so it reads the page. Two processes
run, and they do different jobs:

**1. The sweep** (`sweep_forever.py`) runs continuously in the background.
It is the one that actually finds things. It walks **every** departure/
return date combination in your search — currently 2,745 of them — one at
a time, forever, and records the cheapest bookable fare for each. A full
pass takes about a day, then it starts again.

**2. The scheduled runs** (`tracker/cli.py`) fire six times a day, take
what the sweep has found, re-check the most promising fares so the prices
are current, and send you at most two emails.

Between them, four things ask Google questions:

| | What it does | Cost |
|---|---|---|
| **The sweep** | walks every date combination, one at a time, forever | ~4,500 requests/day |
| **The wide net** | asks Google "cheapest fare in February?" — no dates given | ~100/day |
| **Chrome check** | re-prices the best candidates so your email is current | ~66/day |
| **The HTTP grid** | a fast, shallow scan; the fallback if the sweep stops | ~48/day |

The wide net **finds** fares fastest — it named the first record. The sweep
finds them **thoroughly**, because it is the only thing that will ever look
at a date Google never suggests.

### Why a real browser

**The plain HTTP scrape systematically misses the cheap fares.** This is
the single most important fact about the design. The Lufthansa-group
routings through Zurich — where every fare under $1,600 on this route has
been found — simply do not appear in the server-rendered HTML, at any stop
limit, in any currency. On one window the HTTP scrape reported $1,635 where
the real answer was $1,347.

So the sweep drives **headless Chrome** (`--headless --dump-dom`) for every
window. It costs ~14 seconds a launch against ~3 for HTTP, which is why the
whole design is built around spending that budget carefully. The HTTP grid
is kept only because it is the one thing that can still produce an email if
the sweep stops, and its prices are labelled as an upper bound, never
quoted as "the cheapest".

### What happens to each result

Every option Google returns goes through the same three steps:

1. **Parsed** out of the page — price, airlines, total duration, and the
   airports it connects through.
2. **Visa-checked**, leg by leg. Google's own "connecting airports" filter
   is only a hint, so every routing is re-checked here against
   `tracker/airports.py`. An airport nobody has researched is **refused**,
   not allowed, and a routing that cannot be read at all is dropped rather
   than assumed safe.
3. **Recorded** — the cheapest surviving fare per window goes to
   `discoveries.json`, and every accepted fare is appended to
   `sweep_history.csv`, which is never rewritten.

Typically 9 of 13 options on a window are thrown out at step 2.

## Your two emails

One in the morning, one in the evening. **The evening one is held back
until 20:00** so it carries the day's *cheapest* fare rather than whatever
happened to be found last.

```
06:00  $1,370  → email 1
11:00  $1,352  → held
16:00  $1,180  → better, but still held in case something beats it
21:00  $1,050  → email 2, and it is the day's best
```

Both arrive every day whatever the market did, so **a silent inbox means
something is broken, not that nothing was found** — and a watchdog emails
you if the emails themselves stop. A standout fare (below
`great_price_usd`) breaks the hold and sends immediately.

Set `daily_digest: false` in `config.yaml` if you would rather only hear
from it when a fare beats your budget.

## Sharing with friends

```bash
python -m tracker.cli --share-with "ana@example.com, luis@example.com"
python -m tracker.cli --share-with list     # who is on the list
python -m tracker.cli --share-with ""       # remove everyone
```

They get **the fare emails only** — the two a day, and any `--email-now`
report. They never receive the alarms: a throttle, a stopped sweep, or
unreadable results are operational messages meant for whoever runs this,
and a friend receiving one would just be alarmed by something they cannot
act on.

That separation is structural, not a rule someone has to remember —
`alarm.send` has no way to pass extra recipients at all.

Everyone is **blind copied**, so no friend sees another's address, and the
list lives in `preferences.json`, which is gitignored: other people's
addresses never enter the repository.

## What the email shows

The **20 cheapest options, cheapest first**, with price, duration,
airlines, stops, the hub it routes through, and a link that opens that
exact search. Browser-verified fares lead; rows the background sweep found
carry a "checked 4 hr ago" label so you can tell a live price from an
earlier one.

Your priority months are guaranteed a share of the 20 slots, so a cheap
month elsewhere cannot crowd them out — but the list is always sorted by
price, and the single cheapest fare found always appears.

A cheap/typical/expensive bar shows where the fare sits, with real numbers
at both ends:

```
cheap              typical            expensive
$1,347 – $2,213   $2,213 – $3,202   $3,202 – $13,127
```

## What it searches

You pick **months**, not just a horizon:

```
Months        January, February, March, October, November, December
Priority      January, February, March
Trip lengths  21–38 nights
Lead time     ignore departures within 21 days
```

**The whole trip has to fall inside those months, not just its first day.**
A 31 March departure comes home in late April, so it is not searched at
all. That leaves **2,745 date combinations** — 161 departure days × up to
18 trip lengths.

Coverage is tiered, so the dates most likely to be cheap are checked far
more often than the rest:

| Tier | Share of searches | Revisited |
|---|---|---|
| already known to be cheap | 8% | every ~10 hours |
| plausible dates (by weekday pattern) | 25% | ~1.3 days |
| everything else, in rotation | 67% | ~0.9 days |

At the current rate every window is re-priced inside a day, so **a price
drop lasting a day or more is caught essentially always, anywhere in the
calendar**. The tiering matters less than it used to — it was designed
when a full pass took six days — but it stays, because it is what keeps
the cheapest known fare current between passes and it is what would carry
the search if the rate ever had to come down again.

The tiers are derived from your own data, never hardcoded, so if an
airline changes its schedule the tracker follows within a pass. Check the
current figures any time with `python sweep_forever.py --coverage`.

### Finishing some months first

```bash
python sweep_forever.py --focus 1,2,3   # January, then February, then March
python sweep_forever.py --focus none    # back to normal
```

A focus **redirects** effort and never asks for more of it — the request
rate is untouched.

By default a focus only **backfills**: it prices windows that have no
trustworthy answer yet, and stops as soon as every one of them has one.
That is what you want after a throttle. It is *not* what you want on a
sale day, when every window already has an answer and the answers are the
problem — half of them can be a day old.

`--focus-max-age` makes a stale answer count as unanswered again:

```bash
python sweep_forever.py --focus 1,2,3 --focus-max-age 12
```

## Sale day

The whole sequence, for a day when fares are expected to move — Labor Day,
Black Friday, a flash sale:

```bash
# 1. stop the sweep cleanly (never kill it). This waits until it has
#    really exited, so you can run the next line the moment it returns.
python sweep_forever.py --stop

# 2. re-price every priority-month window once, whatever its age
python sweep_forever.py --focus 1,2,3 --focus-max-age 0 --focus-max-tries 1

# 3. watch it, in another window. No flags needed: it reads what the
#    sweep is doing from the store.
python sweep_forever.py --watch

# 4. when it says the focus is complete, email yourself the results
python -m tracker.cli --email-now
```

**No `--delay` needed** — 5s is the default, so a restart and a reboot
both come back at it. Pass one only to go *slower*; the sweep prints a
warning whenever the rate differs from the previous run.

**Two ways to ask, and they answer different questions.** Simulated over
the real 1,089 January–March windows at the default rate:

| command | finishes in | re-prices |
|---|---|---|
| `--focus 1,2,3 --focus-max-age 12` | **~4 h** | only what is over 12 h old |
| `--focus 1,2,3 --focus-max-age 0 --focus-max-tries 1` | **~7 h** | **all 1,089, once each** |

Use the first when you just want the stale half refreshed. Use the second
on a real sale day: a fare checked an hour ago still holds a *pre-sale*
price, and an age cut-off skips exactly those windows.

`--focus-max-tries 1` is what makes the second one stop. Without it the
focus keeps working — windows go stale again while it is still running —
so it finds more to do until every window has had three goes.

Either way the focus prices everything once before re-pricing anything, so
coverage comes first and you can email yourself part-way through.

### How long it takes

**About 7 hours** to re-price all 1,089 priority-month windows at the
default 5s. Where that goes:

| `--delay` | cycle | launches/h | to the focus | all 1,089 |
|---|---|---|---|---|
| 15s | 29s | 124 | 99 | 11.0 h |
| 10s | 24s | 150 | 120 | 9.1 h |
| **5s** | **19s** | **189** | **152** | **7.2 h** |
| 0s | 14s | 257 | 206 | 5.3 h |

**The page itself costs ~14 s**, so the delay is no longer the dominant
term and even `--delay 0` cannot beat 5.3 h. At 5s you are already within
two hours of the floor, and buying those two hours means doubling the
request rate into territory that has thrown this address into a day-long
throttle before.

One launch in five goes to the hot list rather than the focus. Without it
the run would take 5.8 h, but the cheapest fare in your email could be
seven hours stale by the end.

**You do not wait 7 hours to see anything.** Progress in order:

| by | done |
|---|---|
| ~3.7 h | all of January (558 windows) |
| ~6.8 h | January and February (1,034 of 1,089) |
| ~7.2 h | March too — only 55 windows, since a late-March departure returns in April |

So: start it in the morning, `--email-now` at lunchtime for a first look,
and again in the evening for the full picture. If a sale breaks at 09:00
you have all of January re-priced by early afternoon.

If a throttle lands mid-run the tripwire drops you to `--delay 10` and the
run stretches to ~9 h. It will not fail — it will take longer.

`--email-now` reads only what is already on disk — **it makes no requests
to Google**, so it cannot be throttled, never competes with the sweep, and
is safe to run as many times as you like. It also does not touch the
two-a-day budget or the held evening slot, so your normal emails still
arrive. Its subject starts with `[on demand]` so it is never mistaken for
an alert.

When the focus finishes it says so and hands back to the ordinary
rotation by itself — no second restart needed. The cold cursor is frozen
while it runs, not skipped, so October–December resume exactly where they
stopped.

## Commands

Setup is the four lines at the top of this file. Everything else:

### The background search

```bash
python sweep_forever.py                 # start it; leave it running
python sweep_forever.py --stop          # stop cleanly, waits until it has
python sweep_forever.py --watch         # live progress, refreshing
python sweep_forever.py --status        # findings, and a focus ETA if one is running
python sweep_forever.py --coverage      # how often each date is re-checked
python sweep_forever.py --readiness     # is it safe to change the rate?
python sweep_forever.py --once          # a single batch, then exit
```

Useful flags on the sweep:

| Flag | Default | What it does |
|---|---|---|
| `--delay N` | 5 | seconds between launches. Higher is gentler |
| `--batch N` | 10 | windows priced between saves |
| `--focus 1,2,3` | from config | finish these months before the rest |
| `--focus-max-age H` | off | treat an answer older than H hours as stale |
| `--focus-max-tries N` | 3 | how many times one window may be re-priced |
| `--stop-timeout N` | 420 | how long `--stop` waits before giving up |
| `--recheck-unverified` | — | queue every window whose "no fares" was never trustworthy |
| `--log PATH` | sweep.log | where to append the log |

### Emails

```bash
python -m tracker.cli --email-now       # email what has been collected so far
python -m tracker.cli --dry-run         # a full run that sends nothing
python -m tracker.cli --status          # settings and coverage
python -m tracker.cli --save-preview out.html   # write the email to a file
python -m tracker.cli --share-with "ana@x.com, luis@y.com"
python -m tracker.cli --share-with list
python -m tracker.cli --share-with ""   # remove everyone
```

### Tests

```bash
python -m pytest tests/ -q              # 1,611 tests, entirely offline
```

### Safe to run any time

`--watch`, `--status`, `--coverage`, `--readiness` and `--email-now` **only
read files**. None of them touches Google, so they cannot be throttled and
will not compete with the sweep. Run them as often as you like.

**Always stop the sweep with `--stop`, never Ctrl-C or a kill.** A hard
kill can leave half a line in the CSV, and one such line once crashed every
scheduled run for four hours before anyone noticed.

## What this route actually costs

From 47,000+ browser-verified, visa-free observations:

| | |
|---|---|
| Cheapest ever found | **$1,335** — Edelweiss/SWISS via Zurich, 46 h |
| Median | **$2,541** |
| At or under $1,400 | 1.1% of everything seen |

**Every fare at or under $1,600 has been Lufthansa Group** — Edelweiss,
SWISS or Lufthansa, through Zurich, Frankfurt or Munich.

So a $1,400 alert threshold catches roughly the top 1%. Do not lower it
towards $1,200 believing the route averages ~$1,350 — **that figure is the
floor, not the average.**

## Visa rules

`tracker/airports.py` is the safety-critical file: 42 usable hubs, 174
banned airports. An airport nobody has researched is **refused**, not
allowed — a hand-kept deny list can never be complete, and silence used to
read as approval.

- **Banned** — US, Canada, mainland China, Russia. Even a 60-minute airside
  connection needs a consular appointment.
- **Free** — Mexico, all Schengen, Türkiye, Qatar, UAE, Panama, Colombia,
  Brazil, Peru, Chile, Singapore, Hong Kong, Taiwan, Malaysia, Thailand,
  and Japan itself.
- **Light** — UK (ETA, ~£16) and South Korea (K-ETA, ~$10). Set
  `hub_tier: FREE` to exclude these too.

## Where to run it

**A computer at home, not the cloud.** The single biggest factor in getting
blocked is whether the traffic comes from a residential or a datacenter IP,
and Google flags cloud ranges on reputation no matter how politely you
pace. A Raspberry Pi Zero costs about 28 cents of electricity a month and
can be forgotten about.

What actually gets you blocked, in order:

1. **A fresh browser profile every launch** — without a persistent one, a
   2,700-window sweep looks like 2,700 brand-new browsers from one address.
2. **Two processes searching at once** — measured, this took the success
   rate from 87% to 24% in minutes. Every path now takes a lock.
3. **Rate**, which matters, but less than the two above.
4. **Bursts and a perfectly regular rhythm** — every wait is jittered.

**Never diagnose a block by making more requests.** That turned a short
throttle into an hour-long one. When it looks blocked, stop and wait.

It also protects itself, so neither of those is left to you:

- **If Google starts refusing, the sweep slows down on its own** — one
  rung per incident, 5s → 10 → 15 → 25 → 40 → 60 → 90, and it never speeds
  back up by itself.
- **A restart shortly after trouble does not come back fast.** The
  slow-down above lives in the running process, so a machine that
  throttles at 03:00 and reboots at 06:00 would otherwise return at full
  speed into an address still refusing — which is exactly how the one
  serious block here happened. A fresh start within 12 hours of a throttle
  begins at 40s and says why. Typing `--delay` yourself always wins; only
  the default is second-guessed.

## When something breaks

**Five failures email you:**

| What happened | How you find out |
|---|---|
| Google throttles the sweep | email, and it backs off on its own |
| Google goes dark on a scheduled run | email, when *both* search methods return nothing |
| The sweep stops | email from the next scheduled run, 3–6 h |
| Results become unreadable | email — Google's markup has changed |
| Your emails stop arriving | email from the sweep, after 16 h of silence |

The two halves watch each other, because neither can announce its own
death: the sweep reports the scheduled runs falling silent, and the
scheduled runs report the sweep dying and the parser going blind.

Alerts are sent once, not once per run, and clear themselves when the
condition does.

**And two that it will not email you about**, stated plainly because
knowing the edge of the net matters more than the net:

- **Email delivery itself failing.** You cannot be emailed that email is
  broken. A run that fails to send exits non-zero and retries next time;
  if you want a second channel, set `ntfy_topic` in `config.yaml` for
  phone push.
- **A single scheduled run crashing.** It logs a full traceback to
  `tracker.log` and exits non-zero. Only if *every* run stops sending does
  the 16-hour silence watchdog fire — so a one-off crash is visible in the
  log, not in your inbox.

## What it collects, and where it goes

Everything is a plain file in the project folder. Nothing leaves your
machine except the emails.

### The two that hold the fares

**`sweep_history.csv`** — the important one. Every visa-free fare ever
seen, appended and never rewritten. One row per option, so a window that
returned three bookable fares writes three rows:

```
checked_at_utc, origin, destination, depart_date, return_date,
price_usd, duration_min, stops, hubs, airlines, band, band_source, deep_link
```

Currently ~46,000 rows, ~4.9 MB, growing by roughly 10,000 rows a day.
This is where every claim in this file comes from — the median fare, the
hub comparisons, the weekday analysis. Deleting it loses the history but
breaks nothing.

**`price_history.csv`** — the same format, written by the six scheduled
runs rather than the sweep. Much smaller (~2,800 rows).

### The one that holds "what can I book right now"

**`discoveries.json`** — the sweep's working state, rewritten after every
window:

- the **cheapest current fare per window** (the best 400 are kept; the rest
  live on in the CSV)
- the **cursor** — how far through the pass it is, so a restart resumes
  rather than starting over
- the **check ledger** — for every window: when it was last looked at, in
  how many seconds, whether the page was blank, and whether the connection
  was trustworthy at the time. This is what makes "no fares on this date"
  distinguishable from "Google refused to answer".
- health samples, the re-check queue, and the focus/rate settings

It holds the **latest** price, not the best ever seen — the question it
answers is "what can be booked on this date now".

### The small ones

| File | Contains |
|---|---|
| `config.yaml` | search settings — the only one committed to git |
| `preferences.json` | your email, months, budget, friends' addresses |
| `.env` | SMTP credentials |
| `state.json` | which of today's two emails have been sent |
| `throttle.json` | the HTTP grid's request budget, alarm flags |
| `month_hints.json` | the wide net's cheapest-month answers |
| `rotation.json` | the scheduled runs' position in their rotation |
| `google.lock`, `sweep.pid`, `sweep.stop` | coordination between processes |
| `sweep.log`, `tracker.log` | what each process did |

**`preferences.json` and `.env` are gitignored**, so the repository stays
publishable — no addresses, no credentials, no personal data in anything
tracked.

## Honest caveats

- **Unofficial endpoint.** There is no public Google Flights API; this
  reads the page. Two different things can go wrong, and only one of them
  is Google blocking you. A block is loud and temporary. A **markup
  change** is silent and permanent — the pages still arrive, some rows
  become unreadable, and those fares simply stop existing as far as the
  tracker is concerned, which looks exactly like a quiet market. Both now
  email you (see below).
- **An empty answer proves nothing.** It means this method saw nothing, not
  that no fare exists. A limit was once inferred from silence and it
  silently excluded the exact flight being tracked.
- **A tracked price is not a held quote.** Treat an alert as "go look now".
- **Osaka is filtered, not absent.** Google does return Osaka itineraries;
  every one transits the US or Canada, so none are bookable on this
  passport.
- **Airside transit rules are airline- and airport-dependent.** The deny
  list reflects the general rule for a Costa Rican passport as of August
  2026. Confirm with the airline before you pay.
- **Scraping is against Google's Terms of Service.** This is a personal,
  low-volume tool checking your own travel plans. Keep it that way. If you
  run it, you are the one making those requests.

## License

MIT — see [LICENSE](LICENSE). Use it, change it, share it; no warranty.

The visa rules in `tracker/airports.py` reflect the general position for a
**Costa Rican** passport as of August 2026. If yours is different, that
file is the first thing to change — and confirm with the airline before
you pay either way.
