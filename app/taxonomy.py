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

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
ACTIONABILITY_RANK = {"action_recommended": 0, "review_recommended": 1, "monitor": 2}
