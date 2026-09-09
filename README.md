# Drishti (दृष्टि)

> Formerly named **Suchak**. The database file (`suchak.db`) and the
> `SUCHAK_*` environment variables keep their old names on purpose, so an
> existing installation keeps its data and API keys across the rename —
> nothing on your computer needs to be re-set.

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
entities, users, alerts, and news items, so every screen is populated
immediately.

### Demo accounts

| Login | Role | Sees |
|---|---|---|
| `admin` / `admin123` | Super admin | Cross-entity overview + everything |
| `priya` / `priya123` | Team lead | Bharat National Bank queue, dashboard, alerts, fetch |
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
to that fetch alone — nothing standing changes and no restart is needed. The
news picker defaults to 7 days and the social picker to the standing social
window (365 days), because complaint patterns build over months; the
completion pop-up states the window that actually ran. The all-entities
sweep's picker sets the news window only — social keeps its full window
there, so a routine sweep never silently discards a year of complaint
history. Use
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
  Word overlap only clubs the first day's headlines ("CEO resigns" in six
  outlets), not the story's later angles ("shares dip after top boss exit",
  "what the succession means"). So the classifier clubs too: as each news
  item is classified it is shown the stories already on the queue for that
  entity (last 14 days) and, when the item continues one of them — a
  reaction, an explainer, a follow-up — it is folded in as another source
  instead of becoming a fresh item. Only a story the classifier was
  actually shown can be chosen. To club items stored before this:
  `python -m scripts.re_dedup --smart` (asks the model about the last 90
  days, prints every proposed group, and asks before changing anything;
  reviewed items always survive).
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
  summary, user-defined **Alert** matches, and organizations linked to the
  entity. Falls back to a keyword classifier if the API is unavailable, and
  every verdict records which classifier/model produced it.
- **Review** — a ranked queue (severity × actionability × relevance) where
  team members confirm or correct the category **and the severity**, mark
  actionability, and record the action taken. A reviewer's severity
  correction wins everywhere — queue order, chips, dashboards — and the
  classifier's original verdict stays on the audit line. Reviews accumulate
  rather than replace one another; see **Review history** below.
  Each queue row — and each card on the Social media tab, whose posts never
  reach the queue — carries a **Reviewed** tick box: mark an item reviewed
  — or send it back for another look — without opening it. The tick is the
  item's stored status, so it survives restarts and sessions until somebody
  changes it. Deliberately status-only: it fabricates no verdict, never
  locks the item (the full review form stays open either way), and
  unticking never erases a recorded review — corrections and history stand.
  Because it records no ruling, a ticked item is **not** a precedent and
  gets **no** review-history entry: only an item carrying a verdict
  (`review_relevant IS NOT NULL`) is drawn as a few-shot example or seeded
  into `reviews`. Without that guard an unruled item renders as *"not
  relevant; risk areas: none"* wherever a precedent is written, so clearing
  the queue by ticking would have taught the classifier those stories were
  irrelevant — and the startup backfill would have put a review nobody wrote
  into the supervisory trail. `_drop_phantom_reviews()` clears any such row
  left by the two builds that had the defect.
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
- **Alerts** (the `factors` table; renamed on screen in build .52) — team
  leads define named plain-language rules ("Sales malpractice: raise if…")
  that the classifier judges every item against. Each is scoped to one
  entity, to an **entity type** (`factors.entity_kind`, added in .53 — every
  NBFC, every Urban Cooperative Bank, covering entities of that type added
  later), or to every entity; the last two are the super admin's to set,
  since they fire on other teams' entities. A kind-scoped alert is counted
  only across the entities of that kind, so a stale flag left elsewhere
  never inflates its figure. A match shows as a solid
  red, white-text ⚑ chip on queue rows, social cards, and the item page;
  the Dashboard's alert panel counts news and social matches side by side,
  and the **Alerts** screen's *Matches* column makes the list a watch list
  — each count opens the filtered Queue or Social view behind it (the
  social view gained a `factor=` filter for this).
  Alerts are judged when an item is first classified, so **Re-check
  stored items against alerts** (super admin, Alerts screen) walks the
  live stored items in the background and re-flags each against the
  alerts active now — one model call per item (the `SUCHAK_RESCORE_MAX`
  cap applies), no call where an entity has no active alerts (stale
  flags are simply cleared), skipping unclassified, dismissed, filtered
  and rejected rows. Both walk buttons (Re-check on Alerts, Re-score on
  Policy) refuse a second run while one is going, report live progress
  ("on item 137 of 412"), and their screen renders the running /
  last-outcome line server-side — a long walk's 20-second completion
  toast is not the only record of what happened.
- **Identity, so a complaint is counted once** — public sources repeat
  themselves, and a duplicate is a false trend. Every stored item carries
  the source's own id for the post (`items.source_uid` — a tweet id, a
  Reddit entry id) and its link reduced to what identifies it
  (`items.url_key` via `canonical_url()`: tracking parameters, `m.`/`www.`
  prefixes and fragments dropped, and every form of an X status link —
  `/someone/status/123`, `/i/web/status/123`, `twitter.com`, `?t=&s=` —
  reduced to one). The store checks identity rather than the raw link, so
  the same tweet can no longer arrive twice under its two valid forms (it
  could: which link the app built depended on whether author handles had
  been bought). A restart backfills the comparable form for rows already
  stored, so the fix reaches history, not just new fetches.
- **Complainants, not posts** — `items.author_key` records who complained
  (`x:<author id>`, `reddit:u/<name>`, `ccin:<name>`), and the Social media
  tab and Complaints screen count distinct people beside the raw volume.
  X returns the author id, the conversation and what a post quotes at **no
  extra charge** — it bills per post returned, not per field — so
  `items.thread_key` and `items.quoted_key` are stored too, ready to
  collapse threads exactly rather than by guesswork. An item whose source
  names nobody counts as its own complainant: two unknowns are never
  folded together, so the figure can only overstate how many people are
  behind a set of complaints. The gap between the two numbers is the
  measurement of how much of the volume is repetition.
- **Tunable severity criteria** — the high/medium/low definitions the
  classifier applies are plain-language text edited by the super admin on
  the Policy page (stored in the DB, applied to new classifications). The
  no-API-key keyword fallback keeps its own fixed trigger words.
  **Two scales, and the item decides which applies**: the institutional one
  above is written for events at the entity (a default, a run, a breach),
  and judged by it every customer grievance reads low — so grievances have
  a scale of their own (`grievance_severity_definitions`, its default the
  supervisor's own bands: fraud / aggressive recovery / mis-selling / KYC /
  complaint-handling high; service disruption / lending conduct /
  unauthorized transactions medium; charges / documentation delays / credit
  bureau low). An item carrying complaint topics is scored by the grievance
  scale; everything else by the institutional one. **Re-score stored
  complaints** on the Policy page walks the complaints already collected
  and re-scores each against the scale now in force — one model call per
  complaint (`SUCHAK_RESCORE_MAX` caps a run, default 1000), touching only
  the classifier's severity column: a reviewer's correction is never
  overwritten and keeps winning everywhere.
- **Negative list** — plain-language descriptions of item types the team
  does *not* analyse (default: stock recommendations and share-price
  commentary), edited by the super admin on the Policy page. The cheap
  screen applies it before any expensive classification; a backstop in the
  full verdict covers gate-off mode, and the keyword fallback catches
  obvious stock-tip phrasing. Matching items are parked under the queue's
  *Filtered out* tab with the reason recorded — nothing is silently
  deleted, and genuine company events are never excluded merely because
  the share price is mentioned.
- **Source trust tiers** — every item is tiered *official* (RBI, exchanges)
  / *trusted* (the super-admin-editable outlet list on the Policy page,
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
- **Complaints tab** — a workspace of its own in the masthead, beside
  Overview, DoS View and RD View rather than inside either of them: it spans
  every entity the signed-in officer can see and both kinds of source, which
  is neither the DoS team's per-entity beat nor one RD's region. It is the
  one screen that counts news coverage and social media together, and the
  only one that opens on no individual complaint at all. The classifier tags customer-grievance items with topics —
  Mis-selling, Recovery practices, Service disruption, Unauthorized
  transactions, Charges & fees, Harassment, Account access / KYC, Other
  grievance — and this screen crosses them against the entity, against the
  type of entity (so a pattern that belongs to a sector rather than to one
  institution shows up as a row of its own), and against the place the
  story is about, as three heat matrices shaded on one shared scale. Filters for date,
  type of entity, entity, location, source and severity narrow every figure
  on the page, tiles included; every cell, bar and tile is a link that narrows the page
  further, down to the complaints themselves at the foot of it. Where a
  complaint happened is read from the classifier's own geography verdict
  first and from the districts and states the story names second, with the
  entity's own name excluded from that scan so *Bank of Maharashtra* is
  never placed in Maharashtra; a national story and one that names no place
  each get an honest row of their own rather than being dropped. A row
  total counts complaints and a cell counts complaints carrying that
  category, so a row of cells adds up to more than its total — most
  grievances carry more than one — and the page says so under every matrix.
  Every control counts as if its own choice were not made, so a figure
  beside a control always matches the page that control opens: with Medium
  selected, the High severity tile still shows how many high-severity
  complaints are there to switch to, and the matrix keeps every entity and
  category in view so you can move to another. The tiles for the total, the
  entities and the categories, and the list at the foot, show the selection
  itself. Left out: social posts a reviewer set aside as no use for
  pattern-finding, as on Insights, and anything a reviewer dismissed as not
  this entity's — the review form arrives with the classifier's categories
  already ticked, so a dismissal would otherwise reach this screen as the
  reviewer's own ruling that the item *is* a grievance. The per-entity dashboard's Complaints tile
  still opens the queue grouped by topic, for one bank at a time.
- **Help** — the user guide, in the app rather than in this file: what each
  screen answers, how to read it, the vocabulary, and a role table. It is
  written for the officer signed in, and scoped to what that officer can
  actually open — a member is not told about Settings, a Regional Director is
  not sent to a DoS View they do not have — so it never sends anyone looking
  for a tab their role does not carry. The scoping comes from
  `_visible_screens()`, which mirrors the masthead's rules and the route
  guards; add a screen and its section belongs there too. Prints cleanly.
  This README stays the document for whoever installs and runs the thing.
- **Dashboards** — per-entity risk-area breakdown, severity tiles, complaints
  tile with by-topic breakdown, an **Open actions** tile (with an overdue
  sub-count) into the To-do page, daily volume trend, alert hits, and
  extracted organization linkages — every figure is a drill-down into the
  exact items it counts; a cross-entity overview for the super admin.
  The entity chosen on any DoS View screen follows you across its tab bar:
  Dashboard, Queue, Social media, Insights and To-do each open on the
  entity the last screen was showing, rather than snapping back to the
  first on the roster.
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
link is cleared), global alerts, and all settings; the database will not
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

Signing in by hand is a supported path, not a fallback. With
`--show-browser` the collector waits (default 4 minutes,
`SUCHAK_X_MANUAL_LOGIN_SECONDS`) for the session to become authenticated
however that happens — you solving a CAPTCHA, approving a device, or simply
logging in yourself — and saves it. It also recognises a browser X already
trusts and skips the form entirely.

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

## Reading marks on the RD View

A Regional Director works down a long list, and the second visit should
start where the first left off. Each story on the RD View carries a tick
box: ticking it greys the row, and the **Show** filter narrows the page to
what is not yet ticked.

Three properties are deliberate:

- **Per reader.** The mark lives in `item_reads (item_id, user_id)`. One
  office having read a story says nothing about whether another has, and
  the superadmin's reading never greys the page for an RD.
- **Not a verdict.** It records that someone has seen the item and changes
  nothing else — not its severity, not its review state, not what the
  classifier learns. The supervisory record is untouched.
- **Reversible.** It is a checkbox, not a radio: a radio cannot be
  un-clicked, and a reader who ticks the wrong line must be able to take it
  back.

A marked story is greyed rather than hidden, because a reading mark is not
a deletion — the second pass must still be able to find it.

## Design system

The stylesheet, `app/static/style.css`, is a small design system rather than
a pile of page-specific rules, and it is written to enterprise conventions:
structure carries the meaning and colour only qualifies it, a hairline
border defines a surface, every figure is a link and every column of
figures is aligned on the digit. It is deliberately dense — this is a tool
read for hours, not a page browsed once.

Work with it rather than around it:

- **Tokens first.** Colour, type, spacing, radius and shadow all come from
  the custom properties at the top of the file. A new colour goes in the
  palette or it does not go in.
- **One control height, one radius family.** `--control-h` and the `--r-*`
  radii keep buttons, inputs and dropdowns on a common line. A screen that
  mixes them reads as unfinished however good each part is.
- **Reuse the vocabulary.** Severity, risk, topic, entity and status all
  have a badge class already; a table has `.data-table`, a metric has
  `.tile`, a boxed region has `.panel`, and a filter row has `.toolbar`
  with `.filter-form`.
- **Colour is never the only signal.** Severity chips carry the word, an
  overdue follow-up carries the "overdue" chip, and a serious row is
  marked on its leading edge rather than by tinting the whole row.
- **No external assets.** No web fonts, no icon set, no CDN — the system
  font stack only, because this runs on a laptop behind a filtered network.

The shell is two tiers: a dark masthead that never changes and says which
application this is, and a light context bar beneath it that says which
screen of the current workspace is open. Both live in `templates/base.html`.
Shared filter controls live in `templates/_filters.html`.

An item's page follows the record-and-inspector pattern: the left column is
the record — the story, its sources, its linkages and the trail of reviews
on it — and the right rail is the work, showing what past rulings say and
then the form that records the decision. Settings and Policy share a
third pattern: a rule's name and explanation on the left at a readable
measure, its control on the right, one row to a rule.
