# SJO → Japan flight tracker

Watches Google Flights for round trips from San José, Costa Rica to Tokyo
(NRT/HND) and Osaka (KIX), **only via routes a Costa Rican passport can
actually use** — never through the US, Canada, mainland China or Russia.

You answer four questions once. After that it runs on a schedule forever and
emails you **at most twice a day**, only when something beats your price.
A quiet inbox means nothing did. That is the intended behaviour.

```bash
pip install -r requirements.txt
python setup_tracker.py        # asks: email, travel window, trip lengths, budget
python install_schedule.py     # installs the recurring job
```

Nothing personal is stored in a tracked file, so you can share or publish
this repository exactly as it is.

---

## How often should this run?

Short answer: **4 times a day, ~24 requests each.**

Longer answer, because the common advice on this is wrong in a way that
matters. You will read that scrapers should run "2 times per day" and that
this "completely avoids getting blocked". Two problems with that.

**Runs are the wrong unit. Requests are the unit.** One run of this tracker
is not one request — it is dozens. Compare:

| Schedule | Requests/run | Requests/day |
|---|---|---|
| "Safe" 2 runs/day, full scan | 90 | **180** |
| This project, 4 runs/day | 24 | **96** |
| This project, 6 runs/day | 16 | **96** |

The 6-run schedule makes roughly half the requests of the "safe" 2-run one.
Splitting a fixed daily budget across more runs is strictly better: smaller
bursts, spread further apart, and the fares you care about get re-checked
more often. So the tracker fixes a **daily request budget** and divides it,
rather than fixing a run count.

**Nothing "completely avoids" being blocked, and rate is not the main
driver anyway.** Detection weighs IP reputation, TLS fingerprint, header
patterns and timing together. The dominant factor for a small personal
scraper is which IP the traffic comes from — see deployment below.

The tracker does not rely on any of these numbers being right. It measures
its own empty-result rate every run and moves the budget itself:

| Empty results | Interpretation | Action |
|---|---|---|
| under 20% | healthy | budget +2 (max 40) |
| 20–50% | noisy | hold |
| over 50% | throttled | budget × 0.6 (min 8) |
| 3 bad runs running | blocked | warn, and say to check the IP |

Check where it has settled any time with `python -m tracker.cli --status`.

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
- **Windows** — prints the `schtasks` commands to paste.

```bash
python install_schedule.py                 # 4 runs/day: 06, 11, 16, 21
python install_schedule.py --runs 6        # 06, 09, 12, 15, 18, 21
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

So the second slot is held until 20:00 unless a standout fare appears,
because a standout may not survive the afternoon:

```
06:00  $1,370  -> email 1
11:00  $1,352  -> not materially better, skip
16:00  $1,180  -> better, but the slot is HELD in case something beats it
21:00  $1,050  -> email 2, and it is the day's cheapest
```

| Situation | Result |
|---|---|
| Nothing under your budget | no email |
| First qualifying fare of the day | **email 1** |
| Fare drops below the standout threshold | **email 2**, immediately |
| Better fare before 20:00 | held, not sent |
| Best of the day, at or after 20:00 | **email 2** |
| Anything after two emails | never |

Tune with `last_call_hour`, `min_drop_usd` and `reserve_last_slot` in
`config.yaml`, or set `reserve_last_slot: false` to hear about every
improvement as it happens.

## What the email contains

**The 20 cheapest options, ranked cheapest first**, drawn from anywhere in
the next 8 months. Each row shows the exact USD price, total door-to-door
duration, airlines, stops, the hub it routes through, and a **View** link
that opens exactly that search on Google Flights.

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

Setup asks five things. The window **rolls forward every day**, so it never
runs dry and you never have to reset dates:

```
Search the next N months     : 8
Ignore departures sooner than: 21 days
Priority months              : January, February, March
Minimum % from those months  : 50
Trip lengths                 : 2-5   (or "2,3,4,5", or just "3")
Flex in days                 : 0     (±2 checks 12-16 nights for a 2-week trip)
Sample every N days          : 4
```

That is 224 date combinations, or 672 searches for a complete pass. Far too
many for one run, so coverage has three parts:

- **Hot list** — windows already known to be cheap, re-priced on **every
  run**. This is what makes "prioritise the cheapest" real: your best
  candidates are watched four times a day, so a drop is caught within hours.
- **Priority rotation** — your chosen months get half of what is left,
  revisited about every 6 days.
- **General rotation** — everything else, about every 8 days.

Because priority months are 41% of the space but get 50% of the budget,
they are deliberately oversampled.

Changing your mind later is a one-line edit to `preferences.json`:

```json
"trip_weeks": [2, 3, 4, 5, 6],
"priority_months": [1, 2, 3, 11],
"search_months": 10
```

Or re-run `python setup_tracker.py`; it offers your current answers as
defaults. To pin a fixed window instead of a rolling one, set both
`earliest_departure` and `latest_departure`.

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

Three sources, best first:

1. **Google's own insight**, scraped from the page payload.
2. **Your own history**, once `price_history.csv` holds 25+ observations
   over 5+ separate days. Below p20 is cheap, above p80 is expensive.
3. **Seed bands** until then, derived from the Google Flights digest you
   already receive. Your two screenshots pinned the exchange rate exactly —
   the same Jan 15–24 itinerary appeared as both CRC 767,308 and $1,658,
   giving 462.79 CRC/USD:

   | | CRC | USD |
   |---|---|---|
   | Cheap below | 550,000 | **$1,188** |
   | Travellers usually book at | 615,055 | **$1,329** |
   | Expensive above | 1,050,000 | **$2,269** |

**$1,380 sits above the $1,329 people typically pay**, so it will fire on
fairly ordinary fares. Consider $1,250 / $1,100 once you have a couple of
weeks of history.

## Commands

```bash
python -m tracker.cli --status                        # settings and coverage
python -m tracker.cli --dry-run --save-preview e.html # test, send nothing
python -m tracker.cli -v                              # one real run
python -m tracker.cli --budget 12                     # smaller run
python -m pytest tests/ -q                            # 365 tests, offline
```

## Files

| File | Committed | Contains |
|---|---|---|
| `config.yaml` | yes | search parameters, nothing personal |
| `preferences.json` | **no** | your email, dates, trip lengths, budget |
| `.env` | **no** | SMTP credentials |
| `price_history.csv` | optional | prices, dates, airports, public links |
| `state.json`, `throttle.json`, `rotation.json` | no | runtime state |

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
- **Google's price-insight extraction is unverified against live traffic.**
  It fails safe and falls back to seed bands. Confirm on the first real run.
- **A tracked price is not a held quote.** Treat an alert as "go look now".
- **Airside transit rules are airline- and airport-dependent.** The deny
  list reflects the general rule for a Costa Rican passport as of August
  2026. Confirm with the airline before you pay.
- **Scraping is against Google's Terms of Service.** This is a personal,
  low-volume tool checking your own travel plans. Keep it that way.
