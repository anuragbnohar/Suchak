# Suchak — Supervisory Intelligence Platform: Solution Proposal

*Proposal for a technical solution to the SSM team supervision problem described in SUCHAK.docx.*

---

## 1. Problem summary

Each regulated entity (Scheduled Commercial Banks, NBFCs, Urban/Rural Cooperative Banks, Authorised Payment System Operators) is supervised by a small SSM team of 4–5 people who must:

1. **Collect** all news, social media chatter, videos, and public grievances about their entity.
2. **Review** that flood of information and identify what is *actionable*.
3. **Categorize** items into risk areas (Credit, Market, Liquidity, Operational, Governance, Cybersecurity, …) — with team members of varying experience assigned to different risk areas.

The volume is far beyond what 4–5 people can process, so teams lack an overall picture of their entity, and identification quality depends heavily on individual reviewer experience.

## 2. Recommended approach in one paragraph

Build a **human-in-the-loop supervisory signal triage pipeline**: automate the collection, deduplication, entity-matching, and first-pass risk categorization of public information using an LLM-based classification layer; present the results in a review queue where SSM team members confirm, correct, and act; and feed every reviewer decision back into a knowledge base that improves future classification (via retrieval of similar past-labeled items) and powers action suggestions. Do **not** try to fully automate supervisory judgment — automate the 95% of the work that is collection and sorting, and multiply the effectiveness of the humans doing the 5% that is judgment.

## 3. System architecture

```
 ┌────────────────────────────────────────────────────────────────────┐
 │                        INGESTION LAYER (scheduled workers)         │
 │  Google News RSS · GDELT 2.0 · Press RSS feeds · YouTube API ·     │
 │  Reddit API · RBI press releases · NSE/BSE announcements           │
 └───────────────────────────────┬────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                  NORMALIZATION & DEDUPLICATION                     │
 │  Canonical article schema · embedding-based clustering so the      │
 │  same story from 15 outlets becomes ONE review item                │
 └───────────────────────────────┬────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                     ENTITY RESOLUTION                              │
 │  Alias dictionaries per regulated entity ("SBI", "State Bank",     │
 │  "भारतीय स्टेट बैंक") + fuzzy matching + LLM disambiguation          │
 └───────────────────────────────┬────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                LLM CLASSIFICATION & ENRICHMENT                     │
 │  Structured output per item: relevance score · risk area(s) ·      │
 │  severity · actionability · geography · one-line summary ·         │
 │  custom Factor matches · extracted relationships (for the graph)   │
 │  Few-shot examples retrieved from the reviewer-labeled KB          │
 └───────────────────────────────┬────────────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                  TRIAGE & REVIEW WORKFLOW (web UI)                 │
 │  Per-entity review queue · confirm/correct category · mark         │
 │  actionable · assign to risk-area owner · record action taken      │
 └───────────────┬───────────────────────────────┬────────────────────┘
                 ▼                               ▼
 ┌──────────────────────────────┐  ┌────────────────────────────────┐
 │   KNOWLEDGE BASE / LEARNING  │  │   DASHBOARDS & ALERTS          │
 │  Every reviewer decision     │  │  Entity 360° view · risk       │
 │  stored with embeddings →    │  │  heatmaps · trend lines ·      │
 │  retrieved as few-shot       │  │  Super-admin cross-entity      │
 │  examples · action           │  │  view · second-order linkage   │
 │  suggestions from similar    │  │  warnings                      │
 │  past items                  │  │                                │
 └──────────────────────────────┘  └────────────────────────────────┘
```

### 3.1 Ingestion layer — free sources for the prototype

| Source | Access | What it gives you |
|---|---|---|
| Google News RSS | Free, no key (`news.google.com/rss/search?q="<entity name>"`) | Per-entity query feeds across thousands of outlets — the workhorse |
| GDELT 2.0 | Free, open | Global news metadata every 15 min, with tone scores and themes |
| Direct RSS: Economic Times, Mint, Business Standard, Moneycontrol, Hindu BusinessLine | Free | Indian financial press, low latency |
| YouTube Data API | Free quota (10k units/day) | Video search per entity; titles/descriptions/comments |
| Reddit API | Free tier | Social chatter (r/IndianStockMarket, r/personalfinanceindia, city subreddits) |
| RBI press releases & enforcement actions | Free (scrape/RSS) | Ground-truth regulatory events |
| NSE/BSE corporate announcements | Free, public | Disclosures, defaults, auditor resignations — high-signal |

Notes:
- **X/Twitter API is no longer free** — defer it; Reddit + YouTube comments cover "social chatter" for the prototype.
- **Public grievances** (CPGRAMS, National Consumer Helpline, consumer forums) have no free streaming API; treat as a Phase-3 integration via data-sharing agreement rather than scraping.

### 3.2 Deduplication

One event ("Bank X fined ₹2 crore") appears in dozens of outlets. Embed each article (multilingual embedding model), cluster near-duplicates within a time window, and present **one review item per event** with all source links attached. This alone removes most of the volume problem.

### 3.3 Entity resolution

Maintain a per-entity alias table: legal name, brand names, common abbreviations, stock tickers, Hindi/regional-script names, subsidiary names. Match via dictionary + fuzzy matching first (cheap), and use the LLM only to disambiguate genuinely ambiguous hits (e.g., "Axis" the bank vs. other uses). This keeps per-item cost low.

### 3.4 LLM classification with structured output

For each deduplicated, entity-matched item, one LLM call returns a strict JSON verdict:

```json
{
  "relevant": true,
  "relevance_score": 0.87,
  "risk_areas": ["Credit Risk", "Governance Risk"],
  "severity": "high",
  "actionability": "review_recommended",
  "geography": {"state": "Maharashtra", "city": "Pune"},
  "summary": "Reports of loan evergreening at the Pune branch cluster...",
  "factor_matches": ["Sales malpractice"],
  "relationships": [{"type": "borrower_of", "name": "XYZ Infra Ltd"}]
}
```

Key design choices:

- **Prototype**: use a hosted frontier LLM (fastest path to a convincing demo; Indian-language capability is strong out of the box).
- **Production path**: design the LLM layer as a **pluggable interface**, because a banking regulator will almost certainly require sovereign/on-prem deployment — open-weight models (Llama, Mistral, or Indian models) fine-tuned on the accumulated labeled data can be swapped in without touching the rest of the system.
- **Tune for recall, not precision**, at the relevance gate: a false positive costs a reviewer 10 seconds; a false negative is a missed supervisory signal. Let the severity/actionability fields do the ranking.
- **Log everything**: prompt version, model version, input, output, and the reviewer's eventual verdict — regulators need audit trails, and this log *is* your future training set.

### 3.5 The learning loop (the practical version)

The document asks that the system "learn from the behaviour" of reviewers. The pragmatic mechanism for a prototype — far cheaper and more controllable than fine-tuning — is **retrieval-augmented classification**:

1. Every reviewer decision (category confirmed/corrected, actionable yes/no, action taken) is stored with the item's embedding.
2. When classifying a new item, retrieve the *k* most similar past-labeled items and inject them into the prompt as few-shot examples.
3. Action suggestions come from the same retrieval: "3 similar items were handled by senior reviewer A with action 'sought clarification from entity' — suggest that action."

This directly addresses the **experience-gap problem**: senior reviewers' judgments become retrievable precedents that guide juniors. Fine-tuning becomes an optimization *later*, once thousands of labels exist.

### 3.6 User-defined Factors

The document asks for user-definable factors ("customer service", "sales malpractice", …) with user-enumerated conditions. This maps beautifully onto LLMs: a Factor is a **named natural-language rule** stored in the database —

> *Factor "Sales malpractice": flag if the item alleges mis-selling of insurance or investment products by bank staff, forced bundling with loans, or unauthorized account opening.*

— and evaluated as part of the classification prompt. Team members create/edit Factors in the UI with no engineering involvement, and can test a draft Factor retroactively against the last 30 days of items before activating it.

### 3.7 Second-order linkage

Prototype it as a simple **relationship table**, not a full graph database:

- The classification step already extracts relationships mentioned in news ("XYZ Infra defaulted on its loan from Bank A").
- Store `(entity_a, relation_type, entity_b, source_item, confidence)`.
- A rules job then raises **indirect alerts**: when a negative event hits a non-regulated counterparty, warn every regulated entity linked to it ("XYZ Infra default → elevated credit-risk flag for Bank A and Bank B").
- Seed the table with known high-value links (large borrowers from public disclosures, group/promoter structures). Graduate to a graph DB (Neo4j) only if traversal depth >2 is actually needed.

### 3.8 Roles and views

| Role | Sees |
|---|---|
| SSM team member | Review queue filtered to their entity + risk area(s); their assignments |
| SSM team lead | Full entity view: all queues, 360° dashboard, Factor management, team workload |
| Super admin | Cross-entity heatmap: which entities have rising negative-signal trends, by risk area; systemic patterns (same issue at many entities); second-order alert overview |

## 4. Tech stack recommendation

| Layer | Choice | Why |
|---|---|---|
| Backend API | **Python + FastAPI** | Python owns the NLP/LLM ecosystem; FastAPI gives typed, async APIs |
| Ingestion workers | **Celery + Redis** (or APScheduler for the prototype) | Scheduled fetch jobs per source, retries, rate-limit handling |
| Database | **PostgreSQL + pgvector** | One database for relational data *and* embeddings/similarity search — no separate vector DB to operate |
| LLM | Hosted frontier model behind a **provider-agnostic interface** | Prototype speed now, sovereign swap later |
| Embeddings | Multilingual model (e.g., `multilingual-e5` class) | Hindi/regional-language items cluster with English coverage of the same event |
| Frontend | **React (Next.js)** | Standard, hireable, good dashboard ecosystem |
| Auth/RBAC | Keycloak or simple JWT roles for prototype | Regulator-grade SSO can come later |
| Deployment | **Docker Compose** for prototype | Single-command demo; maps cleanly to K8s later |

## 5. Phased prototype plan

**Phase 0 — Foundation (weeks 1–2)**
- Finalize the taxonomy with the team: risk areas, severity levels, actionability states, action types.
- Pick **5 pilot entities** (mix: 2 large banks, 1 NBFC, 1 UCB, 1 payment operator) and build their alias tables.
- Repo skeleton, database schema, Docker Compose.

**Phase 1 — Demoable core (weeks 3–6)**
- Ingestion: Google News RSS + 3–4 press RSS feeds for the 5 entities.
- Dedup + entity matching + LLM classification.
- Review queue UI: list, item detail with sources, confirm/correct category, mark actionable.
- Basic per-entity dashboard (volume, risk-area breakdown, severity trend).
- **This alone demonstrates the core value proposition.**

**Phase 2 — Learning & customization (weeks 7–10)**
- Feedback loop: labeled items feed retrieval-based few-shot classification; measure accuracy uplift.
- Action suggestions from similar past items.
- User-defined Factors with retroactive testing.
- Geographic tagging + map view. YouTube/Reddit ingestion.
- Super-admin cross-entity dashboard.

**Phase 3 — Intelligence (weeks 11+)**
- Relationship extraction + second-order alerts.
- Evaluation harness: precision/recall of relevance and category vs. reviewer labels, reported per model/prompt version.
- Multilingual expansion (Hindi + 2 regional languages).
- Grievance-data integration groundwork (data-sharing, not scraping).

## 6. Key risks and considerations

1. **Data sovereignty**: a banking regulator sending news text to a foreign-hosted LLM is fine for a prototype on *public* data, but production will need on-prem/India-region inference. The pluggable LLM layer is the insurance policy — build it that way from day one.
2. **Precision/recall tuning**: bias the relevance gate toward recall; use ranking (severity × relevance) to keep reviewer queues manageable. Track "reviewer marked irrelevant" rate as the health metric.
3. **Auditability**: every automated decision must be reconstructable (inputs, prompt version, model version, output). This is both a regulatory expectation and your training data.
4. **Multilingual reality**: a large share of grievances and local news about UCBs/RCBs will be in Hindi and regional languages — multilingual embeddings and LLM classification handle this, but test it explicitly in Phase 1 with a handful of Hindi items.
5. **Scraping legality/ToS**: stick to RSS, official APIs, and public regulatory data for the prototype; avoid scraping consumer-complaint platforms.
6. **Hallucination guardrails**: the LLM only ever *summarizes and classifies text it is given* — never generates claims. Summaries link to sources; reviewers see originals one click away.
7. **Cost control**: dedup before classification (one LLM call per *event*, not per article); dictionary-match before LLM disambiguation; batch where possible. For 5 entities, expect low hundreds of classification calls/day — trivially cheap.

## 7. Suggested repository structure

```
suchak/
├── backend/
│   ├── app/            # FastAPI: routes, auth, RBAC
│   ├── ingestion/      # per-source fetchers (rss, gdelt, youtube, reddit)
│   ├── pipeline/       # dedup, entity resolution, classification
│   ├── learning/       # feedback store, retrieval, suggestion engine
│   └── models/         # SQLAlchemy schema
├── frontend/           # Next.js app: queue, dashboards, admin
├── infra/              # docker-compose, migrations
└── docs/               # this proposal, taxonomy, API docs
```
