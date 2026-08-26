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

## How it searches

Three things look at Google, doing different jobs:

| | What it does | Cost |
|---|---|---|
| **The sweep** | walks **every** date combination, one at a time, forever | ~900 requests/day |
| **The wide net** | asks Google "cheapest fare in February?" | ~100/day |
| **Chrome check** | re-prices the best candidates so your email is current | ~60/day |
| **The HTTP grid** | a fast, shallow scan; a fallback if the sweep stops | ~48/day |

The wide net **finds** fares fastest — it named the $1,347 record. The
sweep finds them **thoroughly**, because it is the only thing that will
ever look at a date Google never suggests.

**Only the browser sees the cheap fares.** The plain HTTP scrape cannot see
the European routings where the sub-$1,400 fares live — on the same window
it reported $1,635 where the real answer was $1,347. So its prices are
never quoted as "the cheapest".

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

| Tier | Revisited |
|---|---|
| already known to be cheap | every few hours |
| plausible dates (by weekday pattern) | ~2 days |
| everything else, in rotation | ~8 days |

That last number is the honest limit. Walking all 2,745 windows back to
back would take about 3 days, but only around 40% of searches go to the
cold rotation — the rest keep the good candidates fresh, which is the
better trade when a fare can move overnight.

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
python sweep_forever.py --focus 1,2,3 --focus-max-age 6
```

## Sale day

The whole sequence, for a day when fares are expected to move — Labor Day,
Black Friday, a flash sale:

```bash
# 1. stop the sweep cleanly (never kill it)
python sweep_forever.py --stop

# 2. re-price the priority months, anything answered over 6 h ago.
#    --delay MUST be repeated: a restart drops to the default rate.
python sweep_forever.py --focus 1,2,3 --focus-max-age 6 --delay 5

# 3. watch it, in another window
python sweep_forever.py --watch

# 4. when it says the focus is complete, email yourself the results
python -m tracker.cli --email-now
```

**Repeat `--delay`.** A restart uses the default, not the rate you were
running at, and that is the difference between ~5 hours and ~16. The sweep
prints a warning when the two differ, but the flag is the fix. The rate is
deliberately not remembered: the boot launcher passes no `--delay` so a
reboot always comes back at the safe default.

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

```bash
python sweep_forever.py --watch      # live progress, leave it open
python sweep_forever.py --status     # what has it found?
python sweep_forever.py --stop       # stop it cleanly (never kill it)
python -m tracker.cli --status       # settings and coverage
python -m tracker.cli --dry-run      # test, send nothing
python -m tracker.cli --email-now    # email what has been collected so far
python -m pytest tests/ -q           # 1,543 tests, offline
```

`--watch`, `--status`, `--coverage` and `--readiness` only read files. They
are safe to run beside the sweep and none of them touches Google.

**Always stop the sweep with `--stop`.** A hard kill can leave half a line
in the CSV, and one such line once crashed every scheduled run for four
hours.

## What this route actually costs

From 3,700+ browser-verified, visa-free observations:

| | |
|---|---|
| Cheapest ever found | **$1,347** — Edelweiss/SWISS via Zurich, 46 h |
| Median | **$2,478** |
| At or under $1,400 | 3% of everything seen |

**Every fare at or under $1,600 has been Lufthansa Group** — Edelweiss,
SWISS or Lufthansa, through Zurich, Frankfurt or Munich.

So a $1,400 alert threshold catches roughly the top 3%. Do not lower it
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

## Files

| File | Committed | Contains |
|---|---|---|
| `config.yaml` | yes | search settings, nothing personal |
| `preferences.json` | **no** | your email, months, budget |
| `.env` | **no** | SMTP credentials |
| `sweep_history.csv` | optional | the sweep's price log — the big one |
| `price_history.csv` | optional | the scheduled runs' price log |
| `discoveries.json` | no | the sweep's cursor and findings |
| `state.json`, `throttle.json` | no | runtime state |
| `sweep.log`, `tracker.log` | no | what each process did |

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
  low-volume tool checking your own travel plans. Keep it that way.
