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
import re
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

PROBE_BUILD = "2026-08-26.1-brightdata"

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
        items = forums.search(reg, ent["id"], 365, on_progress=progress)
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
        info = forums.LAST_DIAGNOSIS.get("undated") or {}
        print(f"\n    {undated} of {len(items)} carry no date.")
        if info.get("kinds"):
            kinds = ", ".join(f"{k}: {n}" for k, n in
                              sorted(info["kinds"].items(), key=lambda x: -x[1]))
            print(f"    undated by type : {kinds}")
        bycompany = info.get("bycompany", 0)
        if bycompany:
            print(f"    {bycompany} are /bycompany/ listings, whose date slot "
                  "the site leaves empty on the results page.")
        if (info.get("dropped")):
            print("    undated rows were DROPPED, not stored: inside a "
                  "window an undated complaint cannot be shown to be "
                  "recent, and the dumps proved undated means old here.")


_COMPANY_HREF = re.compile(r'href="(/[a-z0-9-]+-b\d+)(?:#[^"]*)?"')


def _probe_company_page(term: str) -> None:
    """Find the site's own page for this company and try to read it.

    Search results are relevance-ranked, so they surface decade-old
    complaints and near-name companies (HDFC Bank Standard Life). The
    company page is the site's canonical, entity-specific list -- if it
    parses, it beats searching. Its URL is discovered, never guessed:
    from the search page's own links where they exist, and otherwise from
    a matching complaint's page -- the search page only links "verified"
    companies, but every complaint page links the company it was filed
    under.
    """
    print(f"\n\n=== COMPANY PAGE -- discovering from the site's own links")
    try:
        r = httpx.get("https://www.consumercomplaints.in/",
                      params={"search": term},
                      headers={"User-Agent": BROWSER_UA},
                      timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"    UNREACHABLE: {exc}")
        return

    words = [w for w in re.split(r"[^a-z0-9]+", term.lower()) if w]

    def matched(slug: str) -> int:
        return sum(1 for w in words if w in slug)

    counts = collections.Counter(_COMPANY_HREF.findall(r.text))
    slug = None
    if counts:
        ranked = sorted(counts.items(), key=lambda kv: (-matched(kv[0]), -kv[1]))
        print("    on the search page (slug, links, name-words matched):")
        for cand, n in ranked[:4]:
            print(f"      {cand:<44} x{n}  matches {matched(cand)}/{len(words)}")
        # Every word must match: "bank" alone would fetch any bank's page
        # for any other bank, so a generic word is never enough on its own.
        if matched(ranked[0][0]) == len(words):
            slug = ranked[0][0]
    else:
        print("    no company-page links (-b<id>) on the search page.")

    if slug is None:
        print("    none is this entity's own page -- following a complaint "
              "instead, since a complaint page links the company it was "
              "filed under.")
        parser = forums._Results()
        parser.feed(r.text)
        parser.close()
        rows = [row for row in parser.rows
                if all(w in (row.get("title") or "").lower() for w in words)
                and (row.get("href") or "").startswith("/")
                and "/bycompany/" not in (row.get("href") or "")]
        # A dated row is a recent complaint; its company link reflects the
        # page the site files new complaints under today.
        rows.sort(key=lambda row: bool(row.get("date_text") or row.get("date")),
                  reverse=True)
        if not rows:
            print("    no matching complaint rows to follow either.")
            return
        for row in rows[:2]:
            href = "https://www.consumercomplaints.in" + row["href"]
            print(f"    reading {row['href'][:72]}")
            time.sleep(1.5)
            try:
                cp = httpx.get(href, headers={"User-Agent": BROWSER_UA},
                               timeout=30, follow_redirects=True)
            except httpx.HTTPError as exc:
                print(f"      UNREACHABLE: {exc}")
                continue
            c2 = collections.Counter(_COMPANY_HREF.findall(cp.text))
            r2 = sorted(c2.items(), key=lambda kv: (-matched(kv[0]), -kv[1]))
            if r2 and matched(r2[0][0]) == len(words):
                slug = r2[0][0]
                print(f"      it names its company: {slug}  (x{r2[0][1]})")
                break
            if r2:
                print(f"      best link there is {r2[0][0]} -- not a full "
                      "name match")
        if slug is None:
            print("    could not discover this entity's company page without "
                  "guessing -- staying with search results.")
            return

    url = "https://www.consumercomplaints.in" + slug
    print(f"\n    fetching {url}")
    time.sleep(1.5)
    try:
        page = httpx.get(url, headers={"User-Agent": BROWSER_UA},
                         timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"    UNREACHABLE: {exc}")
        return
    print(f"    http {page.status_code}  {len(page.content)} bytes")
    if page.status_code >= 400:
        return

    parser = forums._Results()
    parser.feed(page.text)
    parser.close()
    if parser.rows:
        dated = sum(1 for row in parser.rows
                    if row.get("date") or row.get("date_text")
                    or forums._date_from_region(row.get("region", "")))
        print(f"    the existing parser reads it: {len(parser.rows)} rows, "
              f"{dated} dated")
        for row in parser.rows[:5]:
            d = row.get("date_text") or row.get("date") or "no date"
            print(f"      [{d:<12}] {row.get('title','')[:66]}")
        return
    # Different markup: show what a parser would have to target.
    print("    the search-results parser reads nothing here -- structure:")
    fp = _Fingerprint()
    try:
        fp.feed(page.text)
    except Exception as exc:
        print(f"    could not parse: {exc}")
        return
    ranked_fp = [(k, c) for k, c in fp.counts.most_common(40)
                 if c >= 3 and fp.samples.get(k)]
    for key, c in ranked_fp[:6]:
        print(f"      {key:<34} x{c}")
        for sample in fp.samples[key][:1]:
            print(f"          | {sample}")


def _dump_raw(term: str) -> None:
    """Print the full markup of rows the parser could not date.

    Prefers rows that are NOT /bycompany/ listings: those are already
    explained (their date slot is empty on the results page). What needs
    seeing is a row that shows a date in the browser while the parser
    finds none. Both result pages are fetched, since page 2 held most of
    the undated rows.
    """
    print(f"\n\n=== RAW MARKUP of undated results (term {term!r})")
    chosen: list[tuple[int, dict, str]] = []
    for page in (1, 2):
        if len(chosen) >= 3:
            break
        params = {"search": term}
        if page > 1:
            params["page"] = page
            time.sleep(1.5)
        try:
            r = httpx.get("https://www.consumercomplaints.in/", params=params,
                          headers={"User-Agent": BROWSER_UA},
                          timeout=30, follow_redirects=True)
        except httpx.HTTPError as exc:
            print(f"    page {page} UNREACHABLE: {exc}")
            continue
        parser = forums._Results()
        parser.feed(r.text)
        parser.close()
        undated = [row for row in parser.rows
                   if not row.get("date") and not row.get("date_text")
                   and not forums._date_from_region(row.get("region", ""))]
        preferred = ([row for row in undated if "/bycompany/" not in (row.get("href") or "")]
                     or undated)
        print(f"    page {page}: {len(parser.rows)} rows, {len(undated)} undated, "
              f"{len(preferred)} of interest")
        for row in preferred:
            if len(chosen) >= 3:
                break
            chosen.append((page, row, r.text))

    if not chosen:
        print("    nothing to dump: every parsed row carries a date.")
        return
    for page, row, text in chosen:
        href = row.get("href") or ""
        print(f"\n    --- page {page} | kind {row.get('kind') or '?'} | "
              f"{row.get('title', '')[:64]!r}")
        i = text.find(href)
        if i < 0:
            print("        (row not locatable in the raw page)")
            continue
        # The row's own container opens at the nearest id="s..." marker;
        # the next one closes it. That is the complete complaint block.
        start_i = text.rfind('<div id="s', 0, i)
        if start_i < 0:
            start_i = max(0, i - 500)
        end_i = text.find('<div id="s', i + 1)
        if end_i < 0:
            end_i = i + 2400
        print(text[start_i:min(end_i, start_i + 2600)])


def _probe_brightdata(ent) -> None:
    """Run the real Bright Data collector once, printing every stage and
    one raw record verbatim -- the field map gets corrected against that
    record, never guessed."""
    import json as _json
    from app import brightdata_x as bd
    print(f"\n=== BRIGHT DATA X -- {ent['name']}  (module {bd.BUILD})")
    print(f"    key set    : {'yes' if bd.KEY else 'NO -- set SUCHAK_BRIGHTDATA_KEY in this terminal'}")
    print(f"    dataset id : {bd.DATASET or 'NOT SET -- set SUCHAK_BRIGHTDATA_DATASET (gd_...)'}")
    print(f"    caps       : {bd.MAX_RECORDS} records, wait up to {bd.WAIT_SECONDS}s")
    if not bd.ENABLED:
        return
    aliases = _json.loads(ent["aliases"] or "[]")
    print(f"    search     : {bd.search_url_for(dict(ent), aliases)}")
    try:
        orig_download = bd.download
        raw_box = {}
        def spy(snapshot_id):
            records = orig_download(snapshot_id)
            if records:
                raw_box["rec"] = records[0]
            return records
        bd.download = spy
        try:
            items = bd.collect(dict(ent), aliases, 365)
        finally:
            bd.download = orig_download
    except bd.SnapshotPending as exc:
        print(f"\n    PENDING: {exc}")
        print("    Run this same command again in a few minutes.")
        return
    except Exception as exc:
        print(f"\n    COULD NOT RUN: {type(exc).__name__}: {exc}")
        if bd.LAST_DIAGNOSIS:
            print(f"    diagnosis: {_json.dumps(bd.LAST_DIAGNOSIS)[:300]}")
        return
    d = bd.LAST_DIAGNOSIS
    print(f"\n    snapshot {d.get('snapshot')} -> {d.get('records')} record(s), "
          f"{d.get('parsed')} parsed, {d.get('unparsable')} unparsable")
    if raw_box.get("rec") is not None:
        print("\n    --- first raw record (for the field map):")
        print("    " + _json.dumps(raw_box["rec"], ensure_ascii=False)[:1500])
    print()
    for it in items[:8]:
        print(f"      [{(it['published_at'] or 'no date')[:10]}] "
              f"{it['source_name']}  {it['title'][:80]}")


def _probe_youtube_comments(reg: Registry, ent) -> None:
    """Run the real comments collector once and show what came back."""
    import logging
    logging.basicConfig(level=logging.INFO, format="    %(message)s")
    from app import ingest
    print(f"\n=== YOUTUBE COMMENTS -- {ent['name']}")
    print(f"    key set    : {'yes' if ingest.YOUTUBE_KEY else 'NO -- set SUCHAK_YOUTUBE_KEY in this terminal first'}")
    print(f"    enabled    : {ingest.YT_COMMENTS}  |  up to "
          f"{ingest.YT_COMMENT_VIDEOS} videos x {ingest.YT_COMMENTS_PER_VIDEO} comments,"
          f" cap {ingest.YT_COMMENTS_MAX}")
    if not ingest.YOUTUBE_KEY:
        return
    try:
        items = ingest.fetch_youtube_comments(reg, dict(ent), ingest.SOCIAL_LOOKBACK_DAYS)
    except Exception as exc:
        print(f"\n    COULD NOT RUN: {type(exc).__name__}: {exc}")
        return
    print(f"\n    {len(items)} comment(s) collected\n")
    for it in items[:8]:
        print(f"      [{(it['published_at'] or 'no date')[:10]}] {it['title'][:72]}")
        print(f"                   {it['snippet'][:110]}")
    if not items:
        print("      (No comments matched. Either the entity's recent videos "
              "carry none, or none of the found videos names the entity.)")


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
    # Grouped by source, a couple each. Sorted globally by date, the newest
    # posts are always the site-wide ones -- so the display was ten of
    # those, which made a healthy run look like the curated subreddits had
    # returned junk. What they actually returned is what matters here.
    by_source: dict[str, list] = {}
    for it in items:
        by_source.setdefault(it["source_name"], []).append(it)
    curated = [f"r/{s}" for s in reddit_source.SUBREDDITS]
    ordered = ([s for s in curated if s in by_source]
               + sorted(k for k in by_source if k not in curated))
    for src in ordered:
        posts = by_source[src]
        tag = "" if src in curated else "  (via site-wide)"
        print(f"      {src} -- {len(posts)} of the {len(items)}{tag}")
        for it in posts[:2]:
            when = (it["published_at"] or "no date")[:10]
            print(f"        [{when}] {it['title'][:78]}")
            if it["snippet"]:
                print(f"                     {it['snippet'][:105]}")
    if not items:
        print("      (Reddit answered normally but matched nothing. Widen "
              "--days, or the entity may simply not be discussed there.)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="name or alias substring")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--reddit-only", action="store_true")
    ap.add_argument("--forums-only", action="store_true")
    ap.add_argument("--youtube", action="store_true",
                    help="test YouTube comment collection for the entity")
    ap.add_argument("--brightdata", action="store_true",
                    help="test X collection through Bright Data for the entity")
    ap.add_argument("--routes", action="store_true",
                    help="try every Reddit route and report which answer")
    ap.add_argument("--dump", action="store_true",
                    help="print raw markup around the first forum result")
    ap.add_argument("--structure", action="store_true",
                    help="scan page structure instead of running the parser")
    args = ap.parse_args()

    print(f"build        : probe {PROBE_BUILD}  |  "
          f"reddit_source {reddit_source.BUILD}  |  forums {forums.BUILD}")

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

        if args.brightdata:
            _probe_brightdata(ent)
            return 0

        if args.youtube:
            _probe_youtube_comments(reg, ent)
            return 0

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
                _probe_company_page(term)
            if args.dump:
                _dump_raw(term)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
