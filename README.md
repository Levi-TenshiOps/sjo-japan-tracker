# SJO → Japan flight tracker

Watches Google Flights for round trips from San José, Costa Rica to Tokyo
(NRT and HND, searched together as the metro code `TYO`), **only via routes
a Costa Rican passport can actually use** — never through the US, Canada,
mainland China or Russia.

Roughly three quarters of everything Google offers on this route connects
through the United States, which needs a consular C-1 transit visa. Filtering
those out is most of what this project does.

You answer a handful of questions once. After that it runs forever and emails
you **exactly twice a day** — a morning digest and an evening one, the second
held back until 20:00 so it carries the day's *cheapest* fare rather than
merely its most recent.

```bash
pip install -r requirements.txt
python setup_tracker.py        # email, months, trip lengths, budget
python install_schedule.py     # installs the recurring job
python sweep_forever.py        # the background sweep (see below)
```

Nothing personal is stored in a tracked file, so you can share or publish
this repository exactly as it is.

---

## How the searching is split

Three things reach Google, and they do different jobs. The split matters
more than the totals.

| | What it does | Cost |
|---|---|---|
| **The sweep** (`sweep_forever.py`) | walks **every** date combination, one at a time, forever | ~900 requests/day |
| **The wide net** (6×/day) | asks Google directly "cheapest fare in February?" | ~102/day |
| **Chrome verification** (6×/day) | re-prices the best candidates so the email is current | ~50-80/day |
| **The HTTP grid** (6×/day) | a fast, shallow scan; kept as a fallback | ~48/day |

The wide net **finds** fares fastest — it named the $1,347 record. The sweep
**finds them thoroughly**, because it is the only thing that will ever look
at a date Google never suggests. Chrome keeps the number in your inbox true.

**Only Chrome sees the cheap fares.** The plain HTTP scrape cannot see the
Lufthansa-group European routings at all — measured on the same window, it
reported $1,635 where the real answer was $1,347. So the grid is never
quoted as "the cheapest"; it is there so an email still goes out if the
sweep is stopped.

### What actually gets you blocked

Learned the hard way, in order of how much it mattered:

1. **A fresh browser profile every launch.** Without a persistent
   `--user-data-dir`, a 2,700-window sweep looks like 2,700 brand-new
   browsers from one address, each running one flight search and never
   returning. No amount of pacing repairs that.
2. **Two processes at once.** Measured: a second process searching alongside
   the sweep took the hit rate from 87% to 24% within minutes. Every path to
   Google now takes a lock (`tracker/gate.py`) — including any diagnostic
   script you write.
3. **Rate**, which matters, but less than the two above.
4. **Bursts and a perfectly regular cadence.** Every wait is jittered.

**Do not diagnose a throttle by making more requests.** That is what turned
a short throttle into an hour-long one. When it looks blocked, stop and wait.

The scheduled runs also measure their own empty-result rate and move the
grid's budget themselves:

| Empty results | Interpretation | Action |
|---|---|---|
| under 20% | healthy | budget +2 (max 40) |
| 20–50% | noisy | hold |
| over 50% | throttled | budget × 0.6 (min 8) |

Check any time with `python -m tracker.cli --status`.

### On the API alternative

The advice you were given suggests Amadeus, Kiwi or Skyscanner with a free
tier. That is out of date: **Amadeus shut its Self-Service portal down on
17 July 2026** and keys stopped working that day. What remains is Amadeus
Enterprise, gated behind accreditation. Skyscanner's API is partner-only.
There is no free, official flight-search API worth building a personal
tracker on right now, which is why this scrapes.

## Where to run it

The single most important factor is whether the traffic looks like a person
at home or a server in a datacenter. Google maintains good datacenter IP
blocklists, and cloud ranges get flagged on reputation regardless of how
politely you pace requests.

| Option | $/month | IP type | Verdict |
|---|---|---|---|
| **Your computer + `install_schedule.py`** | **0.00** | Residential | **Works** |
| **Raspberry Pi Zero 2 W at home (~3 W)** | **0.28** | Residential | **Best** |
| Old phone or mini PC at home (~8 W) | 0.75 | Residential | Works |
| AWS Lambda + EventBridge | 0.00 | Datacenter | Likely blocked |
| Oracle Cloud always-free ARM VM | 0.00 | Datacenter | Likely blocked |
| AWS t4g.nano EC2 | 3.07 | Datacenter | Likely blocked |
| GitHub Actions | 0.00 | Datacenter | Likely blocked |
| Any cloud + residential proxy | ~10.00 | Residential | Works, overkill |

So: **do not use AWS for this.** It is not a budget problem — Lambda would
be free — it is that a datacenter IP is the thing most likely to get you
blocked. Your always-on computer is not a compromise here, it is the better
engineering choice. Keeping a laptop awake costs a few cents of electricity
a month; a Pi Zero costs about 28 cents and can be forgotten entirely.

`install_schedule.py` picks the right mechanism automatically:

- **macOS** — `launchd`, which unlike cron survives sleep and fires a
  missed run on wake.
- **Linux** — a systemd user timer with `Persistent=true`, which catches up
  after downtime. Falls back to cron if systemd is absent.
- **Windows** — prints the `Register-ScheduledTask` commands to paste, plus
  a launcher for the Startup folder so the background sweep survives a
  reboot. That launcher deliberately spells out **no rate at all**: the code
  default is the safe number, and a rate written into a file that runs
  unattended at every boot outlives every later fix to the default. One did,
  and re-armed a 14,000-request-a-day setting at every restart.

```bash
python install_schedule.py                 # 6 runs/day (default)
python install_schedule.py --runs 4        # fewer
python install_schedule.py --hours 7,13,19 # explicit
python install_schedule.py --dry-run       # show, install nothing
```

Missing a run is harmless. Rotation state persists, so the next run picks up
exactly where the last one stopped.

## The two-email budget

Two a day, hard cap, and the second is **reserved for the cheapest fare of
the day** rather than handed to whatever happened to be first.

Without that reservation you get this failure, which is easy to hit:

```
06:00  $1,370  -> email 1
16:00  $1,180  -> email 2
21:00  $1,050  -> nothing left. The best fare of the day never reaches you.
```

So the second slot is held until 20:00, and the day ends on its best number:

```
06:00  $1,370  -> email 1
11:00  $1,352  -> the slot is HELD
16:00  $1,180  -> better, but still HELD in case something beats it
21:00  $1,050  -> email 2, and it is the day's cheapest
```

**Both emails arrive every day, whatever the market did.** `daily_digest` is
on by default, so the price threshold no longer decides *whether* you hear
from it — only what gets highlighted. A quiet inbox means something is
broken, not that nothing was found, and there is a watchdog that emails you
if the emails themselves stop.

| Situation | Result |
|---|---|
| First run of the day | **email 1**, whatever the price |
| A standout fare (below `great_price_usd`) | **email 2**, immediately |
| Anything before 20:00 | held, not sent |
| The run at or after 20:00 | **email 2**, carrying the day's cheapest |
| Anything after two emails | never |

Set `daily_digest: false` in `config.yaml` for the older behaviour, where an
email only arrives when a fare clears your threshold. Tune the rest with
`last_call_hour`, `min_drop_usd` and `reserve_last_slot`.

## What the email contains

**The 20 cheapest options, ranked cheapest first**, drawn from any of the
searched months. Each row shows the exact USD price, total door-to-door
duration, airlines, stops, the hub it routes through, and a **View** link
that opens exactly that search on Google Flights.

The browser-verified fares lead, in their own block. Rows the background
sweep found carry a "checked 4 hr ago" label, because they are real prices
from earlier in the day rather than from this minute — the email used to
mix the two with no way to tell them apart.

Rows above your budget are still listed and marked *over budget*, so you
always see where the market actually sits rather than a single lonely row.

Each email also carries the cheap / typical / expensive verdict on the same
coloured bar Google uses.

### Priority months are guaranteed a share

Picking the 20 cheapest and stopping has a failure mode: if June happens to
be cheap this week, all 20 slots fill with June and the months you actually
care about are invisible even though a good January fare was found.

So selection runs in two passes. The cheapest priority-month options claim
half the slots first; the rest go to the cheapest of everything left,
priority or not. The list is then sorted by price, so **what you read is
still strictly cheapest-first** — the quota shapes *which* options make the
list, never their order.

It only binds when it has to. If the 20 cheapest already contain 12 from
your priority months, you get exactly the 20 cheapest and nothing changes.
And a genuine bargain outside your months is never excluded: the single
cheapest fare found always appears.

| Situation | Result |
|---|---|
| 30 cheap June fares, 30 pricey January ones | 10 January guaranteed, 20 rows |
| Cheapest 20 already mostly January | exactly the cheapest 20, untouched |
| Only 3 January fares exist | all 3 shown, 17 slots released to others |
| A $900 September bargain | always included, ranked first |

Set `priority_share` to `0` to switch the quota off, or `1.0` to fill with
priority months first.

## The search window

Setup asks which **months** to search, not just how far ahead to look. That
distinction was a real bug once: an 8-month rolling horizon quietly included
April and September because they happened to be the ends of it, and nobody
had chosen either.

```
Months to search      : January, February, March, October, November, December
Horizon               : 8 months ahead (how far to look for those months)
Ignore departures within: 21 days
Priority months       : January, February, March
Trip lengths          : 21-38 nights
```

**The whole trip has to fall inside those months, not just its first day.**
A 31 March departure comes home in late April or early May — months that
were deliberately excluded — so those windows are not searched at all. The
effect is a taper rather than a cliff: 1 March keeps 10 trip lengths, 10
March keeps one, 11 March keeps none.

That leaves **2,745 date combinations**: 161 departure days × up to 18 trip
lengths, minus 153 that would end outside the searched months.

Coverage is tiered, because most windows can never hold a bargain:

- **Hot** — windows already known to be cheap, re-priced every few hours.
- **Warm** — dates whose weekday pair has actually produced a cheap fare.
  Derived from the data, never hardcoded, so if the airline moves its
  schedule the tracker follows within a pass.
- **Re-check** — windows that came back empty while the connection was in
  doubt. An empty answer is only believed once something nearby has
  succeeded.
- **Cold** — everything, walked in order, forever. This is what stops the
  other three becoming self-fulfilling.

A full cold pass takes about 3.5 days at the default 90-second pacing.

### Focusing on the months that matter

To finish some months before the rest — before a sale day, say:

```bash
python sweep_forever.py --focus 1,2,3     # January, then February, then March
python sweep_forever.py --focus none      # back to the normal rotation
```

or set `sweep_focus_months: [1, 2, 3]` in `config.yaml` to make it the
default. A focus **redirects** effort and never asks for more of it — the
request rate is untouched, which is the only kind of "go faster" that is
safe here. The cold rotation freezes while it runs and resumes exactly where
it stopped.

## Visa rules encoded

`tracker/airports.py` is the safety-critical file. 26 usable hubs, 124
banned airports.

**Banned** — a consular appointment is required, so even a 60-minute
airside connection is unusable: United States (C-1 transit visa), Canada
(transit visa), mainland China (Costa Rica is not on the visa-free transit
list), Russia (excluded by policy).

**Free** — nothing needed: Mexico (180 days), all Schengen (90/180),
Türkiye, Qatar, UAE, Panama, Colombia, Brazil, Peru, Chile, Singapore, Hong
Kong, Taiwan, Malaysia, Thailand, and Japan itself.

**Light** — a short online form, which you said is fine: United Kingdom
(ETA, ~£16, usually minutes) and South Korea (K-ETA, ~$10). Set
`hub_tier: FREE` to exclude these.

Every long-haul carrier that actually serves SJO is covered:

| Carrier | Hub | Status |
|---|---|---|
| Aeroméxico | MEX | covered |
| Iberia | MAD | covered |
| Edelweiss / SWISS | ZRH | covered |
| KLM | AMS | covered |
| Air France | CDG | covered |
| Condor / Lufthansa | FRA | covered |
| British Airways | LHR | covered (ETA) |
| Turkish | IST | covered |
| Copa | PTY | covered |
| Avianca | BOG | covered |
| Emirates, Qatar | GRU | covered |
| United, American, Delta, JetBlue | IAH, MIA, ATL, FLL | correctly blocked |
| Air Canada | YYZ | correctly blocked |

Google's `connecting_airports` parameter is only a *hint*, so the ban is
re-checked leg by leg on every returned itinerary. A banned routing cannot
reach your inbox even if it is the cheapest fare found.

> ETIAS has slipped to 2027 with no confirmed date. When it lands it is a
> €20, ~10-minute online form and Schengen hubs stay usable. The EES
> rollout has made short Schengen layovers riskier — verify before booking.

## Price classification

The email carries a cheap / typical / expensive bar. Two sources, best
first:

1. **Your own history**, once there are 25+ observations across 5+ separate
   days. Below p20 is cheap, above p80 is expensive.
2. **Seed bands** until then.

**Google's own price insight is no longer used**, and that removal matters.
It describes every routing Google sells — including the US and Canada
transits this passport cannot use — so it measured a population the reader
is not allowed to book from. Against 1,249 visa-free observations its
"cheap" band called **zero** of them cheap: the green zone sat below the
cheapest fare that has ever existed on this route. It is still logged for
comparison, and nothing else.

The rule worth keeping: **a band the data cannot reach is a broken gauge.**

Each zone shows two real numbers, closed with fares actually observed rather
than trailing off into an open interval:

```
cheap              typical            expensive
$1,347 – $2,213   $2,213 – $3,202   $3,202 – $13,127
```

### What this route actually costs

Measured across 2,800+ browser-verified, visa-free observations:

| | |
|---|---|
| Cheapest ever found | **$1,347** (Edelweiss/SWISS via Zurich, 46 h 20 m) |
| p10 | $1,634 |
| **Median** | **$2,464** |
| Dearest seen | $13,127 |
| At or under $1,400 | 3.2% of observations |
| At or under $1,150 | **0%** |

So a $1,400 alert threshold catches roughly the top 3% — which is the point
of it. **Do not lower it towards $1,200 on the belief that the route
averages ~$1,350: that figure is the *floor*, not the average**, and a
$1,200 threshold would never have fired once.

**Every fare at or under $1,600 has been Lufthansa Group** — 32 Edelweiss +
SWISS, 4 Lufthansa + ANA, 3 Lufthansa. Nothing else, in 1,617 observations.

## Commands

**The scheduled run** — searches, then decides whether to email:

```bash
python -m tracker.cli --status                        # settings and coverage
python -m tracker.cli --dry-run --save-preview e.html # test, send nothing
python -m tracker.cli -v                              # one real run
python -m tracker.cli --budget 12                     # smaller run
```

**The background sweep** — the thing that actually walks every date:

```bash
python sweep_forever.py                 # run until stopped
python sweep_forever.py --watch         # live progress, leave it open
python sweep_forever.py --status        # what has it found so far?
python sweep_forever.py --coverage      # how often is each tier revisited?
python sweep_forever.py --readiness     # safe to raise the rate yet?
python sweep_forever.py --focus 1,2,3   # finish these months first
python sweep_forever.py --stop          # ask it to finish and exit cleanly
```

`--watch`, `--status`, `--coverage` and `--readiness` only read. They are
safe to run beside the sweep and none of them touches Google.

**Always stop it with `--stop`, never a hard kill.** A killed sweep can
leave a half-written line in the CSV, and one such line once crashed every
scheduled run for four hours.

```bash
python -m pytest tests/ -q                            # 1,373 tests, offline
```

## Files

| File | Committed | Contains |
|---|---|---|
| `config.yaml` | yes | search parameters, nothing personal |
| `preferences.json` | **no** | your email, dates, trip lengths, budget |
| `.env` | **no** | SMTP credentials |
| `price_history.csv` | optional | the scheduled runs' price log |
| `sweep_history.csv` | optional | the sweep's price log — the large one |
| `discoveries.json` | no | the sweep's cursor, findings and check ledger |
| `month_hints.json` | no | the wide net's cheapest-per-month ledger |
| `state.json`, `throttle.json`, `rotation.json` | no | runtime state |
| `sweep.log`, `tracker.log` | no | what each process did |

## Putting it in a private repo

```bash
cd sjo-japan-tracker
git init && git add . && git commit -m "Initial commit"

# GitHub CLI
gh repo create Levi-TenshiOps/sjo-japan-tracker --private --source=. --push

# or manually, after creating the empty private repo on github.com
git remote add origin git@github.com:Levi-TenshiOps/sjo-japan-tracker.git
git branch -M main && git push -u origin main
```

Before pushing, confirm nothing personal is staged:

```bash
git status --porcelain          # preferences.json and .env must NOT appear
git ls-files | grep -E 'preferences.json|\.env$'   # must return nothing
```

Both are gitignored, so the repository stays publishable if you later flip
it public. The only file worth a second look is `price_history.csv`, which
holds prices, dates, airports and public Google links — no identity.

Note that the scheduled job runs on **your machine**, not on GitHub. The
repo is for version control and backup. There is deliberately no scraping
workflow in `.github/workflows/` — a GitHub runner is a datacenter IP and
would get blocked. `test.yml` runs the offline test suite, which is fine.

## Honest caveats

- **Unofficial endpoint.** There is no public Google Flights API.
  `fast-flights` reads the page. It works well but can break without
  warning. If every line says `no result` for days, check
  [the repo](https://github.com/AWeirdDev/flights).
- **Only Chrome sees the cheap fares.** The HTTP scrape systematically
  misses the European routings where the sub-$1,400 fares live, and it
  cannot see a stay longer than about 30 nights at all. Never quote an HTTP
  price as "the cheapest".
- **An empty answer proves nothing.** It means this fetch method saw
  nothing — not that no fare exists. A cap was once added on that mistake
  and silently excluded the exact flight being tracked.
- **Osaka is filtered, not absent.** Google does return Osaka itineraries;
  every one of them transits the US or Canada, so none are bookable on this
  passport. If that ever changes, Osaka opens up immediately.
- **A tracked price is not a held quote.** Treat an alert as "go look now".
- **Airside transit rules are airline- and airport-dependent.** The deny
  list reflects the general rule for a Costa Rican passport as of August
  2026. Confirm with the airline before you pay.
- **Scraping is against Google's Terms of Service.** This is a personal,
  low-volume tool checking your own travel plans. Keep it that way.
