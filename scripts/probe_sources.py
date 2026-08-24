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
import time
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

from app import forums, reddit_source
from app.db import connect, init_db, q
from app.matching import Registry

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Indian consumer-complaint sites that carry retail banking grievances.
# The {q} slot is the entity search term.
# grahakseva.com and complaintboard.in were dropped after a probe run from
# a real connection: neither domain resolves any more. mouthshut.com answers
# only with a redirect loop on this URL shape. consumercomplaints.in is the
# one that serves results, and it is the one with a parser.
FORUMS = [
    ("consumercomplaints.in", "https://www.consumercomplaints.in/?search={q}"),
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


REDDIT_VARIANTS = [
    ("www + project UA",   "https://www.reddit.com/search.json", None),
    ("www + browser UA",   "https://www.reddit.com/search.json", BROWSER_UA),
    ("old + browser UA",   "https://old.reddit.com/search.json", BROWSER_UA),
    ("www RSS + browser",  "https://www.reddit.com/search.rss",  BROWSER_UA),
    ("old RSS + browser",  "https://old.reddit.com/search.rss",  BROWSER_UA),
    ("r/india .json",      "https://www.reddit.com/r/india/search.json", BROWSER_UA),
]


def _probe_reddit_variants(term: str) -> None:
    """Reddit refuses some routes and not others. Try each and report which
    answers, so the collector can be pointed at one that works instead of
    guessing at the User-Agent."""
    print(f"\n\n=== REDDIT ROUTES -- which ones answer at all? (term {term!r})")
    print("    (a 200 with JSON or XML means this route is usable)")
    working = []
    for label, url, ua in REDDIT_VARIANTS:
        headers = {"User-Agent": ua or reddit_source.USER_AGENT}
        params = {"q": term, "limit": 5, "raw_json": 1}
        try:
            r = httpx.get(url, params=params, headers=headers,
                          timeout=25, follow_redirects=True)
        except httpx.HTTPError as exc:
            print(f"    {label:<20} UNREACHABLE {type(exc).__name__}")
            continue
        ctype = r.headers.get("content-type", "?").split(";")[0]
        note = ""
        if r.status_code == 200:
            if "json" in ctype:
                try:
                    n = len((r.json().get("data") or {}).get("children") or [])
                    note = f"  {n} post(s)"
                    if n:
                        working.append(label)
                except ValueError:
                    note = "  (unreadable JSON)"
            elif "xml" in ctype or "rss" in ctype:
                n = r.text.count("<entry")
                note = f"  {n} entr(ies)"
                if n:
                    working.append(label)
        print(f"    {label:<20} http {r.status_code}  {ctype}{note}")
        time.sleep(1.5)
    print()
    if working:
        print(f"    USABLE: {', '.join(working)}")
    else:
        print("    No route answered with data. Reddit is blocking this "
              "network for anonymous reads.")


def _probe_forum_parser(reg: Registry, ent) -> None:
    """Run the real parser against the live site, so the probe reports what
    the app would actually collect rather than page structure."""
    print(f"\n\n=== CONSUMERCOMPLAINTS.IN PARSER -- {ent['name']}")
    print(f"    build {forums.BUILD}")

    def progress(label, outcome):
        if outcome == "searching":
            print(f"      {label:<10} ...", end="", flush=True)
        else:
            print(f" {outcome}", flush=True)

    try:
        items = forums.search(reg, ent["id"], None, on_progress=progress)
    except forums.ForumUnavailable as exc:
        print(f"\n    COULD NOT RUN: {exc}")
        print(f"    diagnosis: {json.dumps(forums.LAST_DIAGNOSIS)[:300]}")
        return
    print(f"\n    search term: {forums.LAST_DIAGNOSIS.get('term')!r}")
    print(f"    {len(items)} complaint(s) parsed\n")
    undated = sum(1 for i in items if not i["published_at"])
    for it in items[:8]:
        print(f"      [{(it['published_at'] or 'no date')[:10]}] {it['title'][:74]}")
        if it["snippet"]:
            print(f"                   {it['snippet'][:100]}")
    if undated:
        print(f"\n    {undated} of {len(items)} carry no date. If that is all "
              "of them the date element differs from <time datetime=...>; "
              "run with --dump to show the raw markup of one complaint.")


def _dump_raw(term: str) -> None:
    """Print the markup of a result the parser could not date.

    Dumping the first result is close to useless: the first ones are the
    ones that already work. The interesting row is one the parser read but
    found no date on, so that is the one this looks for.
    """
    print(f"\n\n=== RAW MARKUP of an undated result (term {term!r})")
    try:
        r = httpx.get("https://www.consumercomplaints.in/",
                      params={"search": term},
                      headers={"User-Agent": BROWSER_UA},
                      timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"    UNREACHABLE: {exc}")
        return

    parser = forums._Results()
    parser.feed(r.text)
    parser.close()
    if not parser.rows:
        i = r.text.find(forums.TITLE_CLASS)
        if i < 0:
            print("    marker class not present -- the layout has changed.")
            print("   ", r.text[:600].replace("\n", " "))
        else:
            print(r.text[max(0, i - 700): i + 900])
        return

    undated = [row for row in parser.rows
               if not row.get("date") and not row.get("date_text")]
    print(f"    {len(parser.rows)} rows parsed, {len(undated)} without a date")
    row = (undated or parser.rows)[0]
    print(f"    showing: {row.get('title', '')[:70]!r}")
    print(f"    href   : {row.get('href')}")

    # Anchor on this row's own link so the right block is shown.
    i = r.text.find(row.get("href") or "")
    if i < 0:
        print("    could not locate that row in the raw page.")
        return
    print()
    print(r.text[max(0, i - 400): i + 2200])


def _probe_reddit(reg: Registry, ent, days: int) -> None:
    print(f"\n=== REDDIT -- {ent['name']} (last {days} days)")
    print(f"    user-agent : {reddit_source.USER_AGENT[:70]}")
    print(f"    subreddits : {', '.join(reddit_source.SUBREDDITS)}")
    print(f"    {reddit_source.PAUSE_SECONDS:.0f}s pause between searches, "
          f"{len(reddit_source.SUBREDDITS) + 1} searches -- about "
          f"{int((len(reddit_source.SUBREDDITS) + 1) * (reddit_source.PAUSE_SECONDS + 2))}s\n")

    def progress(label, outcome):
        if outcome == "searching":
            print(f"      {label:<22} ...", end="", flush=True)
        else:
            print(f" {outcome}", flush=True)

    try:
        items = reddit_source.search(reg, ent["id"], days, on_progress=progress)
    except reddit_source.RedditUnavailable as exc:
        print(f"\n    COULD NOT RUN: {exc}")
        if reddit_source.LAST_DIAGNOSIS:
            print(f"    diagnosis: {json.dumps(reddit_source.LAST_DIAGNOSIS)[:400]}")
        return

    d = reddit_source.LAST_DIAGNOSIS
    print(f"    query      : {d.get('query')}")
    print(f"    searches   : {d.get('passes')}  "
          f"failed: {len(d.get('errors') or [])}")
    per = d.get("per_source") or {}
    if per:
        print("    per source : " + ", ".join(f"{k} {v}" for k, v in per.items()))
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
    ap.add_argument("--routes", action="store_true",
                    help="try every Reddit route and report which answer")
    ap.add_argument("--dump", action="store_true",
                    help="print raw markup around the first forum result")
    ap.add_argument("--structure", action="store_true",
                    help="scan page structure instead of running the parser")
    args = ap.parse_args()

    print(f"build        : reddit_source {reddit_source.BUILD}  |  "
          f"forums {forums.BUILD}")

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
        elif args.structure or args.dump:
            # These two only need a search term, not a roster entry.
            pass
        else:
            print("\nGive --entity. On the roster:")
            for r in rows:
                print(f"  {r['name']}")
            return 1

        reg = Registry(rows)
        term = "HDFC Bank"
        if ent:
            # The site indexes companies by common name, so the first alias
            # finds the page where the legal name ("... Ltd.") may not.
            names = json.loads(ent["aliases"] or "[]") or [ent["name"]]
            term = names[0]

        if not args.forums_only:
            _probe_reddit(reg, ent, args.days)
            if args.routes:
                _probe_reddit_variants(term)

        if not args.reddit_only:
            if args.structure:
                print(f"\n\n=== FORUM STRUCTURE -- searching for {term!r}")
                for name, template in FORUMS:
                    _probe_forum(name, template, term)
            elif ent:
                _probe_forum_parser(reg, ent)
            if args.dump:
                _dump_raw(term)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
