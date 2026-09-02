"""Supervisory taxonomy: the controlled vocabularies the whole app shares.

Finalize these with the SSM team before the pilot; everything else
(classification schema, review form, dashboards) derives from here.
"""

RISK_AREAS = [
    "Credit Risk",
    "Market Risk",
    "Liquidity Risk",
    "Operational Risk",
    "Governance Risk",
    "Cybersecurity Risk",
    "Conduct & Consumer Protection",
]

SEVERITIES = ["high", "medium", "low"]

ACTIONABILITY = ["action_recommended", "review_recommended", "monitor"]

ACTIONABILITY_LABELS = {
    "action_recommended": "Action recommended",
    "review_recommended": "Review recommended",
    "monitor": "Monitor",
}

# Supervisory actions a reviewer can record. These become the labels the
# learning loop retrieves as precedent for similar future items.
ACTIONS = [
    "No action required",
    "Continue monitoring",
    "Sought clarification from entity",
    "Escalated to senior supervisor",
    "Flagged for next inspection",
    "Referred for enforcement review",
]

ENTITY_KINDS = [
    "Scheduled Commercial Bank",
    "NBFC",
    "Urban Cooperative Bank",
    "Rural Cooperative Bank",
    "Payment System Operator",
]

ROLES = ["member", "lead", "superadmin"]

# Topics for customer-grievance items, assigned by the classifier when an
# item reports complaints (in news coverage or social posts). An empty list
# means the item is not about customer grievances.
COMPLAINT_TOPICS = [
    "Mis-selling",
    "Recovery practices",
    "Service disruption",
    "Unauthorized transactions",
    "Charges & fees",
    "Harassment",
    "Account access / KYC",
    "Other grievance",
]

# Chip shown in the queue/detail for non-news items; news shows none.
SOURCE_TYPE_LABELS = {
    "video": "\u25b6 video",
    "social": "\u2709 social",
    "regulatory": "\u2696 regulator",
    "filing": "\u21ea exchange filing",
}

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
ACTIONABILITY_RANK = {"action_recommended": 0, "review_recommended": 1, "monitor": 2}


# Which platform a social item came from, decided by its link rather than
# its label: labels vary per post (@handle, r/india, a channel name) but
# the host never does, and this reads correctly for items stored before
# the filter existed.
SOCIAL_PLATFORMS = ["X", "Reddit", "consumercomplaints.in", "YouTube"]
_PLATFORM_HOSTS = [
    (("x.com", "twitter.com"), "X"),
    (("reddit.com",), "Reddit"),
    (("consumercomplaints.in",), "consumercomplaints.in"),
    (("youtube.com", "youtu.be"), "YouTube"),
]


def social_platform(url: str) -> str:
    low = (url or "").lower()
    for hosts, label in _PLATFORM_HOSTS:
        if any(f"//{h}" in low or f".{h}" in low or f"//www.{h}" in low
               for h in hosts):
            return label
    return "Other"


# Why a reviewer set a social post aside. A post can be a real grievance
# and still be useless for pattern-finding -- pure venting names no
# product, a generic gripe names no process -- so Insights must not count
# it. The judgement is the reviewer's, never the classifier's.
SOCIAL_SET_ASIDE = [
    ("generic", "Generic — no specific product or process named"),
    ("venting", "Customer venting — anger without a complaint to act on"),
    ("not_service", "Not about the bank's service"),
    ("duplicate", "Duplicate of another post"),
    ("resolved", "Already resolved or informational"),
]
SET_ASIDE_LABELS = dict(SOCIAL_SET_ASIDE)
