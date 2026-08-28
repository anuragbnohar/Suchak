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


# Corporate suffixes the press drops. "X Bank Ltd." and "X Bank" are the
# same institution in every sentence ever written about them, so a roster
# entry carrying only the legal name should still match the coverage.
_LEGAL_SUFFIXES = (
    "private limited", "pvt. ltd.", "pvt ltd", "limited", "ltd.", "ltd",
)

# Indian cooperative banks are spelled every one of these ways, often in
# the same article. Attribution is a strict phrase match, so each spelling
# has to be present as an alias or the headline is discarded unread.
_COOP_FORMS = ("co-operative", "cooperative", "co operative", "co-op")


def derive_aliases(names: list[str]) -> list[str]:
    """Spelling variants of an entity's own names, for attribution.

    Purely mechanical: a dropped legal suffix, and the interchangeable
    spellings of "co-operative". Nothing here guesses which words the
    press omits -- shortening "Motiram Agrawal Jalna Merchants Bank" to
    "Jalna Merchants Bank" is editorial judgement and stays with the
    team. Returned in the order generated, with anything already present
    left out, so callers can append without disturbing the aliases a
    human chose to put first.
    """
    seen = {n.strip().lower() for n in names if n.strip()}
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = " ".join(candidate.split()).strip(" ,")
        key = candidate.lower()
        if candidate and key not in seen:
            seen.add(key)
            out.append(candidate)

    for name in list(names):
        base = " ".join((name or "").split())
        if not base:
            continue
        # drop a trailing legal suffix, once
        low = base.lower()
        for suffix in _LEGAL_SUFFIXES:
            if low.endswith(" " + suffix):
                add(base[: -len(suffix)].rstrip(" ,"))
                break

    # spelling variants, over the names and anything just derived
    for name in list(names) + list(out):
        low = (name or "").lower()
        for form in _COOP_FORMS:
            if form not in low:
                continue
            start = low.index(form)
            original = name[start:start + len(form)]
            for other in _COOP_FORMS:
                if other == form:
                    continue
                # The team reads these aliases on the Entities page, so a
                # variant of "Co-Operative Bank" should not arrive as
                # "cooperative Bank".
                if original[:1].isupper():
                    other = "-".join(w.capitalize() for w in other.split("-")) \
                        if "-" in other else \
                        " ".join(w.capitalize() for w in other.split(" "))
                add(name[:start] + other + name[start + len(form):])
            break
    return out
