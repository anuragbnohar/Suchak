"""Entity resolution: decide which regulated entity a news item is about.

The hard case is names that contain other names. "Bank of India" is a
substring of "State Bank of India", "Union Bank of India" and "Central
Bank of India", so naive substring matching attributes every SBI story to
Bank of India as well.

Two free, deterministic mechanisms fix this before any paid API call:

1. Word-boundary matching, so "Indian Bank" does not match inside
   "Indian Banking Association".
2. Longest-match-wins across the WHOLE registry: a match is discarded when
   a longer alias belonging to a different entity covers the same span of
   text. "State Bank of India" therefore suppresses the "Bank of India"
   match nested inside it, while a standalone "Bank of India" mention
   elsewhere in the same article still counts.

Entities may also carry `exclude_terms` — phrases that disqualify an item
outright, as a manual lever for cases the automatic rules miss.
"""
import json
import re

__all__ = ["Registry", "alias_patterns", "build_query"]


def _variants(alias: str) -> list[str]:
    """Spelling variants worth matching: '&' and 'and' are interchangeable
    in Indian bank names ('Punjab & Sind Bank' / 'Punjab and Sind Bank')."""
    alias = " ".join(alias.split())
    out = {alias}
    if "&" in alias:
        out.add(alias.replace("&", "and"))
    if re.search(r"\band\b", alias, re.I):
        out.add(re.sub(r"\band\b", "&", alias, flags=re.I))
    return sorted(out)


def alias_patterns(alias: str) -> list[re.Pattern]:
    """Case-insensitive, word-boundary-anchored patterns for one alias."""
    pats = []
    for v in _variants(alias):
        # tolerate any run of whitespace between words
        body = r"\s+".join(re.escape(w) for w in v.split())
        pats.append(re.compile(rf"(?<!\w){body}(?!\w)", re.I))
    return pats


class Registry:
    """All entities and their aliases, for cross-entity disambiguation."""

    def __init__(self, rows):
        self.entities = {}
        for row in rows:
            aliases = json.loads(row["aliases"] or "[]")
            try:
                excludes = json.loads(row["exclude_terms"] or "[]")
            except (KeyError, IndexError, TypeError, ValueError):
                excludes = []
            try:
                handle = row["x_handle"] or ""
            except (KeyError, IndexError):
                handle = ""
            # An X handle is a name for the entity, so it counts when
            # deciding what a post is about -- "@HDFCBank_Cares my card is
            # blocked" never spells out "HDFC Bank". It is deliberately kept
            # out of `aliases`, which builds news search queries.
            match_names = list(aliases)
            if handle:
                match_names += [f"@{handle}", handle]
            self.entities[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "aliases": aliases,
                "handle": handle,
                "excludes": excludes,
                "patterns": [(a, p) for a in match_names for p in alias_patterns(a)],
                "exclude_patterns": [p for e in excludes for p in alias_patterns(e)],
            }

    def spans(self, text: str) -> list[tuple]:
        """Every alias hit as (start, end, entity_id, alias)."""
        found = []
        for eid, ent in self.entities.items():
            for alias, pat in ent["patterns"]:
                for m in pat.finditer(text):
                    found.append((m.start(), m.end(), eid, alias))
        return found

    def resolve(self, text: str) -> set[int]:
        """Entity ids genuinely mentioned in `text`, after suppressing
        matches nested inside a longer name belonging to another entity."""
        text = " ".join((text or "").split())
        found = self.spans(text)
        kept = set()
        for start, end, eid, _alias in found:
            length = end - start
            subsumed = any(
                other_eid != eid
                and o_start <= start
                and o_end >= end
                and (o_end - o_start) > length
                for o_start, o_end, other_eid, _ in found
            )
            if not subsumed:
                kept.add(eid)
        # honour manual exclusions
        return {
            eid for eid in kept
            if not any(p.search(text) for p in self.entities[eid]["exclude_patterns"])
        }

    def mentions(self, entity_id: int, text: str) -> bool:
        return entity_id in self.resolve(text)

    def competitors_of(self, entity_id: int) -> list[str]:
        """Other entities' aliases that CONTAIN one of this entity's aliases —
        the names that would otherwise pollute this entity's search results."""
        mine = self.entities[entity_id]["aliases"]
        my_pats = [(a, p) for a in mine for p in alias_patterns(a)]
        rival = []
        for eid, ent in self.entities.items():
            if eid == entity_id:
                continue
            for other in ent["aliases"]:
                if any(p.search(other) and other.lower() != a.lower()
                       for a, p in my_pats):
                    rival.append(other)
        return sorted(set(rival))


def build_query(registry: Registry, entity_id: int, days: int | None = None,
                max_aliases: int = 3, or_token: str = "OR",
                aliases: list[str] | None = None) -> str:
    """Search string: this entity's aliases, minus the longer names that
    contain them, optionally limited to a recent window.

    `or_token` is "OR" for Google News and "|" for the YouTube Data API,
    which spell the boolean the same idea two different ways.
    """
    ent = registry.entities[entity_id]
    # `aliases` lets the caller decide which spellings matter for this feed --
    # a Marathi edition wants the Devanagari forms, not the Latin ones.
    aliases = (aliases if aliases is not None else ent["aliases"])[:max_aliases]
    query = f" {or_token} ".join(f'"{a}"' for a in aliases)
    if len(aliases) > 1:
        query = f"({query})"
    for rival in registry.competitors_of(entity_id):
        query += f' -"{rival}"'
    for term in ent["excludes"]:
        query += f' -"{term}"'
    if days:
        query += f" when:{days}d"
    return query
