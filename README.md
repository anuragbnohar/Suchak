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
  list. One source type for week one; Google News already aggregates the
  Indian financial press.
- **De-duplicate** — the same story from many outlets is clustered into one
  review item by title similarity; extra outlets attach as additional sources.
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
  seed.py        Fictional demo data
  templates/     Server-rendered pages
  static/        Stylesheet
```
