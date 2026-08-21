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
in that entity's queue. A background cycle also runs every 30 minutes.

## What it does

- **Ingest** — Google News RSS query feed per entity, built from its alias
  list, limited to a lookback window. One source type for week one; Google
  News already aggregates the Indian financial press.
- **Disambiguate** — names that contain other names ("Bank of India" inside
  "State Bank of India") are resolved by longest match across the whole
  entity registry, with word boundaries, so a story about one bank is never
  filed under another. Rival names are also excluded from each search query.
  All of this is free and runs before anything is stored.
- **De-duplicate** — the same story from many outlets is clustered into one
  review item by title similarity; extra outlets attach as additional sources.
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
  team members confirm or correct the category, mark actionability, and
  record the action taken.
- **Learn** — reviewed items become retrieval-based few-shot examples for
  future classification, and power "suggested action" on similar new items.
  No fine-tuning needed.
- **Factors** — team leads define named plain-language rules ("Sales
  malpractice: flag if…") that the classifier evaluates on every item.
- **Dashboards** — per-entity risk-area breakdown, severity tiles, daily
  volume trend, factor hits, and extracted organization linkages; a
  cross-entity overview for the super admin.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables LLM classification (else keyword fallback) |
| `SUCHAK_MODEL` | `claude-opus-5` | Claude model used for classification |
| `SUCHAK_GATE_MODEL` | `claude-haiku-4-5` | Cheap relevance screen; `""` disables it |
| `SUCHAK_LOOKBACK_DAYS` | `30` | How far back each feed asks for news; `0` = current |
| `SUCHAK_MAX_ENTRIES` | `100` | Items per feed (Google News returns ~100 max) |
| `SUCHAK_FETCH_DELAY` | `1.5` | Seconds between entity feeds during a sweep |
| `SUCHAK_FETCH_MINUTES` | `30` | Background fetch interval; `0` disables |
| `SUCHAK_DB` | `./suchak.db` | SQLite database path |
| `SUCHAK_SECRET` | random per start | Session-cookie signing key (set for stable logins) |

## Scope cuts for the one-week build

Deliberately deferred, per the design doc: YouTube (removed from scope),
Reddit/social feeds, GDELT and per-outlet RSS (Google News covers them),
embedding models (TF-IDF similarity is enough at this scale), PostgreSQL
(SQLite), Celery (in-process background task), separate React frontend
(server-rendered pages), SSO (simple sessions), CSRF protection, and
second-order **alerts** (linkages are extracted and displayed; alerting
across entities is the natural next step).

## Loading India's scheduled commercial banks

```bash
python -m scripts.load_banks     # 33 SCBs: 12 public sector, 21 private
```

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
  ingest.py      Google News RSS fetch + dedup
  classify.py    Claude structured classification + keyword fallback + learning loop
  similarity.py  Pure-Python TF-IDF (dedup + retrieval)
  auth.py        Passwords + sessions
  matching.py    Entity resolution: longest-match disambiguation, query building
  seed.py        Fictional demo data
  templates/     Server-rendered pages
  static/        Stylesheet
```
