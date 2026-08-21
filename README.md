# Suchak (सूचक)

A one-week prototype of a supervisory intelligence platform for SSM teams at
the Banking Supervisor of India. It collects public news about regulated
entities, de-duplicates it, classifies it by risk area with an LLM, and puts
it in front of a small team for review — learning from every review it
records.

Full design rationale: [`docs/SOLUTION_PROPOSAL.md`](docs/SOLUTION_PROPOSAL.md).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional but recommended — enables LLM classification.
# Without it, a keyword fallback classifier keeps the pipeline working.
export ANTHROPIC_API_KEY=sk-ant-...

python run.py            # http://localhost:8000
```

On first start the app seeds a demo database (`suchak.db`) with **fictional**
entities, users, factors, and news items, so every screen is populated
immediately.

### Demo accounts

| Login | Role | Sees |
|---|---|---|
| `admin` / `admin123` | Super admin | Cross-entity overview + everything |
| `priya` / `priya123` | Team lead | Bharat National Bank queue, dashboard, factors, fetch |
| `rahul` / `rahul123` | Team member | Bharat National Bank queue + dashboard |

### Pulling live news

Demo entities are fictional and won't match real news. To see the live
pipeline: sign in as `admin` → **Entities** → add a real regulated entity
with its aliases (e.g. *State Bank of India, SBI*) → **Fetch**. Items arrive
via Google News RSS (free, no key), get de-duplicated, classified, and appear
in that entity's queue. Fetching is **on demand only** — each entity has its
own Fetch button, and nothing is collected until you press one. Set
`SUCHAK_FETCH_MINUTES` to a positive number to add an automatic sweep;
remember it fetches *every* loaded entity and bills for it unattended.

## What it does

- **Ingest** — pluggable sources, one normalized item shape:
  - *Google News RSS* — free, no key. An aggregator, so one query per entity
    reaches the whole Indian financial press.
  - *YouTube Data API v3* — video coverage per entity. Needs a free API key
    in `SUCHAK_YOUTUBE_KEY`; skipped silently when unset.
  - *X/Twitter recent search* — customer complaints only. **The one paid
    source**, and **off by default**: it needs `SUCHAK_X_ENABLED=1` *and*
    `SUCHAK_X_BEARER`, so a token left in the environment can never start
    spending on its own. When on, the query is deliberately narrow and the
    result count hard-capped. `scripts/x_trial.py` still runs a bounded,
    confirmed one-off trial without the standing switch.

  **Broadcast sources** are fetched *once per sweep* — one feed covers every
  entity — and each item is routed to the entities it names, via the same
  longest-match registry that keeps news attribution honest. All free, on by
  default:
  - *RBI press releases* (RSS) — penalties, enforcement actions, directions.
  - *NSE corporate announcements* (RSS) — board outcomes, disclosures.
  - *BSE corporate announcements* (JSON) — the endpoint the BSE website
    itself uses; there is no separate documented API, so treat it as
    changeable.

  Items from official sources skip the paid relevance screen (routing is
  already deterministic) and carry *regulator* / *exchange filing* chips in
  the queue. An RBI release and the news coverage of the same penalty merge
  into one review item; distinct exchange filings with formulaic identical
  titles deliberately do **not** merge.

  Adding a source means adding one function and one entry in `SOURCES`;
  everything downstream is source-agnostic.
- **Disambiguate** — names that contain other names ("Bank of India" inside
  "State Bank of India") are resolved by longest match across the whole
  entity registry, with word boundaries, so a story about one bank is never
  filed under another. Rival names are also excluded from each search query.
  All of this is free and runs before anything is stored.
- **De-duplicate** — the same story from many outlets is clustered into one
  review item; extra outlets attach as additional sources. Google News
  publisher suffixes are stripped, plurals and month names fold together,
  the entity's own name is ignored (it inflates every pair equally), and a
  headline joins a cluster if it matches *any* variant already in it. A
  merge needs at least three shared distinctive words. To re-cluster items
  ingested before these rules: `python -m scripts.re_dedup` (reviewed items
  always survive as the primary).
  De-duplication spans source types, so a video and an article about the same
  event become one item. Social posts are excluded from this: ten customers
  complaining about blocked cards is ten data points, not one story told ten
  times — volume *is* the conduct signal.
- **Screen** — a small, cheap model (Haiku 4.5 by default) decides whether
  each item is genuinely about the entity before the full classification
  runs. Rejected items cost about a fiftieth of a verdict, stay out of the
  queue, and remain visible under the queue's *Filtered out* tab with the
  reason recorded. Set `SUCHAK_GATE_MODEL=""` to disable.
- **Classify** — one Claude call per item returns a strict JSON verdict:
  relevance, risk areas, severity, actionability, geography, a one-line
  summary, user-defined **Factor** matches, and organizations linked to the
  entity. Falls back to a keyword classifier if the API is unavailable, and
  every verdict records which classifier/model produced it.
- **Review** — a ranked queue (severity × actionability × relevance) where
  team members confirm or correct the category **and the severity**, mark
  actionability, and record the action taken. A reviewer's severity
  correction wins everywhere — queue order, chips, dashboards — and the
  classifier's original verdict stays on the audit line.
- **Learn** — reviewed items become retrieval-based few-shot examples for
  future classification, and power "suggested action" on similar new items.
  No fine-tuning needed.
- **Factors** — team leads define named plain-language rules ("Sales
  malpractice: flag if…") that the classifier evaluates on every item.
- **Tunable severity criteria** — the high/medium/low definitions the
  classifier applies are plain-language text edited by the super admin on
  the Factors page (stored in the DB, applied to new classifications). The
  no-API-key keyword fallback keeps its own fixed trigger words.
- **Negative list** — plain-language descriptions of item types the team
  does *not* analyse (default: stock recommendations and share-price
  commentary), edited by the super admin on the Factors page. The cheap
  screen applies it before any expensive classification; a backstop in the
  full verdict covers gate-off mode, and the keyword fallback catches
  obvious stock-tip phrasing. Matching items are parked under the queue's
  *Filtered out* tab with the reason recorded — nothing is silently
  deleted, and genuine company events are never excluded merely because
  the share price is mentioned.
- **Source trust tiers** — every item is tiered *official* (RBI, exchanges)
  / *trusted* (the super-admin-editable outlet list on the Factors page,
  seeded with the major Indian financial press and wires) / *other*. Within
  the same severity, official and trusted sources rank first; trusted items
  carry a ✓ beside the outlet; the queue's source filter shows trusted
  sources only. Editing the list re-tiers every stored item immediately.
- **Complaints view** — the classifier tags customer-grievance items (from
  news and social posts) with topics: Mis-selling, Recovery practices,
  Service disruption, Unauthorized transactions, Charges & fees, Harassment,
  Account access / KYC. The dashboard's Complaints tile opens the queue
  grouped by topic; a by-topic table drills into each.
- **Dashboards** — per-entity risk-area breakdown, severity tiles, complaints
  tile with by-topic breakdown, daily volume trend, factor hits, and
  extracted organization linkages — every figure is a drill-down into the
  exact items it counts; a cross-entity overview for the super admin.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables LLM classification (else keyword fallback) |
| `SUCHAK_MODEL` | `claude-sonnet-5` | Model that writes the full verdict |
| `SUCHAK_GATE_MODEL` | `claude-haiku-4-5` | Cheap model screening every fetched item; `""` disables it |
| `SUCHAK_X_ENABLED` | unset (off) | Must be `1` for the paid X source to run at all |
| `SUCHAK_YOUTUBE_KEY` | unset | YouTube Data API key; unset disables video ingestion |
| `SUCHAK_YOUTUBE_MAX` | `25` | Videos per entity per sweep (API caps at 50) |
| `SUCHAK_X_ENABLED` | unset (off) | X is off unless this is `1` **and** a bearer token is set |
| `SUCHAK_X_BEARER` | unset | X API bearer token; both this and the flag are required |
| `SUCHAK_X_MAX_POSTS` | `100` | **Hard spend cap**: posts per entity per sweep |
| `SUCHAK_X_STRATEGY` | `complaints` | `complaints`, `care_handle`, or `both` |
| `SUCHAK_X_LANGS` | `en,hi` | Languages fetched from X |
| `SUCHAK_X_COMPLAINT_TERMS` | see `ingest.py` | Grievance vocabulary ANDed with the entity |
| `SUCHAK_X_PRICE_PER_POST` | `0.005` | Used only to report estimated spend |
| `SUCHAK_RBI_RSS` | RBI press-release feed | `""` disables; override if RBI moves the URL |
| `SUCHAK_NSE_RSS` | NSE announcements feed | `""` disables |
| `SUCHAK_BSE_API` | BSE announcements endpoint | `""` disables |
| `SUCHAK_BROADCAST_MAX` | `200` | Items taken per broadcast feed per sweep |
| `SUCHAK_LOOKBACK_DAYS` | `30` | How far back each feed asks for news; `0` = current |
| `SUCHAK_MAX_ENTRIES` | `100` | Items per feed (Google News returns ~100 max) |
| `SUCHAK_FETCH_DELAY` | `1.5` | Seconds between entity feeds during a sweep |
| `SUCHAK_FETCH_MINUTES` | `0` (manual only) | Minutes between automatic sweeps; `0` fetches only on demand |
| `SUCHAK_DB` | `./suchak.db` | SQLite database path |
| `SUCHAK_SECRET` | random per start | Session-cookie signing key (set for stable logins) |

## Scope cuts for the one-week build

Deliberately deferred, per the design doc: Reddit/social feeds,
GDELT and per-outlet RSS (Google News covers them),
embedding models (TF-IDF similarity is enough at this scale), PostgreSQL
(SQLite), Celery (in-process background task), separate React frontend
(server-rendered pages), SSO (simple sessions), CSRF protection, and
second-order **alerts** (linkages are extracted and displayed; alerting
across entities is the natural next step).

### Enabling YouTube

Create a project at <https://console.cloud.google.com>, enable **YouTube Data
API v3**, create an API key, then:

```bash
export SUCHAK_YOUTUBE_KEY=AIza...        # PowerShell: $env:SUCHAK_YOUTUBE_KEY="AIza..."
```

Quota is free: `search.list` costs 100 units against a 10,000/day allowance,
so a 33-bank sweep costs 3,300 units and roughly three sweeps a day fit
inside the free tier. Videos carry a `video` chip in the queue and flow
through the same disambiguation, de-duplication, screening and
classification as news.

### Regulator & exchange feeds (free, on by default)

Nothing to configure — each sweep pulls RBI press releases and NSE/BSE
corporate announcements once and routes items to the banks they name. The
**Entities** page shows each feed's last status (items fetched, routed, or
the error if the fetch failed).

One-time check on first run: these endpoints were unreachable from the
environment this code was built in, so the default URLs are best-effort.
If a feed shows `fetch failed` on the Entities page, find the current URL
(RBI's RSS page; NSE's "RSS feeds" page; the announcements API called by
`bseindia.com/corporates/ann.html`) and set `SUCHAK_RBI_RSS` /
`SUCHAK_NSE_RSS` / `SUCHAK_BSE_API` accordingly.

### Enabling X/Twitter (paid — read this first)

X is the only source that costs money, and it bills **per post returned**,
not per query. Three things keep that bounded:

1. The query is narrow by construction — entity terms ANDed with grievance
   vocabulary, retweets and promoted posts excluded.
2. `SUCHAK_X_MAX_POSTS` is a hard ceiling per entity per sweep. One request,
   never paginated, so the bill cannot exceed it. At the default 100 posts
   and $0.005/post that is $0.50 per bank per sweep, whatever happens.
3. Every fetch logs how many billed posts it pulled and the estimated spend,
   visible on the Entities page.

`SUCHAK_X_STRATEGY` picks how complaints are found:

| Strategy | Query | Notes |
|---|---|---|
| `complaints` | entity names AND grievance vocabulary | Works with no extra config |
| `care_handle` | `to:<bank grievance handle>` | Highest precision, cheapest; needs `x_handle` set |
| `both` | either of the above | Widest, and the most expensive |

Note X's recent-search endpoint only covers the **last 7 days**, whatever
`SUCHAK_LOOKBACK_DAYS` says. Going further back needs full-archive access.

Run a bounded trial for one bank — it prints the query and the worst-case
cost and waits for confirmation before spending anything:

```bash
export SUCHAK_X_BEARER=...
python -m scripts.x_trial "HDFC Bank Ltd." --handle HDFCBank_Cares --max 100
```

Verify a bank's grievance handle on X before using `care_handle`; a wrong
handle silently returns nothing.

## Re-classifying items already collected

Classification happens once, at fetch time, so items collected before an
API key was set keep their keyword-rule verdicts. To re-judge them with
the configured models:

```bash
python -m scripts.reclassify --entity "ICICI Bank Ltd."   # try one bank first
python -m scripts.reclassify                              # everything
```

Shows the item count, both models, a cost estimate and a runtime estimate,
then waits for confirmation. Reviewed items are skipped unless
`--include-reviewed` is passed.

## Clearing the demo data

```bash
python -m scripts.reset_data              # entities AND items; asks first
python -m scripts.reset_data --items-only # just the collected items
```

`--items-only` clears everything fetched but keeps your entities, their
aliases and the team assignments — the right one for re-collecting from
scratch without rebuilding the setup.

Permanently deletes every entity and every collected item (including
reviews on them) and the fetch log. Keeps user accounts (their team-entity
link is cleared), global factors, and all settings; the database will not
re-seed demo data afterwards. Typical sequence for going live:

```bash
python -m scripts.reset_data --yes
python -m scripts.load_banks --team "HDFC Bank Ltd."
```

`--team` points the lead/member demo accounts (priya, rahul) at the named
bank so they have a queue to work; the super admin sees everything anyway.

## Loading India's scheduled commercial banks

```bash
python -m scripts.load_banks                        # all 33 SCBs
python -m scripts.load_banks --only "SBI,HDFC,ICICI"  # just a few, for a pilot
```

`--only` matches its terms against each bank's name and aliases, so short
forms are enough. Load only what you intend to monitor: the background
sweep fetches **every** loaded entity, so an unused entity still costs
API calls on each cycle.

Then sign in as `admin` and use **Entities -> Fetch now**. Aliases are chosen
for precision -- ambiguous abbreviations (BoB, TMB) are deliberately omitted,
since a bad alias costs money and fills the queue with another bank's news.
Editing an entity's aliases in the UI is the lever for tuning this.

## Layout

```
app/
  main.py        FastAPI app + routes
  db.py          SQLite schema and helpers
  taxonomy.py    Risk areas, severities, actions — finalize with the team
  ingest.py      Pluggable sources (news, video, X) + dedup
  classify.py    Claude structured classification + keyword fallback + learning loop
  similarity.py  Pure-Python TF-IDF (dedup + retrieval)
  auth.py        Passwords + sessions
  matching.py    Entity resolution: longest-match disambiguation, query building
  seed.py        Fictional demo data
  templates/     Server-rendered pages
  static/        Stylesheet
```
