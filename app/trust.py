"""Source trust tiers.

Every item gets a tier at ingest:

  official  RBI releases and exchange filings -- routed deterministically
  trusted   the outlet matches the team's trusted-source list
  other     everything else

The trusted list is plain text in settings (one outlet per line or
comma-separated), edited by the super admin; changing it recomputes the
tier of every stored item so ranking and filters update immediately.
Matching is on the normalized outlet name with the item URL as fallback;
entries shorter than five characters (PTI, ANI, Mint) match exactly so
they cannot hide inside longer words.
"""
import re

from .db import get_setting, q

TRUSTED_SOURCES_KEY = "trusted_sources"

DEFAULT_TRUSTED_SOURCES = "\n".join([
    # Marathi press. A Maharashtra cooperative bank is covered here long
    # before it reaches the national English papers, so these have to be
    # trusted or the only items about it would rank below everything.
    "Lokmat",
    "Sakal",
    "Loksatta",
    "Maharashtra Times",
    "Tarun Bharat",
    "Pudhari",
    "Divya Marathi",
    "ABP Majha",
    "TV9 Marathi",
    "Economic Times",
    "Times of India",
    "Livemint",
    "Mint",
    "Business Standard",
    "Moneycontrol",
    "BusinessLine",
    "The Hindu",
    "Financial Express",
    "Indian Express",
    "Business Today",
    "Reuters",
    "Bloomberg",
    "Press Trust of India",
    "PTI",
    "ANI",
    "CNBC TV18",
    "NDTV Profit",
    "Zee Business",
    "ET Now",
    "Fortune India",
])

TIER_RANK = {"official": 0, "trusted": 1, "other": 2}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def parse_trusted(text: str) -> list[str]:
    return [e.strip() for e in re.split(r"[\n,]", text or "") if e.strip()]


def load_trusted_norms(db) -> list[str]:
    text = get_setting(db, TRUSTED_SOURCES_KEY, DEFAULT_TRUSTED_SOURCES)
    return [_norm(e) for e in parse_trusted(text) if _norm(e)]


def tier_for(source_type: str | None, source_name: str | None, url: str | None,
             trusted_norms: list[str]) -> str:
    if (source_type or "news") in ("regulatory", "filing"):
        return "official"
    name = _norm(source_name)
    hay = f"{name}|{_norm(url)}"
    for entry in trusted_norms:
        if len(entry) >= 5:
            if entry in hay:
                return "trusted"
        elif entry == name:   # short entries (PTI, ANI, Mint) match exactly
            return "trusted"
    return "other"


def recompute_source_tiers(db) -> int:
    """Re-tier every stored item against the current trusted list. Cheap
    (one pass, one transaction); called at startup and when the list is
    edited, so stored tiers never go stale."""
    trusted = load_trusted_norms(db)
    rows = q(db, "SELECT id, source_type, source_name, url, source_tier FROM items")
    changes = []
    for r in rows:
        tier = tier_for(r["source_type"], r["source_name"], r["url"], trusted)
        if tier != (r["source_tier"] or ""):
            changes.append((tier, r["id"]))
    # Attached sources too, so the trusted filter can find a story whose
    # trusted report is one of the links rather than the face. The tier is
    # judged with the primary item's source_type -- the attached row does
    # not record its own.
    srows = q(db, "SELECT s.id, s.source_name, s.url, s.source_tier,"
                  " i.source_type FROM item_sources s JOIN items i ON i.id = s.item_id")
    schanges = []
    for r in srows:
        tier = tier_for(r["source_type"], r["source_name"], r["url"], trusted)
        if tier != (r["source_tier"] or ""):
            schanges.append((tier, r["id"]))
    if changes or schanges:
        with db:
            db.executemany("UPDATE items SET source_tier = ? WHERE id = ?", changes)
            db.executemany("UPDATE item_sources SET source_tier = ? WHERE id = ?",
                           schanges)
    return len(changes) + len(schanges)
