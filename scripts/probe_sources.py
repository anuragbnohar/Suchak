"""Test the complaint sources from your own machine and report what works.

    python -m scripts.probe_sources --entity HDFC
    python -m scripts.probe_sources --entity HDFC --reddit-only
    python -m scripts.probe_sources --forums-only

Nothing is stored and nothing is classified, so this costs no money. It
answers one question per source: did it serve us data, and what did the
data look like? A source that refuses says why, so a failure is
attributable rather than silent.

The forum section prints structure, not a parser's output. No parser for
those sites exists yet -- this is what tells us whether writing one is
worth doing, and what to write it against.
"""
import argparse
import collections
import html.parser
import json
import urllib.parse

# Runnable either as `python -m scripts.probe_sources` or
# `python scripts/probe_sources.py`. The second form puts scripts/ on the
# import path instead of the project root, so add the root here.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import reddit_source
from app.db import connect, init_db, q
from app.matching import Registry

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Indian consumer-complaint sites that carry retail banking grievances.
# The {q} slot is the entity search term.
FORUMS = [
    ("consumercomplaints.in", "https://www.consumercomplaints.in/?search={q}"),
    ("grahakseva.com",        "https://www.grahakseva.com/search?q={q}"),
    ("mouthshut.com",         "https://www.mouthshut.com/search?q={q}"),
    ("complaintboard.in",     "https://www.complaintboard.in/search?q={q}"),
]


class _Fingerprint(html.parser.HTMLParser):
    """Count repeated tag+class combinations and keep the text under them.

    A complaint listing is always the same element repeated, so the class
    that appears dozens of times with real sentences under it is the one a
    parser would target."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = collections.Counter()
        self.samples = collections.defaultdict(list)
        self.title = ""
        self._stack = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        cls = (a.get("class") or "").strip()
        key = f"{tag}.{cls.split()[0]}" if cls else None
        if key:
            self.counts[key] += 1
        self._stack.append(key)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        text = " ".join(data.split())
        if len(text) < 40:
            return
        for key in reversed(self._stack):
            if key:
                if len(self.samples[key]) < 3:
                    self.samples[key].append(text[:160])
                break


def _probe_forum(name: str, template: str, term: str) -> None:
    url = template.format(q=urllib.parse.quote_plus(term))
    print(f"\n--- {name}")
    print(f"    {url}")
    try:
        resp = httpx.get(url, headers={"User-Agent": BROWSER_UA},
                         timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"    UNREACHABLE: {type(exc).__name__}: {exc}")
        return

    print(f"    http {resp.status_code}  {len(resp.content)} bytes  "
          f"{resp.headers.get('content-type','?').split(';')[0]}")
    if resp.status_code >= 400:
        print(f"    refused: {resp.text[:160]}")
        return
    if "html" not in resp.headers.get("content-type", "").lower():
        print("    not HTML -- skipping structure scan")
        return

    fp = _Fingerprint()
    try:
        fp.feed(resp.text)
    except Exception as exc:                      # malformed markup
        print(f"    could not parse: {exc}")
        return

    print(f"    page title: {fp.title[:90] or '(none)'}")
    # Repeated containers that actually hold sentences are the candidates.
    ranked = [(k, n) for k, n in fp.counts.most_common(40)
              if n >= 3 and fp.samples.get(k)]
    if not ranked:
        print("    no repeated text containers found -- the page is probably "
              "rendered by JavaScript, so a plain fetch will not work.")
        return
    print("    repeated text containers (a parser would target one of these):")
    for key, n in ranked[:6]:
        print(f"      {key:<34} x{n}")
        for s in fp.samples[key][:2]:
            print(f"          | {s}")


def _probe_reddit(reg: Registry, ent, days: int) -> None:
    print(f"\n=== REDDIT -- {ent['name']} (last {days} days)")
    print(f"    user-agent : {reddit_source.USER_AGENT[:70]}")
    print(f"    subreddits : {', '.join(reddit_source.SUBREDDITS)}")
    try:
        items = reddit_source.search(reg, ent["id"], days)
    except reddit_source.RedditUnavailable as exc:
        print(f"\n    COULD NOT RUN: {exc}")
        if reddit_source.LAST_DIAGNOSIS:
            print(f"    diagnosis: {json.dumps(reddit_source.LAST_DIAGNOSIS)[:400]}")
        return

    d = reddit_source.LAST_DIAGNOSIS
    print(f"    query      : {d.get('query')}")
    print(f"    searches   : {d.get('passes')}  "
          f"failed: {len(d.get('errors') or [])}")
    for err in (d.get("errors") or [])[:3]:
        print(f"      ! {err[:150]}")
    print(f"\n    {len(items)} post(s) found\n")
    for it in items[:10]:
        when = (it["published_at"] or "")[:10]
        print(f"      [{when}] {it['source_name']}  {it['title'][:80]}")
        if it["snippet"]:
            print(f"                 {it['snippet'][:110]}")
    if not items:
        print("      (Reddit answered normally but matched nothing. Widen "
              "--days, or the entity may simply not be discussed there.)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="name or alias substring")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--reddit-only", action="store_true")
    ap.add_argument("--forums-only", action="store_true")
    args = ap.parse_args()

    print(f"build        : reddit_source {reddit_source.BUILD}")

    init_db()
    db = connect()
    try:
        rows = q(db, "SELECT * FROM entities ORDER BY name")
        ent = None
        if args.entity:
            needle = args.entity.lower()
            for r in rows:
                hay = (r["name"] + " " + (r["aliases"] or "")).lower()
                if needle in hay:
                    ent = r
                    break
            if ent is None:
                print(f"\nNo entity matched {args.entity!r}. On the roster:")
                for r in rows:
                    print(f"  {r['name']}")
                return 1
        elif not args.forums_only:
            print("\nGive --entity to test Reddit (or --forums-only).")
            return 1

        if not args.forums_only:
            _probe_reddit(Registry(rows), ent, args.days)

        if not args.reddit_only:
            term = (ent["name"] if ent else "hdfc bank")
            print(f"\n\n=== INDIAN COMPLAINT FORUMS -- searching for {term!r}")
            print("    (structure only -- no parser is written for these yet)")
            for name, template in FORUMS:
                _probe_forum(name, template, term)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
