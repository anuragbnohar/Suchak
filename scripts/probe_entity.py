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
import time

import feedparser
import httpx

from app.ingest import (LOOKBACK_CHOICES, NEWS_QUERY_ALIASES, _strip_html,
                        aliases_for_language, entity_languages,
                        fetch_google_news, google_news_url)
from app.matching import (Registry, alias_patterns, near_miss as _near_miss,
                          _match_tokens as _tokens, _token_present as _present)

# Not \w+: in Devanagari the vowel signs are combining marks, which \w does
# not match, so \w+ shreds "नागरीक" into fragments and every comparison
# against it becomes noise. Splitting on whitespace and trimming punctuation
# keeps words of any script intact.
PROBE_BUILD = "2026-08-26.1-per-alias"


def _fetch_feed(url: str) -> list[dict]:
    """One feed, minimally parsed. Raises on transport errors so the caller
    can print the failure instead of mistaking it for an empty result."""
    resp = httpx.get(url, timeout=20, follow_redirects=True,
                     headers={"User-Agent": "Suchak/0.1 (supervisory prototype)"})
    resp.raise_for_status()
    out = []
    for entry in feedparser.parse(resp.text).entries:
        title = _strip_html(entry.get("title", ""))
        if title:
            out.append({"title": title, "link": entry.get("link", ""),
                        "source": (entry.get("source") or {}).get("title", "")})
    return out


def _per_alias_report(registry, entity, aliases, langs, days, extra):
    """Fetch a separate feed for every spelling and compare with the
    combined query. Google's answer to a six-phrase OR is not the union of
    its answers to each phrase, so this is the only way to see which
    spelling the press actually uses -- and what the OR is costing."""
    print("\n--- one feed per spelling "
          "(what would Google return for each alone?)\n")
    union: dict[str, str] = {}
    per_alias: list[tuple[str, object, list]] = []
    tested = 0
    for lang in langs:
        ordered = aliases_for_language(aliases, lang)
        in_query = set(ordered[:NEWS_QUERY_ALIASES])
        # Tried phrases first: they are the question being asked, and with
        # a dozen stored aliases the cap must never squeeze them out.
        candidates = list(dict.fromkeys(list(extra) + ordered))
        for alias in candidates[:14]:
            if tested:
                time.sleep(1.0)
            tested += 1
            url = google_news_url(registry, entity["id"], lang, days,
                                  aliases=[alias])
            tag = ("tried" if alias in extra else
                   "in query" if alias in in_query else "NOT in query")
            try:
                entries = _fetch_feed(url)
            except Exception as exc:
                print(f"  [{lang}] {alias!r:<55} ERROR "
                      f"{type(exc).__name__}: {exc}")
                per_alias.append((alias, None, []))
                continue
            for e in entries:
                union.setdefault(e["title"].lower(), e["title"])
            per_alias.append((alias, len(entries), entries))
            print(f"  [{lang}] {alias!r:<55} {len(entries):>3} result(s)  ({tag})")
            for e in entries[:2]:
                print(f"           · {e['title'][:96]}")
    return union, per_alias


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True, help="name or alias substring")
    ap.add_argument("--days", type=int, default=None,
                    help=f"lookback window; one of {', '.join(map(str, LOOKBACK_CHOICES))}")
    ap.add_argument("--lang", help="probe only this language code")
    ap.add_argument("--try", dest="extra", action="append", default=[],
                    metavar="PHRASE",
                    help="also test this spelling as its own search, without "
                         "saving it as an alias (repeatable)")
    args = ap.parse_args()
    print(f"probe_entity {PROBE_BUILD}")

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

        print("\nfetching the combined query...\n")
        candidates = fetch_google_news(registry, entity, args.days)
        if not candidates:
            print("The combined feed returned nothing at all.")

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

        union, per_alias = _per_alias_report(registry, entity, aliases, langs,
                                             args.days, args.extra)
        combined_titles = {c["title"].lower() for c in candidates}
        only_single = [t for k, t in union.items() if k not in combined_titles]
        print(f"\n--- verdict")
        print(f"  combined query returned   : {len(candidates)}")
        print(f"  all spellings, together   : {len(union)} distinct article(s)")
        if only_single:
            print(f"  MISSED by the combined query ({len(only_single)}):")
            for t in only_single[:8]:
                print(f"    · {t[:100]}")
            print("  Google under-returns multi-phrase OR queries: articles a "
                  "single spelling finds can vanish when six spellings are "
                  "ORed. The spellings marked 'NOT in query' above never reach "
                  "Google at all -- reorder the aliases so the productive ones "
                  "come first. A spelling marked 'tried' that returns results "
                  "belongs in the alias list: add it on the Entities page.")
        elif union:
            print("  The combined query found everything the individual "
                  "spellings did -- the misses, if any, are spellings nobody "
                  "has tested yet. Try more with --try \"...\".")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
