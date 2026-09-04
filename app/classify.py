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
from datetime import datetime, timedelta, timezone

from . import taxonomy
from .db import get_setting, one, q, x
from .matching import Registry
from .similarity import alias_tokens, rank_similar

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
# How many of the reviewer's own set-aside rulings are shown to the
# classifier as examples of what this team does not want to see again.
SET_ASIDE_EXAMPLES = max(0, min(
    int(os.environ.get("SUCHAK_SET_ASIDE_EXAMPLES", "12")), 40))
# Same-event clustering. Outlets share words while they report the fact
# ("CEO resigns") and stop sharing them once they move to its angles
# ("shares dip after top boss exit", "what the succession means"), so
# word overlap -- the fetch-time rule -- cannot club a story's second
# day. The classifier reads every item anyway, so it is shown the stories
# already on the queue and asked which one, if any, this item continues.
# How far back those stories are drawn from, and how many are shown.
EVENT_WINDOW_DAYS = max(1, int(os.environ.get("SUCHAK_EVENT_WINDOW_DAYS", "14")))
EVENT_CANDIDATES = max(0, min(
    int(os.environ.get("SUCHAK_EVENT_CANDIDATES", "50")), 120))
# Never folded into a story, and never offered as one: social posts (ten
# complaints are ten data points) and exchange filings (formulaic titles).
# The same pair as ingest.NO_MERGE_TYPES, kept here because ingest imports
# this module.
NO_FOLD_TYPES = {"social", "filing"}
DEFAULT_EXCLUSION_RULES = (
    "Stock recommendations and share-price commentary: buy/sell/hold calls, "
    "brokerage target prices, 'stocks to pick' listicles, technical-analysis "
    "trade ideas, and routine share-price movement reports that describe no "
    "underlying event at the institution."
)

# Risk-area definitions the classifier applies. The default is drawn from
# the three RBI guidance notes the supervisor anchored this taxonomy to --
# Credit Risk Management (Oct 2002), Market Risk Management (Oct 2002,
# which treats liquidity as integral to market risk management), and
# Operational Risk Management and Operational Resilience (Apr 2024). The
# text is editable on the Factors page, so the team can refine it against
# the documents themselves; the classifier follows whatever is stored.
RISK_DEFS_KEY = "risk_definitions"
DEFAULT_RISK_DEFS = (
    "Credit Risk (per RBI Guidance Note on Credit Risk Management, 2002): "
    "possibility of losses associated with diminution in the credit quality "
    "of borrowers or counterparties -- defaults and NPAs, slippages and "
    "restructuring, wilful default, weak recovery and write-offs, "
    "concentration in a borrower, group or sector, counterparty settlement "
    "failure, invoked guarantees. This is about the bank's asset quality; "
    "a borrower's complaint about loan service is Conduct, not Credit. "
    "Market Risk (per RBI Guidance Note on Market Risk Management, 2002): "
    "possibility of loss from adverse movement of market prices -- interest "
    "rates, foreign exchange, equity and commodity prices -- on trading and "
    "investment portfolios: treasury and derivative losses, mark-to-market "
    "hits, interest-rate risk in the banking book. "
    "Liquidity Risk (treated by the same Market Risk guidance note as "
    "integral to it): inability to meet obligations as they fall due or to "
    "fund asset growth without unacceptable cost -- deposit runs and "
    "withdrawal stress, withdrawal caps, redemption pressure, loss of "
    "funding-market access, severe asset-liability mismatch. "
    "Operational Risk (per RBI Guidance Note on Operational Risk Management "
    "and Operational Resilience, 2024): risk of loss from inadequate or "
    "failed internal processes, people and systems, or from external "
    "events; includes legal risk; excludes strategic and reputational "
    "risk. Internal and external fraud in operations, process and payment "
    "failures, system outages and service disruption, third-party and "
    "outsourcing failures, business-continuity events. "
    "Cybersecurity Risk (ICT and cyber, treated under operational "
    "resilience by the 2024 note but tracked separately here): cyber "
    "attacks and data breaches, digital-channel and card/ATM compromise, "
    "phishing and OTP fraud, ransomware. Use this, not Operational, when "
    "the vector is cyber. "
    "Governance Risk (supervisory judgement; outside the three notes): "
    "board and senior-management failures, auditor concerns or exits, "
    "related-party dealings, disclosure lapses, strictures on management. "
    "Conduct & Consumer Protection (supervisory judgement; outside the "
    "three notes): mis-selling, unfair or hidden charges, harassment in "
    "recovery, grievance-handling failure, wrongful freezes or denials of "
    "service to customers. "
    "An item may genuinely engage several areas -- a cyber fraud the bank "
    "then mishandled with the customer is Cybersecurity and Conduct; "
    "classify by the nature of each deficiency the text shows."
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
        "like_set_aside": {"type": ["boolean", "null"]},
        "same_event_as": {"type": ["integer", "null"]},
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
        "like_set_aside", "same_event_as",
    ],
    "additionalProperties": False,
}

# One call per batch of stored items: which of them report one occurrence.
# Used by scripts/re_dedup.py --smart to tidy items classified before the
# classifier learned to fold stories as they arrive.
GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["label", "item_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}

# What "the same event" means, in one place, so the fold-as-you-classify
# path and the batch re-cluster path cannot drift apart.
SAME_EVENT_RULE = (
    "The same occurrence means the same resignation, the same penalty, the "
    "same outage, the same fraud case, the same results announcement. "
    "Reaction pieces, share-price and market fallout, analysis, opinion, "
    "explainers, statements and follow-up reporting on one occurrence all "
    "belong to it, however different the angle or the wording, and whatever "
    "language they are written in. A different occurrence of the same kind "
    "is NOT the same event: another penalty, a later quarter's results, a "
    "different person leaving, a separate outage on another day. A new "
    "development that would stand as its own headline -- a successor "
    "appointed, a case filed over the fraud, the regulator's order after the "
    "incident -- is also its own event."
)

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
            "You screen Indian banking news for a supervisory team. Headlines "
            "may be in English or in an Indian language such as Marathi or "
            "Hindi; judge them the same way either way. Two checks.\n"
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
    # The entity's own name is in every headline about it, so it says
    # nothing about whether two stories are alike; score on the rest.
    ent = one(db, "SELECT aliases FROM entities WHERE id = ?", (entity_id,))
    stop = alias_tokens(json.loads(ent["aliases"])) if ent else set()
    by_id = {r["id"]: r for r in rows}
    candidates = [(r["id"], f"{r['title']} {r['summary'] or ''}") for r in rows]
    ranked = rank_similar(text, candidates, top_k=top_k, exclude=stop)
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
                  exclusion_rules: str = DEFAULT_EXCLUSION_RULES,
                  risk_defs: str = DEFAULT_RISK_DEFS,
                  set_aside: str = "", events: str = "") -> str:
    lines = [
        "You are a supervisory triage assistant for the Banking Supervisor of India.",
        "You classify public news items about a regulated entity so a small "
        "supervision team can focus its review. You only summarize and classify "
        "the text you are given — never invent facts beyond it.",
        "",
        f"Regulated entity under supervision: {entity['name']} ({entity['kind']}).",
        f"Known aliases: {', '.join(json.loads(entity['aliases']))}.",
        "",
        # Regional entities are covered in the regional press, so items can
        # arrive in Marathi, Hindi or another Indian language. The reviewer
        # queue has to stay scannable by one team, so the verdict is always
        # written in English whatever the source language.
        "Items may be in English or in an Indian language such as Marathi or "
        "Hindi. Read the item in whatever language it is written, but always "
        "write the summary and every other text field in English. Do not "
        "translate the entity's name; leave proper nouns as they are.",
        "",
        "Risk areas -- choose zero or more that genuinely apply, guided by "
        "these definitions, which follow the RBI guidance notes the "
        "supervisory team works to: " + risk_defs,
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
        (set_aside + "\n" if set_aside else "") +
        "Negative list -- item types the team does NOT analyse: "
        + exclusion_rules + " "
        "Set excluded=true with a short exclusion_reason when the item is of "
        "such a type; otherwise excluded=false and exclusion_reason=null. "
        "Genuine company events are never excluded merely because the share "
        "price is also mentioned.",
        "like_set_aside and same_event_as: null unless an instruction below "
        "asks you to set them.",
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
    if events:
        lines.append(events)
    return "\n".join(lines)


def set_aside_examples(db, entity_id: int) -> list[dict]:
    """What this team has ruled useless for pattern-finding, newest first.

    The team's own rulings, not a global list: one supervisor's "generic"
    is another's signal, and the examples are only ever shown to the
    entity they were made on."""
    rows = q(db,
             "SELECT title, snippet, set_aside FROM items"
             " WHERE entity_id = ? AND source_type = 'social'"
             " AND set_aside IS NOT NULL"
             " ORDER BY id DESC LIMIT ?",
             (entity_id, SET_ASIDE_EXAMPLES))
    return [dict(r) for r in rows]


def _render_set_aside(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["", "This team has set these social posts aside as no use for "
             "spotting supervisory patterns. Judge whether the item you are "
             "given is of the same character:"]
    for r in rows:
        text = " ".join(((r.get("snippet") or r.get("title") or "").split()))[:180]
        lines.append(f'- [{r.get("set_aside")}] "{text}"')
    lines.append(
        "Set like_set_aside=true ONLY when the item is plainly the same kind "
        "of thing: a generic gripe naming no product or process, pure "
        "venting, or a post that is not about the bank's service. A post "
        "that names a specific product, transaction, charge, branch or "
        "process failure is NOT set-aside material, however angrily it is "
        "written. When in doubt, set false -- a missed complaint costs the "
        "team far more than one extra post to read.")
    return "\n".join(lines)


def recent_events(db, entity_id: int, exclude_id: int | None = None,
                  near: str | None = None) -> list[dict]:
    """Stories already on the queue that a new item might continue: this
    entity's own items fetched in the last EVENT_WINDOW_DAYS -- primaries
    only (classified, not screened out, not social or filings), returned
    newest first.

    When there are more than EVENT_CANDIDATES, the ones published closest
    to `near` (the item's own date) are kept: coverage of one occurrence
    clusters in time, so a 90-day backfill of a hundred items still shows
    each item the stories of its own week rather than the last fifty
    stored."""
    if not EVENT_CANDIDATES:
        return []
    # created_at is SQLite's datetime('now') text, so compare in that shape
    since = (datetime.now(timezone.utc)
             - timedelta(days=EVENT_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    near = near or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = q(db,
             "SELECT id, title, summary, published_at FROM items"
             " WHERE entity_id = ? AND gated_out = 0 AND status != 'new'"
             "   AND source_type NOT IN ('social','filing')"
             "   AND created_at >= ? AND id != ?"
             " ORDER BY COALESCE(ABS(julianday(COALESCE(published_at, created_at))"
             "                       - julianday(?)), 1e9), id DESC"
             " LIMIT ?",
             (entity_id, since, exclude_id or 0, near, EVENT_CANDIDATES))
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (r.get("published_at") or "", r["id"]), reverse=True)
    return out


def _event_line(r: dict) -> str:
    title = " ".join((r.get("title") or "").split())
    summary = " ".join((r.get("summary") or "").split())[:160]
    date = (r.get("published_at") or "")[:10]
    line = f'- [{r["id"]}]' + (f" {date}" if date else "") + f' "{title}"'
    if summary and summary.lower() != title.lower():
        line += f" -- {summary}"
    return line


def _render_events(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["", "Stories already on this team's queue for this entity, "
             "newest first, each with its number:"]
    lines += [_event_line(r) for r in rows]
    lines.append(
        "If the item you are given reports the SAME occurrence as one of "
        "these stories, set same_event_as to that story's number. "
        + SAME_EVENT_RULE +
        " When the item continues none of them, or you are unsure, set "
        "same_event_as to null.")
    return "\n".join(lines)


def group_same_events(entity, rows: list[dict]) -> list[dict]:
    """Which of these stored items report one occurrence? One model call
    per batch. Returns [{"label", "item_ids"}] holding only groups of two
    or more, ids limited to those offered, each id in one group at most.
    Raises when the model cannot be reached -- the caller says so, rather
    than reporting a quiet 'nothing to merge'."""
    if len(rows) < 2:
        return []
    client = _get_client()
    listing = "\n".join(_event_line(r) for r in rows)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=(
            "You help a banking supervision team tidy its review queue. You "
            "are given stored news items about one regulated entity -- "
            f"{entity['name']} ({entity['kind']}) -- each with its number, "
            "date, headline and one-line summary. Items may be in English or "
            "an Indian language; judge them the same way. Group the items "
            "that report the SAME occurrence. " + SAME_EVENT_RULE + " "
            "Return only groups of two or more items and leave out items "
            "that stand alone. Give each group a short label naming the "
            "occurrence. When unsure whether two items are the same "
            "occurrence, keep them apart."
        ),
        messages=[{"role": "user", "content": f"Items:\n{listing}"}],
        output_config={"format": {"type": "json_schema", "schema": GROUP_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused to group the items")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    offered = {r["id"] for r in rows}
    seen: set[int] = set()
    groups = []
    for g in data.get("groups") or []:
        ids = []
        for i in g.get("item_ids") or []:
            if isinstance(i, bool) or not isinstance(i, int):
                continue
            if i in offered and i not in seen:
                ids.append(i)
                seen.add(i)
        if len(ids) >= 2:
            groups.append({"label": (g.get("label") or "")[:120], "item_ids": ids})
    return groups


def _llm_classify(entity, factors, examples, title, snippet, source, published,
                  severity_defs: str = DEFAULT_SEVERITY_DEFS,
                  exclusion_rules: str = DEFAULT_EXCLUSION_RULES,
                  risk_defs: str = DEFAULT_RISK_DEFS,
                  set_aside: str = "", events: str = ""):
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
                             exclusion_rules, risk_defs, set_aside, events),
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


def _fold_into(db, item, primary_id: int) -> bool:
    """This item is another outlet's take on a story already on the queue:
    it becomes a source on that story and its own row goes. Only ever
    called for a row being classified for the first time, so no review,
    ruling or to-do is lost; sources the fetch already attached to it
    move across with it. A trusted outlet's article becomes the story's
    face -- unless a person has already reviewed the story, whose face
    must not change under them."""
    primary = one(db, "SELECT id, entity_id, title, url, source_name,"
                      " source_tier, published_at, reviewed_at, status"
                      " FROM items WHERE id = ?", (primary_id,))
    if not primary or primary["entity_id"] != item["entity_id"]:
        return False
    try:
        item_tier = item["source_tier"] or ""
    except (KeyError, IndexError):
        item_tier = ""
    promote = (item_tier in ("trusted", "official")
               and primary["source_tier"] not in ("trusted", "official")
               and not primary["reviewed_at"]
               and primary["status"] in ("new", "classified"))
    log.info("Folded %r into story #%s %r%s", item["title"][:60],
             primary_id, primary["title"][:60],
             " (trusted source becomes the face)" if promote else "")
    with db:
        # Order matters: the folded row must be gone before the primary
        # may take over its URL -- items are unique per (entity, url).
        db.execute("UPDATE item_sources SET item_id = ? WHERE item_id = ?",
                   (primary_id, item["id"]))
        db.execute("DELETE FROM items WHERE id = ?", (item["id"],))
        if promote:
            db.execute(
                "INSERT INTO item_sources (item_id, url, source_name, title,"
                " published_at, source_tier) VALUES (?,?,?,?,?,?)",
                (primary_id, primary["url"], primary["source_name"],
                 primary["title"], primary["published_at"],
                 primary["source_tier"]))
            db.execute(
                "UPDATE items SET title=?, url=?, source_name=?,"
                " source_tier=?, snippet=?, published_at=? WHERE id=?",
                (item["title"], item["url"], item["source_name"], item_tier,
                 item["snippet"], item["published_at"], primary_id))
        else:
            db.execute(
                "INSERT INTO item_sources (item_id, url, source_name, title,"
                " published_at, source_tier) VALUES (?,?,?,?,?,?)",
                (primary_id, item["url"], item["source_name"], item["title"],
                 item["published_at"], item_tier))
    return True


def classify_item(db, item) -> str:
    """Classify one item row and persist the verdict. Returns what became
    of the item: 'classified', 'gated' (screened out, kept for audit) or
    'folded' (merged as a source into a story already on the queue)."""
    entity = one(db, "SELECT * FROM entities WHERE id = ?", (item["entity_id"],))

    try:
        source_type = item["source_type"] or "news"
    except (KeyError, IndexError):
        source_type = "news"
    try:
        human_ruled = item["attribution"] == "human"
    except (KeyError, IndexError):
        human_ruled = False
    # Sources whose attribution comes from the request rather than from the
    # text skip the screen. RBI releases and exchange filings are routed to
    # an entity deterministically by the registry. Social posts are collected
    # by searching the entity's own grievance handle, and a customer writing
    # "my card was debited twice, no refund" never names the bank -- asking a
    # model whether that post is "about HDFC Bank" would discard the very
    # complaints the source exists to find. The screen exists for noisy
    # feeds, so it runs only on those.
    exclusion_rules = get_setting(db, EXCLUSION_RULES_KEY, DEFAULT_EXCLUSION_RULES)
    if GATE_MODEL and source_type not in ("regulatory", "filing", "social"):
        try:
            keep, excluded, reason = _gate(entity, item["title"],
                                           item["source_name"], exclusion_rules)
            if not keep and human_ruled:
                # A team member has ruled this item IS the entity's -- the
                # cheap screen does not get to overrule that. The negative
                # list below still applies: it is about content type, not
                # about which bank the item concerns.
                log.info("Gate disagreed but a human attributed %r to %s; "
                         "keeping", item["title"][:60], entity["name"])
                keep = True
            if not keep:
                log.info("Gated out %r for %s: %s",
                         item["title"][:60], entity["name"], reason)
                _record_gated_out(db, item, reason)
                return "gated"
            if excluded:
                log.info("Negative-list exclusion %r for %s: %s",
                         item["title"][:60], entity["name"], reason)
                _record_gated_out(db, item, f"negative list: {reason}")
                return "gated"
        except Exception as exc:
            # never let the screen block the pipeline; fall through to
            # full classification, which makes its own relevance judgment
            log.warning("Relevance screen unavailable (%s: %s)",
                        type(exc).__name__, exc)

    factors = active_factors(db, item["entity_id"])
    examples = similar_reviewed(db, item["entity_id"], f"{item['title']} {item['snippet'] or ''}")
    severity_defs = get_setting(db, SEVERITY_DEFS_KEY, DEFAULT_SEVERITY_DEFS)
    risk_defs = get_setting(db, RISK_DEFS_KEY, DEFAULT_RISK_DEFS)
    # The stories this item might continue. None for social posts and
    # filings (see NO_FOLD_TYPES), and none for an item a team member has
    # just attributed by hand: they are looking at that very row, and it
    # must not vanish under another story's sources while they watch.
    events = ([] if (source_type in NO_FOLD_TYPES or human_ruled)
              else recent_events(db, item["entity_id"], item["id"],
                                 item["published_at"]))
    offered = {r["id"] for r in events}
    try:
        verdict, classifier, model = _llm_classify(
            entity, factors, examples,
            item["title"], item["snippet"], item["source_name"], item["published_at"],
            severity_defs, exclusion_rules, risk_defs,
            # The team's own set-aside rulings, so the classifier stops
            # showing them the same kind of post twice. Social only: the
            # rulings are about social noise.
            _render_set_aside(set_aside_examples(db, item["entity_id"]))
            if item["source_type"] == "social" else "",
            events=_render_events(events),
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
        return "gated"

    # Same story, another outlet or another angle: fold it into the story
    # already on the queue. Only a number the model was actually offered
    # counts -- an invented one is ignored, and the item stands alone.
    target = verdict.get("same_event_as") if classifier == "llm" else None
    if isinstance(target, bool):
        target = None
    if target is not None:
        try:
            target = int(target)
        except (TypeError, ValueError):
            target = None
    if target is not None and target in offered:
        if _fold_into(db, item, target):
            return "folded"
    elif target is not None:
        log.info("Ignored same_event_as=%r for %r: not a story that was offered",
                 target, item["title"][:60])

    # What the reviewers taught it: a post of the same character as ones
    # they set aside is dropped before it reaches the tab. Auditable on
    # purpose -- the reason names the learning, the item is stored, and
    # the Social media tab counts these separately so a mis-learned gate
    # is visible rather than silent.
    if (item["source_type"] == "social" and classifier == "llm"
            and verdict.get("like_set_aside")):
        log.info("Set-aside pattern matched for %s: %r",
                 entity["name"], item["title"][:60])
        _record_gated_out(db, item,
                          "matches posts this team set aside as no use for "
                          "pattern-finding",
                          classifier=classifier, model=model)
        return "gated"

    # A social post with no grievance in it is the social equivalent of an
    # irrelevant headline: a protein-deal thread that mentions the bank's
    # card, investor chatter. News like that never reaches the queue (the
    # gate drops it), so social should not either. Only an LLM verdict is
    # trusted to gate here -- the heuristic fallback cannot tell a
    # complaint from a mention, and a wrongly gated grievance is exactly
    # the silent loss this pipeline is built to avoid.
    if (item["source_type"] == "social" and classifier == "llm"
            and not (verdict.get("complaint_topics") or [])):
        log.info("No grievance in social post %r for %s",
                 item["title"][:60], entity["name"])
        _record_gated_out(db, item, "social post with no grievance in it",
                          classifier=classifier, model=model)
        return "gated"

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
    return "classified"


def classify_pending(db, limit: int = 100) -> dict:
    """Classify everything still 'new', oldest first, so a story's first
    report is on the queue by the time its later angles are read and can
    fold into it. Returns how many were classified (or screened out) and
    how many folded into an existing story."""
    rows = q(db, "SELECT * FROM items WHERE status = 'new' ORDER BY id LIMIT ?", (limit,))
    out = {"classified": 0, "folded": 0}
    for row in rows:
        if classify_item(db, row) == "folded":
            out["folded"] += 1
        else:
            out["classified"] += 1
    return out


def classify_new_items(db, limit: int = 100) -> int:
    done = classify_pending(db, limit)
    return done["classified"] + done["folded"]
