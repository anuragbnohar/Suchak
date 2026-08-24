"""Show what a feed returns for one entity, and why each item was kept or dropped.

    python -m scripts.probe_entity --entity Nagpur --days 365
    python -m scripts.probe_entity --entity "Shriram" --days 30 --lang en

"N rejected as another entity's news" tells you the feed worked and
attribution refused the results, but not what they were. Attribution is a
regex over the alias list, so the usual cause is a spelling the aliases do
not carry -- Nagarik vs Nagrik, नागरीक vs नागरिक. This prints every headline
the feed returned, marks the ones that failed, and for each failure shows
which alias words *did* appear, so a near miss is obvious.

Read-only: nothing is stored, nothing is classified, nothing is billed.
"""
import argparse
import json
import re

# Runnable either as `python -m scripts.probe_entity` or `python scripts/probe_entity.py`.
# The second form puts scripts/ on the import path instead of the project
# root, so add the root here and let `app` import the same way in both.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, q
from app.ingest import (LOOKBACK_CHOICES, entity_languages, fetch_google_news,
                        google_news_url)
from app.matching import Registry, alias_patterns

# Not \w+: in Devanagari the vowel signs are combining marks, which \w does
# not match, so \w+ shreds "नागरीक" into fragments and every comparison
# against it becomes noise. Splitting on whitespace and trimming punctuation
# keeps words of any script intact.
_PUNCT = ".,;:!?\"'()[]{}<>«»—–-·|/\u0964\u0965\u2018\u2019\u201c\u201d"


def _tokens(text: str) -> list[str]:
    return [w for w in ((raw.strip(_PUNCT).lower()) for raw in (text or "").split()) if w]


def _present(token: str, hay: list[str]) -> bool:
    """A token counts as present if some word in the text starts with it, so
    an inflected form (बँक -> बँकेवर, Bank -> Bank's) still counts."""
    return any(w.startswith(token) for w in hay)


def _near_miss(title: str, snippet: str, aliases: list[str]):
    """The alias sharing the most words with this text, and which of its
    words are absent. This ranks candidates for a human to look at -- a
    lexically close headline can still be a different institution, and the
    caller prints the headline so that judgement stays with the reader."""
    hay = _tokens(f"{title} {snippet}")
    best = None
    for alias in aliases:
        want = _tokens(alias)
        if not want:
            continue
        missing = [w for w in want if not _present(w, hay)]
        score = (len(want) - len(missing)) / len(want)
        if best is None or score > best[0]:
            best = (score, alias, len(want) - len(missing), missing)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True, help="name or alias substring")
    ap.add_argument("--days", type=int, default=None,
                    help=f"lookback window; one of {', '.join(map(str, LOOKBACK_CHOICES))}")
    ap.add_argument("--lang", help="probe only this language code")
    args = ap.parse_args()

    init_db()
    db = connect()
    try:
        rows = q(db, "SELECT * FROM entities ORDER BY name")
        term = args.entity.strip().lower()
        match = [e for e in rows
                 if term in e["name"].lower()
                 or any(term in a.lower() for a in json.loads(e["aliases"]))]
        if not match:
            print(f"No entity matched {args.entity!r}. Loaded: "
                  + ", ".join(e["name"] for e in rows))
            return 1
        if len(match) > 1:
            print("Matched more than one entity; be more specific: "
                  + ", ".join(e["name"] for e in match))
            return 1

        entity = match[0]
        aliases = json.loads(entity["aliases"])
        registry = Registry(rows)
        langs = [args.lang] if args.lang else entity_languages(entity)

        print(f"{entity['name']}  [{entity['kind']}]")
        print(f"aliases : {', '.join(aliases)}")
        print(f"languages: {', '.join(langs)}")
        for lang in langs:
            print(f"\nquery [{lang}]: "
                  f"{google_news_url(registry, entity['id'], lang, args.days)}")

        print("\nfetching...\n")
        candidates = fetch_google_news(registry, entity, args.days)
        if not candidates:
            print("The feed returned nothing at all. This is genuinely no coverage,")
            print("not a matching problem -- widening the window is the only lever,")
            print("and for a small entity the RBI feed may be the real channel.")
            return 0

        names = {r["id"]: r["name"] for r in rows}
        kept = rejected = 0
        for c in candidates:
            text = f"{c['title']} {c.get('snippet') or ''}"
            hit = registry.mentions(entity["id"], text)
            kept, rejected = (kept + 1, rejected) if hit else (kept, rejected + 1)
            print(f"{'KEEP  ' if hit else 'REJECT'} {c['title'][:110]}")
            print(f"       {c.get('source_name') or '?'} · {c.get('published_at') or 'undated'}")
            if not hit:
                others = [names[i] for i in registry.resolve(text)]
                if others:
                    print(f"       -> attributed instead to: {', '.join(others)}")
                near = _near_miss(c["title"], c.get("snippet") or "", aliases)
                if near and near[0] >= 0.6 and near[3]:
                    score, alias, hits, missing = near
                    total = hits + len(missing)
                    print(f"       -> close to {alias!r}: {hits}/{total} words match, "
                          f"absent: {', '.join(missing)}")
                    print(f"          spelling variant, or a different institution? "
                          f"read the headline. If it is this bank, add its spelling "
                          f"as an alias.")
            print()

        print(f"{kept} would be kept, {rejected} rejected, {len(candidates)} returned.")
        if rejected and not kept:
            print("\nEverything was rejected. Either the feed is returning other "
                  "institutions' news, in which case attribution is doing its job, "
                  "or the aliases do not carry the spelling the press uses. The "
                  "'close to' lines above are the ones worth reading: a headline "
                  "that is clearly this bank under another spelling means adding "
                  "that spelling as an alias.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
