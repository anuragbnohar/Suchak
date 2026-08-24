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

Each Fetch button has a **lookback picker** (7 / 30 / 90 / 365 days) applying
to that fetch alone — nothing standing changes and no restart is needed. Use
it when an entity comes back empty: a small cooperative bank can go months
without press coverage, and a standing 7-day window cannot tell you that.
Re-fetching is incremental, so a wider window only classifies — and only
bills for — what is not already stored. `SUCHAK_LOOKBACK_DAYS` still sets the
standing default for every entity.

Broadcast feeds (RBI, exchanges) keep their own window: one fetch serves
every entity, so widening them on one entity's behalf would re-scan the lot.
X recent search is capped at 7 days by its API regardless.

## What it does

- **Ingest** — pluggable sources, one normalized item shape. Every fetch is
  **incremental over a rolling window** (7 days by default, widenable to 30,
  90 or 365 for one fetch from the picker beside each Fetch button; up to 100
  items per entity): URLs already stored are skipped, and a story another outlet
  re-reports merges into the item it duplicates. Re-pressing Fetch therefore
  classifies — and bills for — only what is genuinely new.
  - *Google News RSS* — free, no key. An aggregator, so one query per entity
    reaches the whole Indian financial press. **One feed per language
    configured on the entity** (`en`, `hi`, `mr`, `gu`, `bn`, `ta`, `te`,
    `kn`, `ml`), because Google News publishes a separate edition per
    language. Languages are per entity on purpose: a Maharashtra cooperative
    bank is covered in Marathi, a national bank is not, and fetching every
    language for every entity multiplies volume and cost for nothing. A
    language edition that fails is logged and skipped rather than losing the
    ones that worked, and the per-entity item cap applies to the combined
    result, so adding a language never raises the ceiling that bounds
    classification spend.
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
- **Work in more than one language** — a regional entity is reported in the
  regional press first. Two things have to line up, and only together:
  the entity needs the language in its list (which feed is fetched) *and* an
  alias written in that script (whether the item is attributed to it).
  Attribution is a regex over the alias list and runs before any model, so a
  Marathi headline with only a Latin alias is dropped silently and never
  reaches the classifier. Devanagari aliases match inflected forms for free —
  `नागपूर नागरिक सहकारी बँक` matches `…बँकेवर` and `…बँकेला` — because
  Marathi case suffixes attach as combining marks, which fall outside the
  word boundary. Both models read the source language and are instructed to
  write every verdict field in English, so one queue stays scannable by the
  whole team. The Marathi press (Lokmat, Sakal, Loksatta, Maharashtra Times,
  Pudhari, Tarun Bharat, Divya Marathi, ABP Majha, TV9 Marathi) is in the
  trusted-source list, so regional coverage of a regional entity does not
  rank below the national English papers that never mention it.
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
  classifier's original verdict stays on the audit line. Reviews accumulate
  rather than replace one another; see **Review history** below.
- **Review history** — every review is kept, never overwritten. Each
  submission is appended to a `reviews` table with its reviewer, role and
  timestamp; the item's own `review_*` columns mirror the latest one, so the
  queue, dashboards and learning loop still read a single current verdict.
  The item page lists them oldest first on a timeline, each entry showing
  what it *changed* from the one before it (`severity medium → high`,
  `actionable no → yes`) rather than only what it restated, with the last
  marked **current**. A re-review that alters nothing is labelled as
  confirming the previous verdict. The queue flags items reviewed more than
  once. Reviews recorded before this table existed are backfilled on first
  start, so no history is lost.
- **To-do** — answering *Actionable: Yes* on a review opens a follow-up on
  that item, and the To-do page is where the team closes it. Members and
  leads see their entity's follow-ups, the super admin sees every entity's.
  Each carries an owner (the reviewer by default), an optional due date, the
  recorded action and the reviewer's note. Closing is one click; a disclosure
  on each card holds the closing note, the owner and the due date, so the
  default view stays a readable list rather than a wall of form fields.
  Tabs for Open / Closed / All, quick filters for overdue and
  *assigned to me*, dropdowns for severity, risk area and entity, and a count
  in the nav so open work is visible from every page. Overdue items sort first,
  then by severity. Leads and the super admin can reassign or change a due
  date; members close what they own. Re-reviewing an item never reopens work
  someone already closed — the review records the judgment, the To-do records
  the work. Reviews recorded before this existed are backfilled as open
  follow-ups on first start.
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
- **Social media** — a screen of customer grievances posted to each entity's
  grievance handle on X, grouped by complaint topic. Deliberately narrower
  than the queue: a post is listed only if the classifier found a grievance
  in it. Praise, questions and noise addressed to the same handle are counted
  in the footer but not shown, because the question this screen answers is
  *what are customers complaining about*, not *what was said*. Each post links
  out to the original and in to its review page; the handle is set per entity
  on the Entities page. Collection is the `care_handle` strategy — `to:handle`
  — capped at `SUCHAK_X_MAX_POSTS` (default 50) per entity per fetch, and off
  until `SUCHAK_X_ENABLED=1`.
- **Complaints view** — the classifier tags customer-grievance items (from
  news and social posts) with topics: Mis-selling, Recovery practices,
  Service disruption, Unauthorized transactions, Charges & fees, Harassment,
  Account access / KYC. The dashboard's Complaints tile opens the queue
  grouped by topic; a by-topic table drills into each.
- **Dashboards** — per-entity risk-area breakdown, severity tiles, complaints
  tile with by-topic breakdown, an **Open actions** tile (with an overdue
  sub-count) into the To-do page, daily volume trend, factor hits, and
  extracted organization linkages — every figure is a drill-down into the
  exact items it counts; a cross-entity overview for the super admin.
- **Three ways to read the same week** — the super admin's overview groups by
  **Entity** ("who needs attention"), **Severity** ("how serious is this
  week") or **Risk area** ("what kind of problem is showing up"). The two
  category views span every entity, and each row keeps its per-entity split
  as clickable chips: the queue is per entity, so a cross-entity total nobody
  could open would break the rule that every figure is a drill-down. A
  reviewer's severity *and* risk-area corrections drive the grouping, not the
  classifier's original verdict — on every surface, not just here. Items still
  awaiting classification are excluded and counted in a footnote: they carry
  no verdict, and including them would file every one under *low*.

  Every figure in these views opens the items behind it. Totals and
  awaiting-review counts lead to a **cross-entity queue** (`?entity=all`,
  super admin only), which labels each row with the entity it belongs to; the
  per-entity chips lead to that entity's own filtered queue. A number a
  supervisor cannot open is not a drill-down.
  Two deliberate exceptions to the day window: *Awaiting review* and *Open
  actions* are all-time, because an item unread or a follow-up owed five weeks
  ago is still work today. Both are labelled as such, and where an entity has
  items published outside the window the count carries a `+N older` link to
  the unwindowed queue — otherwise a quiet entity reads as a contradiction,
  nothing in the last seven days sitting beside four awaiting review.

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
| `SUCHAK_LOOKBACK_DAYS` | `7` | Rolling window each fetch asks for; raise for a one-off backfill |
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

## Setting the supervised roster

`scripts/set_roster.py` makes the entity list exactly the roster defined at
the top of that file — adding what is missing and removing what is not on it
— so changing which institutions are supervised is one command rather than
clicking through the UI.

```bash
python -m scripts.set_roster --dry-run       # show the plan, change nothing
python -m scripts.set_roster                 # plan, then confirm
python -m scripts.set_roster --yes           # no prompt
python -m scripts.set_roster --sync-aliases  # also align existing aliases/excludes
```

Each roster entry carries a name, a kind, aliases, exclude terms and news
languages. Entities already present keep their aliases, exclude terms and
languages untouched unless `--sync-aliases` is given, because those are tuned by hand on the Entities
page. Removal deletes the entity's items **and its review history**; the plan
counts both before anything happens, and asks before acting.

The roster is not limited to banks: `ENTITY_KINDS` covers NBFCs, urban and
rural cooperative banks and payment system operators, and the pipeline is
identical for all of them. What differs is where the signal comes from — a
large listed NBFC generates national press and exchange filings, while a
small cooperative bank is mostly covered by RBI press releases, which the
broadcast feed routes by mention.

Aliases and `exclude_terms` are what keep a shared brand from becoming noise.
`"Shriram Finance"` is an alias; `"Shriram"` alone is not, because the group
also runs insurance, housing-finance and properties arms. Exclude terms are
subtracted from the search query *and* from entity attribution, so
"SBI Life Insurance posts Q1 profit" resolves to no entity at all rather than
being filed under State Bank of India and paying for a screen to reject it.

Languages are also editable on the Entities page, in the same inline form as
the aliases — enter comma-separated codes. Unknown codes are dropped on save
rather than stored and silently ignored at fetch time.

To remove a single entity from the UI instead: **Entities → Remove**
(super admin only), which shows what will be deleted and asks you to type the
entity's name.

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

## Testing the browser collector

`scripts/probe_x.py` runs the X browser collector on its own — nothing is
stored, classified or billed — so a failure is attributable rather than
mixed into a fetch.

```bash
python -m scripts.probe_x --login-only --show-browser   # sign in once
python -m scripts.probe_x --entity HDFC --max 10        # probe one handle
```

It distinguishes the two outcomes that matter: **could not run** (session
expired, rate limit, markup changed) prints why and exits non-zero, while
**ran and found nothing** says so explicitly. Those look identical in a
normal fetch, and confusing them is how a broken collector gets read as a
quiet week.

Selectors live in `_EXTRACT` in `app/x_scrape.py` — one JS pass over the
timeline, deliberately kept in a single place, because X's markup is not an
interface and that block is what needs repairing when it changes.

## When an entity returns nothing

```bash
python -m scripts.probe_entity --entity Nagpur --days 365
```

Prints every headline the feed returned and whether attribution kept it, and
for each rejection names which alias words were present and which were
absent. It stores nothing, classifies nothing and bills nothing.

The fetch log's *"N rejected as another entity's news"* says attribution
refused the results but not what they were. This answers that — and the
answer is usually the alias list, not the window.

Attribution is a contiguous phrase match, which is what makes it precise and
also what makes it brittle against how the press actually writes a name. A
cooperative bank is the clear case: the same institution appears as *Nagpur
Nagarik Sahakari Bank*, *Nagpur Nagarik Sah Bank* and *Nagpur Nagarik Bank*
across three outlets, and neither transliteration of नागरिक is canonical.
None of the shortened forms match an alias holding the full name, so **every
form the press uses has to be listed**. Rejections are logged with their
headline, so a running server answers the same question in its console.

`SUCHAK_QUERY_ALIASES` (default 6) sets how many of an entity's aliases go
into each search query; the rest are still used for attribution. Aliases are
ordered per edition, so a Marathi feed is queried with the Devanagari
spellings and an English feed with the Latin ones.

## Layout

```
app/
  main.py        FastAPI app + routes
  db.py          SQLite schema, migrations, backfills
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
