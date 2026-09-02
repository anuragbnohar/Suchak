"""Patterns across an entity's social-media grievances.

One complaint is an anecdote; thirty complaints about the same NOC
process are a supervisory finding. This module reads the grievances the
Social media tab already holds, asks the strong model what keeps
recurring -- a product, a process, a failure mode -- and stores each
pattern with its evidence and a recommendation.

The model proposes; the code verifies. Every insight must cite item ids
from the grievances it was shown, the citations are checked against that
set, and an insight left with fewer than MIN_EVIDENCE valid citations is
dropped -- a pattern that cannot point at its own evidence is not a
pattern. Counts shown are recomputed from the verified citations, never
taken from the model's own arithmetic.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from .classify import MODEL, _get_client
from .db import q, x
from .ingest import SOCIAL_LOOKBACK_DAYS

log = logging.getLogger("suchak.insights")

# A pattern needs several independent complaints behind it. Two people
# angry about the same thing is a coincidence worth watching; three or
# more citing the same process is a deficiency signal.
MIN_EVIDENCE = 3
MAX_GRIEVANCES = 80          # newest first; bounds one generation's tokens
MAX_INSIGHTS = 8

INSIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_process": {
                        "type": "string",
                        "description": "The specific product or process the "
                        "complaints converge on, e.g. 'Home loan NOC "
                        "issuance', 'FASTag toll deduction', 'Account "
                        "freezing on suspicion of fraud'. Specific, not a "
                        "category like 'customer service'."},
                    "pattern": {
                        "type": "string",
                        "description": "What keeps happening, in two or "
                        "three sentences grounded in the cited complaints."},
                    "recommendation": {
                        "type": "string",
                        "description": "What the supervisory team should "
                        "examine or ask the bank, in one or two sentences."},
                    "severity": {"type": "string",
                                 "enum": ["high", "medium", "low"]},
                    "item_ids": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Ids of the complaints this pattern "
                        "rests on. Only ids from the list you were given."},
                },
                "required": ["product_process", "pattern", "recommendation",
                             "severity", "item_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["insights"],
    "additionalProperties": False,
}

SYSTEM = """You assist a Reserve Bank of India supervisory (SSM) team.
You are given customer grievances about one regulated entity, collected
from social media and consumer-complaint forums. Each carries an id.

Find patterns that indicate a deficiency in the bank's products,
processes or services: the same product or process drawn on repeatedly,
the same failure mode recurring across products, or a cluster that
together points at one root cause.

Rules:
- Name the specific product or process. "Customer service is poor" is
  not a finding; "NOC issuance after home-loan closure stalls for
  months" is.
- Cite only ids from the list you were given, and only complaints that
  genuinely evidence the pattern.
- A pattern needs at least three supporting complaints. Fewer is an
  anecdote: leave it out.
- No patterns is a valid answer: return an empty list rather than
  forcing one. These findings reach a supervisor; a manufactured
  pattern wastes an examination.
- Severity reflects customer harm and prudential concern together:
  money lost or accounts frozen outranks slow paperwork.
- Write in plain English. Output English only, whatever language the
  complaints are in."""


def grievances_for(db, entity_id: int) -> list[dict]:
    """The complaints a generation run reads: classified social items with
    a grievance in them, newest first, capped. Posts a reviewer set aside
    (generic, venting, duplicate...) are excluded -- a pattern built on
    noise is worse than no pattern."""
    rows = q(db,
             "SELECT id, title, snippet, published_at, complaint_topics,"
             " COALESCE(review_severity, severity) AS sev"
             " FROM items WHERE entity_id = ? AND source_type = 'social'"
             " AND gated_out = 0 AND status != 'new'"
             " AND complaint_topics IS NOT NULL AND complaint_topics != '[]'"
             " AND set_aside IS NULL"
             " ORDER BY COALESCE(published_at, '') DESC LIMIT ?",
             (entity_id, MAX_GRIEVANCES))
    return [dict(r) for r in rows]


def _render_grievances(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        topics = ", ".join(json.loads(r["complaint_topics"] or "[]"))
        when = (r["published_at"] or "undated")[:10]
        snippet = " ".join((r["snippet"] or "").split())[:400]
        lines.append(f"[id {r['id']}] ({when}, severity {r['sev']}, "
                     f"topics: {topics or 'none'})\n"
                     f"  {r['title']}\n  {snippet}")
    return "\n\n".join(lines)


def _llm_insights(entity_name: str, rows: list[dict]) -> list[dict]:
    client = _get_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Entity: {entity_name}\n"
                   f"{len(rows)} grievances follow.\n\n"
                   + _render_grievances(rows)}],
        output_config={"format": {"type": "json_schema",
                                  "schema": INSIGHTS_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused insight generation")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text).get("insights") or []


def generate(db, entity, user_id: int | None = None) -> dict:
    """Regenerate this entity's insights from its current grievances.

    Replaces the previous set: insights are derived data, and two
    generations shown side by side would double-count the same
    complaints. Returns counts for the caller's status message.
    """
    rows = grievances_for(db, entity["id"])
    if not rows:
        return {"grievances": 0, "insights": 0, "dropped": 0}

    proposed = _llm_insights(entity["name"], rows)

    valid_ids = {r["id"] for r in rows}
    kept, dropped = [], 0
    for ins in proposed[:MAX_INSIGHTS]:
        cited = [i for i in dict.fromkeys(ins.get("item_ids") or [])
                 if i in valid_ids]
        if len(cited) < MIN_EVIDENCE:
            dropped += 1
            continue
        kept.append({
            "product": (ins.get("product_process") or "").strip(),
            "pattern": (ins.get("pattern") or "").strip(),
            "recommendation": (ins.get("recommendation") or "").strip(),
            "severity": ins.get("severity")
                if ins.get("severity") in ("high", "medium", "low") else "medium",
            "item_ids": cited,
        })
    kept = [k for k in kept if k["product"] and k["pattern"]
            and k["recommendation"]]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db:
        db.execute("DELETE FROM insights WHERE entity_id = ?", (entity["id"],))
        for ins in kept:
            db.execute(
                "INSERT INTO insights (entity_id, generated_at, generated_by,"
                " model, window_days, n_grievances, product, pattern,"
                " recommendation, severity, item_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entity["id"], now, user_id, MODEL, SOCIAL_LOOKBACK_DAYS,
                 len(rows), ins["product"], ins["pattern"],
                 ins["recommendation"], ins["severity"],
                 json.dumps(ins["item_ids"])))
    log.info("Insights for %s: %d kept, %d dropped, from %d grievances",
             entity["name"], len(kept), dropped, len(rows))
    return {"grievances": len(rows), "insights": len(kept), "dropped": dropped}
