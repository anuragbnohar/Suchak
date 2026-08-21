"""Demo seed data.

Entities and news items here are FICTIONAL so the demo never asserts
anything about a real institution. Add real regulated entities via the
Entities page and press "Fetch now" to pull live public news for them.
"""
import json
from datetime import datetime, timedelta, timezone

from .auth import hash_password
from .db import get_setting, one, set_setting, x

_now = datetime.now(timezone.utc)


def _ago(days: float) -> str:
    return (_now - timedelta(days=days)).isoformat(timespec="seconds")


ENTITIES = [
    ("Bharat National Bank", "Scheduled Commercial Bank",
     ["Bharat National Bank", "BNB Bank"]),
    ("Meru Finance Ltd", "NBFC", ["Meru Finance", "Meru Finance Ltd"]),
    ("Suvarna Sahakari Bank", "Urban Cooperative Bank",
     ["Suvarna Sahakari Bank", "Suvarna Cooperative Bank"]),
    ("Kaveri Gramin Bank", "Rural Cooperative Bank", ["Kaveri Gramin Bank"]),
    ("PayEase Payments", "Payment System Operator", ["PayEase Payments", "PayEase"]),
]

USERS = [
    # username, password, display, role, entity index (1-based) or None, risk areas
    ("admin", "admin123", "Super Admin", "superadmin", None, []),
    ("priya", "priya123", "Priya Nair", "lead", 1, []),
    ("rahul", "rahul123", "Rahul Verma", "member", 1,
     ["Credit Risk", "Operational Risk"]),
]

FACTORS = [
    # entity index or None (global), name, conditions
    (None, "Sales malpractice",
     "Flag if the item alleges mis-selling of insurance or investment products, "
     "forced bundling of products with loans, or unauthorized account opening."),
    (1, "Branch service failures",
     "Flag reports of branch-level customer service breakdowns, extended branch "
     "or ATM outages, or refusal of basic banking services."),
]

# entity idx, days ago, title, source, snippet, severity, risk_areas,
# actionability, geography, relationships, review (None or dict)
DEMO_ITEMS = [
    (1, 0.4,
     "Bharat National Bank UPI services down for six hours across multiple states",
     "The Banking Bulletin",
     "Customers reported failed UPI transactions from early morning; the bank "
     "attributed the disruption to a data-centre switch failure and restored "
     "services by afternoon.",
     "medium", ["Operational Risk"], "review_recommended", "Pan-India", [], None),
    (1, 1.2,
     "Vantara Infra defaults on ₹840 crore loan; Bharat National Bank largest lender",
     "Finance Daily",
     "Vantara Infrastructure missed a scheduled repayment on its consortium loan. "
     "Bharat National Bank holds the largest exposure at around ₹840 crore.",
     "high", ["Credit Risk"], "action_recommended", "Maharashtra",
     [{"type": "borrower_of", "name": "Vantara Infrastructure"}],
     {"user": "priya", "days_ago": 0.9, "relevant": 1,
      "risk_areas": ["Credit Risk"], "actionable": 1,
      "action": "Sought clarification from entity",
      "notes": "Asked for exposure detail and provisioning plan."}),
    (1, 2.1,
     "Complaints mount over insurance policies bundled with BNB Bank home loans",
     "Business Khabar",
     "Borrowers allege branch staff made purchase of a linked insurance policy a "
     "condition for home-loan disbursal in several districts.",
     "medium", ["Conduct & Consumer Protection"], "review_recommended", "Uttar Pradesh",
     [], {"user": "rahul", "days_ago": 1.8, "relevant": 1, "severity": "high",
          "risk_areas": ["Conduct & Consumer Protection"], "actionable": 1,
          "action": "Flagged for next inspection",
          "notes": "Matches Sales malpractice factor; recurring pattern. "
                   "Raised severity: forced bundling is systemic, not incidental."}),
    (1, 3.0,
     "Bharat National Bank posts steady quarterly results, deposits grow 11%",
     "Finance Daily",
     "The lender reported stable asset quality and continued deposit growth for "
     "the quarter.",
     "low", [], "monitor", None, [],
     {"user": "rahul", "days_ago": 2.5, "relevant": 1, "risk_areas": [],
      "actionable": 0, "action": "No action required", "notes": "Routine results coverage."}),
    (1, 5.5,
     "Phishing campaign impersonates BNB Bank net-banking portal",
     "CyberWatch India",
     "Security researchers found lookalike domains harvesting customer "
     "credentials via SMS links purporting to be from the bank.",
     "high", ["Cybersecurity Risk", "Operational Risk"], "review_recommended",
     "Pan-India", [], None),
    (1, 9.0,
     "BNB Bank branch in Nashik shut for third day over staffing dispute",
     "Metro Times Nashik",
     "Customers were turned away as a local staffing dispute kept the branch "
     "closed; the bank said normal operations would resume shortly.",
     "medium", ["Operational Risk", "Conduct & Consumer Protection"],
     "monitor", "Maharashtra", [], None),

    (2, 0.8,
     "Meru Finance faces redemption pressure as debt fund exits accelerate",
     "Finance Daily",
     "Institutional investors pulled funds from instruments issued by the NBFC "
     "amid concerns over its commercial-property loan book.",
     "high", ["Liquidity Risk", "Credit Risk"], "action_recommended", None, [], None),
    (2, 4.2,
     "Meru Finance auditor flags related-party transactions in annual filing",
     "The Banking Bulletin",
     "The statutory auditor's report noted transactions with promoter-linked "
     "companies requiring additional disclosure.",
     "high", ["Governance Risk"], "review_recommended", None,
     [{"type": "promoter_of", "name": "Meru Group Holdings"}],
     {"user": "priya", "days_ago": 3.9, "relevant": 1,
      "risk_areas": ["Governance Risk"], "actionable": 1,
      "action": "Escalated to senior supervisor",
      "notes": "Auditor qualification; needs governance review."}),
    (2, 12.0,
     "Meru Finance expands gold-loan branches in southern states",
     "Business Khabar",
     "The NBFC announced fifty new branches focused on gold-backed lending.",
     "low", [], "monitor", "Karnataka", [], None),

    (3, 0.6,
     "Depositors queue outside Suvarna Sahakari Bank after fraud rumours on social media",
     "Metro Times Pune",
     "Long queues formed at two branches after viral messages alleged "
     "irregularities in the bank's loan book; the bank denied the claims.",
     "high", ["Liquidity Risk", "Governance Risk"], "action_recommended", "Maharashtra",
     [], None),
    (3, 6.5,
     "Suvarna Sahakari Bank AGM postponed amid board disagreement",
     "Pune Vartapatra",
     "The annual general meeting was deferred after directors disagreed over "
     "co-option of new board members.",
     "medium", ["Governance Risk"], "review_recommended", "Maharashtra", [],
     {"user": "priya", "days_ago": 6.0, "relevant": 1,
      "risk_areas": ["Governance Risk"], "actionable": 0,
      "action": "Continue monitoring", "notes": "Watch for escalation at rescheduled AGM."}),

    (4, 2.8,
     "Kaveri Gramin Bank waives minimum-balance charges for crop-loan customers",
     "Rural Sandesh",
     "The bank announced fee relief for agricultural borrowers ahead of the "
     "sowing season.",
     "low", [], "monitor", "Karnataka", [], None),
    (4, 10.5,
     "Recovery-agent conduct complaint filed against Kaveri Gramin Bank",
     "Rural Sandesh",
     "A borrower group alleged coercive recovery practices in two taluks and "
     "sought regulatory intervention.",
     "medium", ["Conduct & Consumer Protection"], "review_recommended", "Karnataka",
     [], None),

    (5, 0.3,
     "PayEase wallet outage delays merchant settlements nationwide",
     "The Banking Bulletin",
     "Merchants reported settlement delays of up to a day after a database "
     "failure at the payment operator.",
     "medium", ["Operational Risk"], "review_recommended", "Pan-India", [], None),
    (5, 7.8,
     "Data of PayEase customers offered for sale on hacker forum, researchers say",
     "CyberWatch India",
     "A dataset purporting to contain customer phone numbers and transaction "
     "metadata was listed on an underground forum; PayEase said it found no "
     "breach of its core systems.",
     "high", ["Cybersecurity Risk"], "action_recommended", "Pan-India", [], None),
    (3, 1.4,
     "Reserve Bank of India imposes monetary penalty on Suvarna Sahakari Bank",
     "Reserve Bank of India",
     "A monetary penalty of \u20b945 lakh was imposed for non-compliance with "
     "directions on KYC norms and loan classification, based on the statutory "
     "inspection of the bank.",
     "high", ["Governance Risk"], "action_recommended", "Maharashtra", [], None),
    (1, 2.6,
     "Bharat National Bank Ltd - Outcome of Board Meeting",
     "BSE",
     "The board approved raising Tier-II capital of up to \u20b91,200 crore "
     "through issuance of bonds in one or more tranches.",
     "low", [], "monitor", None, [], None),

    (5, 13.2,
     "PayEase partners with Kaveri Gramin Bank for rural payment access",
     "Rural Sandesh",
     "The payment operator will provide payment infrastructure for the rural "
     "bank's customers.",
     "low", [], "monitor", "Karnataka",
     [{"type": "partner_of", "name": "Kaveri Gramin Bank"}], None),
]


def seed_if_empty(db) -> bool:
    if one(db, "SELECT 1 FROM entities LIMIT 1"):
        return False
    # a database emptied on purpose (scripts/reset_data.py) must stay empty
    if get_setting(db, "seeded", "") == "1":
        return False

    entity_ids = {}
    for i, (name, kind, aliases) in enumerate(ENTITIES, start=1):
        entity_ids[i] = x(db, "INSERT INTO entities (name, kind, aliases) VALUES (?,?,?)",
                          (name, kind, json.dumps(aliases)))

    user_ids = {}
    for username, pw, display, role, eidx, areas in USERS:
        user_ids[username] = x(
            db,
            "INSERT INTO users (username, password_hash, display_name, role, entity_id, risk_areas)"
            " VALUES (?,?,?,?,?,?)",
            (username, hash_password(pw), display, role,
             entity_ids.get(eidx), json.dumps(areas)),
        )

    for eidx, name, conditions in FACTORS:
        x(db, "INSERT INTO factors (entity_id, name, conditions, created_by) VALUES (?,?,?,?)",
          (entity_ids.get(eidx), name, conditions, user_ids["admin"]))

    for (eidx, days, title, source, snippet, severity, areas, actionability,
         geo, rels, review) in DEMO_ITEMS:
        status = "classified"
        fields = {
            "entity_id": entity_ids[eidx],
            "title": title,
            "url": f"https://example.org/demo/{abs(hash(title)) % 10_000_000}",
            "source_name": source,
            "snippet": snippet,
            "published_at": _ago(days),
            "created_at": _ago(days),
            "status": status,
            "relevance": 0.95,
            "risk_areas": json.dumps(areas),
            "severity": severity,
            "actionability": actionability,
            "geography": geo,
            "summary": snippet,
            "factor_matches": json.dumps(
                ["Sales malpractice"] if "insurance" in title.lower() or "bundled" in title.lower() else []
            ),
            "relationships": json.dumps(rels),
            "classifier": "seed-demo",
            "model": "demo-data",
            "classified_at": _ago(days),
        }
        if review:
            fields.update({
                "status": "reviewed" if review["relevant"] else "dismissed",
                "reviewed_by": user_ids[review["user"]],
                "reviewed_at": _ago(review["days_ago"]),
                "review_relevant": review["relevant"],
                "review_severity": review.get("severity"),
                "review_risk_areas": json.dumps(review["risk_areas"]),
                "review_actionable": review["actionable"],
                "review_action": review["action"],
                "review_notes": review["notes"],
            })
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        x(db, f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(fields.values()))

    set_setting(db, "seeded", "1", user_ids["admin"])

    # tag the demo items that report customer grievances with their topics
    topic_updates = [
        ("%insurance policies bundled%", ["Mis-selling"]),
        ("%UPI services down%", ["Service disruption"]),
        ("%branch in Nashik shut%", ["Service disruption"]),
        ("%Recovery-agent conduct complaint%", ["Recovery practices", "Harassment"]),
        ("%wallet outage delays%", ["Service disruption"]),
    ]
    for pattern, topics in topic_updates:
        x(db, "UPDATE items SET complaint_topics=? WHERE title LIKE ?",
          (json.dumps(topics), pattern))

    # mark the official-source demo items with their source types
    x(db, "UPDATE items SET source_type='regulatory'"
          " WHERE title LIKE 'Reserve Bank of India imposes%'")
    x(db, "UPDATE items SET source_type='filing'"
          " WHERE title LIKE '%Outcome of Board Meeting%'")

    return True
