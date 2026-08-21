"""Classification of ingested items.

Primary path: Claude with a structured-output JSON schema. The prompt
carries the taxonomy, the entity's context, active user-defined Factors,
and few-shot examples retrieved from human-reviewed items — this is how
the system "learns" from reviewer behaviour without any fine-tuning.

Fallback path: a keyword heuristic, so the pipeline runs end-to-end with
no API key (demo mode) or when the API is unreachable. Every verdict
records which classifier and model produced it, for auditability.
"""
import json
import logging
import os
from datetime import datetime, timezone

from . import taxonomy
from .db import get_setting, one, q, x
from .matching import Registry
from .similarity import rank_similar

log = logging.getLogger("suchak.classify")

# Two models by design: a cheap one screens every fetched item for
# relevance and negative-list matches, and a stronger one writes the full
# verdict only for what survives. Override either with the env vars.
MODEL = os.environ.get("SUCHAK_MODEL", "claude-sonnet-5")

# Severity criteria live in the settings table so the team can tune them in
# the admin UI without touching code; this is the default until edited.
# NOTE: the keyword fallback classifier has its own hardcoded trigger list
# and does not read this text.
# The negative list: item types the team does NOT analyse. Edited by the
# super admin in the UI; matching items are parked under "Filtered out"
# with the reason recorded, never classified or queued.
EXCLUSION_RULES_KEY = "exclusion_rules"
DEFAULT_EXCLUSION_RULES = (
    "Stock recommendations and share-price commentary: buy/sell/hold calls, "
    "brokerage target prices, 'stocks to pick' listicles, technical-analysis "
    "trade ideas, and routine share-price movement reports that describe no "
    "underlying event at the institution."
)

SEVERITY_DEFS_KEY = "severity_definitions"
DEFAULT_SEVERITY_DEFS = (
    "high = potential supervisory concern needing prompt attention "
    "(fraud, default, run on deposits, regulatory breach, cyber incident); "
    "medium = notable negative development worth review; "
    "low = routine or positive coverage"
)
# A small, cheap model screens each item for relevance before the full
# classification runs, so noise costs a fraction of a full verdict.
# Set SUCHAK_GATE_MODEL="" to disable the screen.
GATE_MODEL = os.environ.get("SUCHAK_GATE_MODEL", "claude-haiku-4-5")

GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "about_entity": {"type": "boolean"},
        "excluded": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["about_entity", "excluded", "reason"],
    "additionalProperties": False,
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "relevance_score": {"type": "number"},
        "risk_areas": {
            "type": "array",
            "items": {"type": "string", "enum": taxonomy.RISK_AREAS},
        },
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "actionability": {
            "type": "string",
            "enum": ["action_recommended", "review_recommended", "monitor"],
        },
        "geography": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "factor_matches": {"type": "array", "items": {"type": "string"}},
        "complaint_topics": {
            "type": "array",
            "items": {"type": "string", "enum": taxonomy.COMPLAINT_TOPICS},
        },
        "excluded": {"type": "boolean"},
        "exclusion_reason": {"type": ["string", "null"]},
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["type", "name"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "relevant", "relevance_score", "risk_areas", "severity",
        "actionability", "geography", "summary", "factor_matches",
        "complaint_topics", "relationships", "excluded", "exclusion_reason",
    ],
    "additionalProperties": False,
}

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _gate(entity, title: str, source: str | None,
          exclusion_rules: str = DEFAULT_EXCLUSION_RULES) -> tuple[bool, bool, str]:
    """Cheap screen with two jobs in one call: is the item actually about
    this entity, and is it a type on the team's negative list?

    Guards against near-miss names (a story about State Bank of India
    surfacing in Bank of India's feed) and screens out excluded item types
    (stock tips and the like) before the expensive verdict runs. Returns
    (about_entity, excluded, reason).
    """
    client = _get_client()
    aliases = ", ".join(json.loads(entity["aliases"]))
    resp = client.messages.create(
        model=GATE_MODEL,
        max_tokens=256,
        system=(
            "You screen Indian banking news for a supervisory team. Two checks.\n"
            "1. about_entity: is the headline about one specific regulated entity?\n"
            f"Entity: {entity['name']} ({entity['kind']}). Known as: {aliases}.\n"
            "Answer false when the headline is about a DIFFERENT institution "
            "whose name merely contains or resembles this one (for example "
            "'State Bank of India' is not 'Bank of India', and 'Union Bank of "
            "India' is not 'City Union Bank'), when the words appear only as a "
            "generic phrase rather than this institution's name, or when the "
            "entity is not a subject of the story at all. Answer true when the "
            "story concerns this institution, including routine coverage.\n"
            "2. excluded: the team keeps a negative list of item types it does "
            "NOT analyse. Set excluded=true when the item is of such a type, "
            "even though it mentions the entity. The negative list:\n"
            f"{exclusion_rules}\n"
            "Genuine company events (fraud, penalties, defaults, outages, "
            "results, management changes) are never excluded merely because "
            "the share price is also mentioned."
        ),
        messages=[{"role": "user",
                   "content": f"Headline: {title}\nSource: {source or 'unknown'}"}],
        output_config={"format": {"type": "json_schema", "schema": GATE_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        return True, False, "screen refused; passed through"
    verdict = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return (bool(verdict.get("about_entity")), bool(verdict.get("excluded")),
            (verdict.get("reason") or "")[:300])


def active_factors(db, entity_id: int) -> list:
    return q(
        db,
        "SELECT * FROM factors WHERE active = 1 AND (entity_id IS NULL OR entity_id = ?)"
        " ORDER BY entity_id IS NULL DESC, name",
        (entity_id,),
    )


def similar_reviewed(db, entity_id: int, text: str, top_k: int = 3) -> list:
    """Reviewed items most similar to `text` — same entity first, then any
    entity — used both as few-shot examples and for action suggestions."""
    rows = q(
        db,
        "SELECT id, entity_id, title, summary, review_relevant, review_risk_areas,"
        "       review_severity, review_actionable, review_action, review_notes"
        " FROM items WHERE status IN ('reviewed','dismissed') AND reviewed_at IS NOT NULL"
        " ORDER BY (entity_id = ?) DESC, reviewed_at DESC LIMIT 400",
        (entity_id,),
    )
    if not rows:
        return []
    by_id = {r["id"]: r for r in rows}
    candidates = [(r["id"], f"{r['title']} {r['summary'] or ''}") for r in rows]
    ranked = rank_similar(text, candidates, top_k=top_k)
    return [(by_id[key], score) for key, score in ranked]


def suggest_action(similar: list) -> str | None:
    """Most common recorded action among similar actionable reviewed items."""
    actions = [
        r["review_action"] for r, _ in similar
        if r["review_actionable"] and r["review_action"]
    ]
    if not actions:
        return None
    return max(set(actions), key=actions.count)


def _build_system(entity, factors, examples,
                  severity_defs: str = DEFAULT_SEVERITY_DEFS,
                  exclusion_rules: str = DEFAULT_EXCLUSION_RULES) -> str:
    lines = [
        "You are a supervisory triage assistant for the Banking Supervisor of India.",
        "You classify public news items about a regulated entity so a small "
        "supervision team can focus its review. You only summarize and classify "
        "the text you are given — never invent facts beyond it.",
        "",
        f"Regulated entity under supervision: {entity['name']} ({entity['kind']}).",
        f"Known aliases: {', '.join(json.loads(entity['aliases']))}.",
        "",
        "Risk areas (choose zero or more that genuinely apply): "
        + "; ".join(taxonomy.RISK_AREAS) + ".",
        f"Severity: {severity_defs}.",
        "Actionability: action_recommended = the team likely must act; "
        "review_recommended = a person should read this soon; monitor = ambient awareness only.",
        "Relevance: the item must actually concern this entity (not merely a "
        "similarly named organization). relevance_score is 0 to 1.",
        "Geography: the Indian state/city the item concerns, if identifiable, else null.",
        "Summary: one factual sentence based only on the given text.",
        "Relationships: organizations linked to the entity in the text, with the link type "
        "(borrower_of, promoter_of, subsidiary_of, partner_of, auditor_of, vendor_of, other).",
        "Complaint topics: when the item reports customer grievances -- complaints by "
        "customers of the entity, whether covered in news or posted directly on social "
        "media -- list every matching topic from: "
        + "; ".join(taxonomy.COMPLAINT_TOPICS) + ". "
        "Leave the list empty when the item is not about customer grievances.",
        "Negative list -- item types the team does NOT analyse: "
        + exclusion_rules + " "
        "Set excluded=true with a short exclusion_reason when the item is of "
        "such a type; otherwise excluded=false and exclusion_reason=null. "
        "Genuine company events are never excluded merely because the share "
        "price is also mentioned.",
    ]
    if factors:
        lines += ["", "User-defined Factors — list each factor whose conditions the item meets "
                      "in factor_matches (use the factor name exactly):"]
        for f in factors:
            lines.append(f"- {f['name']}: {f['conditions']}")
    if examples:
        lines += ["", "Precedents — similar items this team already reviewed "
                      "(follow their judgment where applicable):"]
        for r, _score in examples:
            label = "relevant" if r["review_relevant"] else "not relevant"
            areas = ", ".join(json.loads(r["review_risk_areas"] or "[]")) or "none"
            act = "actionable" if r["review_actionable"] else "not actionable"
            action = f"; action taken: {r['review_action']}" if r["review_action"] else ""
            sev = ""
            try:
                if r["review_severity"]:
                    sev = f"; severity: {r['review_severity']}"
            except (KeyError, IndexError):
                pass
            lines.append(f'- "{r["title"]}" -> {label}; risk areas: {areas}; {act}{sev}{action}')
    return "\n".join(lines)


def _llm_classify(entity, factors, examples, title, snippet, source, published,
                  severity_defs: str = DEFAULT_SEVERITY_DEFS,
                  exclusion_rules: str = DEFAULT_EXCLUSION_RULES):
    client = _get_client()
    user_msg = (
        f"Classify this item.\n"
        f"Title: {title}\n"
        f"Source: {source or 'unknown'}\n"
        f"Published: {published or 'unknown'}\n"
        f"Snippet: {snippet or '(none)'}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=_build_system(entity, factors, examples, severity_defs,
                             exclusion_rules),
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused classification")
    text = next(b.text for b in resp.content if b.type == "text")
    verdict = json.loads(text)
    return verdict, "llm", MODEL


# --- keyword fallback -------------------------------------------------------

_HEURISTIC_KEYWORDS = {
    "Credit Risk": [
        "default", "npa", "bad loan", "write-off", "writeoff", "provisioning",
        "restructur", "insolvency", "bankruptc", "loan fraud", "evergreen",
        "recovery suit", "wilful defaulter",
    ],
    "Market Risk": [
        "treasury loss", "forex", "derivative", "mark-to-market", "bond loss",
        "trading loss", "investment loss",
    ],
    "Liquidity Risk": [
        "withdrawal", "deposit run", "bank run", "liquidity crunch",
        "cash crunch", "redemption pressure", "withdrawal cap", "unable to pay depositors",
    ],
    "Operational Risk": [
        "outage", "downtime", "system failure", "technical glitch", "server down",
        "upi fail", "embezzle", "theft", "robbery", "internal fraud", "atm fraud",
    ],
    "Governance Risk": [
        "resign", "board dispute", "auditor", "audit qualification", "promoter",
        "related party", "penalty", "show cause", "violation", "money laundering",
        "kyc lapse", "licence cancel", "license cancel", "irregularit",
    ],
    "Cybersecurity Risk": [
        "hack", "breach", "ransomware", "phishing", "data leak", "malware",
        "ddos", "cyber attack", "cyberattack",
    ],
    "Conduct & Consumer Protection": [
        "mis-sell", "misselling", "mis-sold", "complaint", "grievance",
        "harass", "recovery agent", "overcharg", "hidden charge", "unauthorised account",
        "unauthorized account",
    ],
}

_COMPLAINT_TOPIC_KEYWORDS = {
    "Mis-selling": ["mis-sell", "misselling", "mis-sold", "forced insurance",
                    "forced bundling", "bundled with", "policies bundled"],
    "Recovery practices": ["recovery agent", "coercive recovery", "recovery practices"],
    "Service disruption": ["outage", "downtime", "server down", "technical glitch",
                           "upi fail", "failed transactions", "services down",
                           "branch shut", "not working"],
    "Unauthorized transactions": ["unauthorized transaction", "unauthorised transaction",
                                  "money deducted", "account debited without",
                                  "fraudulent debit"],
    "Charges & fees": ["hidden charge", "overcharg", "excess charge", "wrongful charge"],
    "Harassment": ["harass"],
    "Account access / KYC": ["account frozen", "account blocked", "account locked",
                             "kyc pending", "kyc rejected"],
}

# Fixed patterns for the default negative-list rule; the fallback cannot
# evaluate free-text rules, so only obvious stock-tip phrasing is caught.
_STOCK_RECO_KEYWORDS = [
    "target price", "price target", "buy rating", "sell rating", "hold rating",
    "buy call", "sell call", "stocks to buy", "stocks to pick", "top picks",
    "top stock", "brokerage", "technical picks", "intraday picks", "upside of",
    "stock recommendation", "share price target",
]

_HIGH_SEVERITY = [
    "fraud", "scam", "default", "penalty", "hack", "breach", "bank run",
    "deposit run", "insolvency", "arrest", "raid", "licence cancel",
    "license cancel", "money laundering", "ransomware",
]


def _heuristic_classify(entity, title, snippet, registry=None):
    text = f"{title} {snippet or ''}".lower()
    registry = registry or Registry([entity])
    eid = entity["id"]
    in_title = registry.mentions(eid, title)
    in_text = in_title or registry.mentions(eid, f"{title} {snippet or ''}")

    risk_areas = [
        area for area, words in _HEURISTIC_KEYWORDS.items()
        if any(w in text for w in words)
    ]
    high = any(w in text for w in _HIGH_SEVERITY)
    severity = "high" if high else ("medium" if risk_areas else "low")
    complaint_topics = [
        topic for topic, words in _COMPLAINT_TOPIC_KEYWORDS.items()
        if any(w in text for w in words)
    ]
    verdict = {
        "relevant": in_text,
        "relevance_score": 0.9 if in_title else (0.6 if in_text else 0.2),
        "risk_areas": risk_areas,
        "severity": severity,
        "actionability": "review_recommended" if high else ("monitor" if risk_areas else "monitor"),
        "geography": None,
        "summary": (snippet or title)[:280],
        "factor_matches": [],
        "complaint_topics": complaint_topics,
        "relationships": [],
    }
    if any(k in text for k in _STOCK_RECO_KEYWORDS):
        verdict["excluded"] = True
        verdict["exclusion_reason"] = "stock recommendation (keyword match)"
    return verdict, "heuristic", "keyword-rules"


def _record_gated_out(db, item, reason: str, classifier: str = "gate",
                      model: str | None = None) -> None:
    """Park a screened-out item: kept for audit under the Filtered-out tab,
    out of the review queue, never counted on dashboards."""
    x(
        db,
        "UPDATE items SET status='classified', gated_out=1, gate_reason=?,"
        " relevance=0, risk_areas='[]', severity='low', actionability='monitor',"
        " summary=?, classifier=?, model=?, classified_at=? WHERE id=?",
        (reason, item["title"], classifier, model or GATE_MODEL,
         datetime.now(timezone.utc).isoformat(timespec="seconds"), item["id"]),
    )


def classify_item(db, item) -> None:
    """Classify one item row and persist the verdict."""
    entity = one(db, "SELECT * FROM entities WHERE id = ?", (item["entity_id"],))

    try:
        source_type = item["source_type"] or "news"
    except (KeyError, IndexError):
        source_type = "news"
    # Official sources (RBI releases, exchange filings) are routed to an
    # entity deterministically by the registry -- asking a model whether the
    # filing is "about" the bank would only cost money and add a failure
    # mode. The screen exists for noisy feeds, so it runs only on those.
    exclusion_rules = get_setting(db, EXCLUSION_RULES_KEY, DEFAULT_EXCLUSION_RULES)
    if GATE_MODEL and source_type not in ("regulatory", "filing"):
        try:
            keep, excluded, reason = _gate(entity, item["title"],
                                           item["source_name"], exclusion_rules)
            if not keep:
                log.info("Gated out %r for %s: %s",
                         item["title"][:60], entity["name"], reason)
                _record_gated_out(db, item, reason)
                return
            if excluded:
                log.info("Negative-list exclusion %r for %s: %s",
                         item["title"][:60], entity["name"], reason)
                _record_gated_out(db, item, f"negative list: {reason}")
                return
        except Exception as exc:
            # never let the screen block the pipeline; fall through to
            # full classification, which makes its own relevance judgment
            log.warning("Relevance screen unavailable (%s: %s)",
                        type(exc).__name__, exc)

    factors = active_factors(db, item["entity_id"])
    examples = similar_reviewed(db, item["entity_id"], f"{item['title']} {item['snippet'] or ''}")
    severity_defs = get_setting(db, SEVERITY_DEFS_KEY, DEFAULT_SEVERITY_DEFS)
    try:
        verdict, classifier, model = _llm_classify(
            entity, factors, examples,
            item["title"], item["snippet"], item["source_name"], item["published_at"],
            severity_defs, exclusion_rules,
        )
    except Exception as exc:  # missing key, network, rate limit, refusal, bad JSON
        log.warning("LLM classification failed (%s: %s); using heuristic", type(exc).__name__, exc)
        verdict, classifier, model = _heuristic_classify(
            entity, item["title"], item["snippet"])

    # backstop for when the cheap screen is disabled or was unavailable:
    # the full verdict also evaluates the negative list
    if verdict.get("excluded"):
        reason = verdict.get("exclusion_reason") or "matches the negative list"
        log.info("Negative-list exclusion %r for %s: %s",
                 item["title"][:60], entity["name"], reason)
        _record_gated_out(db, item, f"negative list: {reason}",
                          classifier=classifier, model=model)
        return

    x(
        db,
        "UPDATE items SET status='classified', relevance=?, risk_areas=?, severity=?,"
        " actionability=?, geography=?, summary=?, factor_matches=?, complaint_topics=?,"
        " relationships=?, classifier=?, model=?, classified_at=? WHERE id=?",
        (
            float(verdict.get("relevance_score") or 0),
            json.dumps(verdict.get("risk_areas") or []),
            verdict.get("severity") or "low",
            verdict.get("actionability") or "monitor",
            verdict.get("geography"),
            verdict.get("summary") or item["title"],
            json.dumps(verdict.get("factor_matches") or []),
            json.dumps(verdict.get("complaint_topics") or []),
            json.dumps(verdict.get("relationships") or []),
            classifier,
            model,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            item["id"],
        ),
    )


def classify_new_items(db, limit: int = 100) -> int:
    rows = q(db, "SELECT * FROM items WHERE status = 'new' ORDER BY id LIMIT ?", (limit,))
    for row in rows:
        classify_item(db, row)
    return len(rows)
