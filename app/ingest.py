"""Ingestion: pluggable sources, one normalized item shape.

Each source is a function taking (registry, entity) and returning a list of
candidate dicts with the same keys. Everything downstream — disambiguation,
de-duplication, screening, classification — is source-agnostic, so adding a
source means adding one function and one entry in SOURCES.

Live sources:
  google_news  Google News RSS. Free, no key. An aggregator, so one query
               reaches the whole Indian financial press.
  youtube      YouTube Data API v3 search. Needs a free API key in
               SUCHAK_YOUTUBE_KEY; skipped silently when unset.
  x            X/Twitter recent search, scoped to customer complaints.
               Needs a bearer token in SUCHAK_X_BEARER; skipped silently
               when unset. This is the ONLY paid source, billed per post
               returned, so it is capped hard -- see SUCHAK_X_MAX_POSTS.
"""
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .classify import classify_new_items
from .db import connect, one, q, x
from .matching import Registry, build_query
from .similarity import title_similarity

log = logging.getLogger("suchak.ingest")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
# Google News returns at most ~100 results per query, so this is the ceiling
# rather than a throttle.
MAX_ENTRIES_PER_FEED = int(os.environ.get("SUCHAK_MAX_ENTRIES", "100"))
# How far back to ask for news. 0 = whatever Google considers current.
LOOKBACK_DAYS = int(os.environ.get("SUCHAK_LOOKBACK_DAYS", "30"))
# Pause between entity feeds so a 33-entity sweep is not seen as abuse.
FETCH_DELAY_SECONDS = float(os.environ.get("SUCHAK_FETCH_DELAY", "1.5"))
DUP_WINDOW_DAYS = 7
DUP_THRESHOLD = 0.6

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_KEY = os.environ.get("SUCHAK_YOUTUBE_KEY", "")
# search.list costs 100 quota units; the free daily allowance is 10,000, so
# 33 banks cost 3,300 units per sweep. maxResults is capped at 50 by the API.
YOUTUBE_MAX_RESULTS = min(int(os.environ.get("SUCHAK_YOUTUBE_MAX", "25")), 50)

# --- X / Twitter -------------------------------------------------------------
# The only source that costs money, and it bills per post RETURNED, so the
# query is written to be narrow and the result count is capped hard. Recent
# search only covers the last 7 days regardless of SUCHAK_LOOKBACK_DAYS.
X_SEARCH = "https://api.x.com/2/tweets/search/recent"
X_BEARER = os.environ.get("SUCHAK_X_BEARER", "")
# Hard ceiling on posts per entity per sweep. At $0.005/post this is your
# spend control: 100 posts = $0.50 per bank per sweep, whatever happens.
X_MAX_POSTS = max(10, min(int(os.environ.get("SUCHAK_X_MAX_POSTS", "100")), 1000))
X_PRICE_PER_POST = float(os.environ.get("SUCHAK_X_PRICE_PER_POST", "0.005"))
# complaints | care_handle | both
X_STRATEGY = os.environ.get("SUCHAK_X_STRATEGY", "complaints")
X_LANGS = [c.strip() for c in os.environ.get("SUCHAK_X_LANGS", "en,hi").split(",") if c.strip()]
X_RECENT_SEARCH_DAYS = 7

# Vocabulary that marks a post as a grievance rather than market chatter.
# Kept tight on purpose: every extra term returns more posts, and every post
# returned is billed.
X_COMPLAINT_TERMS = [t.strip() for t in os.environ.get(
    "SUCHAK_X_COMPLAINT_TERMS",
    'complaint,grievance,fraud,cheated,refund,unauthorized,debited,blocked,'
    'harassment,mis-sold,misselling,"not working","no response","customer care",'
    '"worst service"'
).split(",") if t.strip()]

# only one fetch cycle at a time
fetch_lock = threading.Lock()


def load_registry(db) -> Registry:
    return Registry(q(db, "SELECT * FROM entities"))


def google_news_url(registry: Registry, entity_id: int) -> str:
    """Feed URL for one entity: its aliases, minus the longer names that
    contain them, limited to the lookback window."""
    query = build_query(registry, entity_id, days=LOOKBACK_DAYS or None)
    return GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query))


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(text or "")).strip()


def _entry_published(entry) -> str | None:
    parsed = entry.get("published_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
    return None


def _entry_source(entry, link: str) -> str:
    src = entry.get("source")
    if src and src.get("title"):
        return src["title"]
    try:
        return urllib.parse.urlparse(link).netloc or "unknown"
    except ValueError:
        return "unknown"


def fetch_google_news(registry: Registry, entity) -> list[dict]:
    """Google News RSS: free, no key, aggregates the Indian press."""
    url = google_news_url(registry, entity["id"])
    resp = httpx.get(url, timeout=20, follow_redirects=True,
                     headers={"User-Agent": "Suchak/0.1 (supervisory prototype)"})
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)

    items = []
    for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
        title = _strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "source_name": _entry_source(entry, link),
            "snippet": _strip_html(entry.get("summary", "")),
            "published_at": _entry_published(entry),
            "source_type": "news",
        })
    return items


def fetch_youtube(registry: Registry, entity) -> list[dict]:
    """YouTube Data API v3 search.

    Video titles and descriptions are short and promotional, so the free
    disambiguation layer matters more here than for news: a video merely
    naming several banks should not be filed under all of them.
    """
    if not YOUTUBE_KEY:
        return []
    params = {
        "key": YOUTUBE_KEY,
        "q": build_query(registry, entity["id"], or_token="|"),
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": YOUTUBE_MAX_RESULTS,
        "regionCode": "IN",
    }
    if LOOKBACK_DAYS:
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    resp = httpx.get(YOUTUBE_SEARCH, params=params, timeout=20)
    if resp.status_code == 403:
        # quota exhausted or key not enabled: report, do not crash the sweep
        raise RuntimeError(f"YouTube API refused the request: {resp.text[:200]}")
    resp.raise_for_status()

    items = []
    for entry in resp.json().get("items", []):
        video_id = (entry.get("id") or {}).get("videoId")
        snip = entry.get("snippet") or {}
        title = _strip_html(snip.get("title", ""))
        if not video_id or not title:
            continue
        items.append({
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source_name": snip.get("channelTitle") or "YouTube",
            "snippet": _strip_html(snip.get("description", "")),
            "published_at": snip.get("publishedAt"),
            "source_type": "video",
        })
    return items


def x_query(registry: Registry, entity) -> str:
    """Build a narrow complaint query.

    Every clause here exists to reduce the number of posts returned, because
    that is exactly what X bills for. Retweets and promoted posts are
    excluded outright; the entity terms are ANDed with complaint vocabulary
    so market chatter and news reposts do not come back.
    """
    ent = registry.entities[entity["id"]]
    handle = ent.get("handle") or ""
    names = " OR ".join(f'"{a}"' for a in ent["aliases"][:2])
    complaints = " OR ".join(X_COMPLAINT_TERMS)

    if X_STRATEGY == "care_handle":
        if not handle:
            raise RuntimeError(
                f"strategy 'care_handle' needs an x_handle for {entity['name']}")
        core = f"to:{handle}"
    elif X_STRATEGY == "both" and handle:
        core = f"(to:{handle} OR (({names}) ({complaints})))"
    else:
        core = f"(({names}) ({complaints}))"

    query = f"{core} -is:retweet -is:nullcast"
    if X_LANGS:
        langs = " OR ".join(f"lang:{c}" for c in X_LANGS)
        query += f" ({langs})" if len(X_LANGS) > 1 else f" {langs}"
    return query


def fetch_x(registry: Registry, entity) -> list[dict]:
    """X/Twitter recent search, scoped to customer complaints.

    Paid, billed per post returned. One request per entity per sweep, never
    paginated, so the cost per sweep is bounded by X_MAX_POSTS and cannot
    run away if a query turns out broader than expected.
    """
    if not X_BEARER:
        return []

    since = datetime.now(timezone.utc) - timedelta(
        days=min(LOOKBACK_DAYS or X_RECENT_SEARCH_DAYS, X_RECENT_SEARCH_DAYS))
    params = {
        "query": x_query(registry, entity),
        "max_results": min(X_MAX_POSTS, 100),   # API ceiling per request
        "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tweet.fields": "created_at,lang,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    resp = httpx.get(X_SEARCH, params=params, timeout=20,
                     headers={"Authorization": f"Bearer {X_BEARER}"})
    if resp.status_code in (401, 403):
        raise RuntimeError(f"X rejected the credentials: {resp.text[:200]}")
    if resp.status_code == 429:
        raise RuntimeError("X rate limit reached; try again later")
    resp.raise_for_status()

    payload = resp.json()
    posts = payload.get("data") or []
    users = {u["id"]: u for u in (payload.get("includes", {}).get("users") or [])}

    items = []
    for post in posts[:X_MAX_POSTS]:
        text = " ".join((post.get("text") or "").split())
        if not text:
            continue
        author = users.get(post.get("author_id"), {})
        username = author.get("username") or "i"
        # A post has no headline, so the first line stands in as the title
        # and the full text becomes the snippet the classifier reads.
        title = text if len(text) <= 120 else text[:117].rstrip() + "..."
        items.append({
            "title": title,
            "url": f"https://x.com/{username}/status/{post['id']}",
            "source_name": f"@{username}",
            "snippet": text,
            "published_at": post.get("created_at"),
            "source_type": "social",
            # Targeting the bank's own grievance handle establishes
            # attribution by itself; such posts rarely spell out the bank
            # name, so the usual name check would wrongly discard them.
            "attribution_confident": X_STRATEGY == "care_handle",
            "billed": True,
        })
    return items


SOURCES = {
    "google_news": fetch_google_news,
    "youtube": fetch_youtube,
    "x": fetch_x,
}


def ingest_entity(db, entity, registry: Registry | None = None) -> dict:
    """Fetch every enabled source for one entity and store what survives
    disambiguation and de-duplication."""
    registry = registry or load_registry(db)
    result = {"found": 0, "added": 0, "merged": 0, "rejected": 0,
              "billed": 0, "note": None}
    notes = []

    candidates = []
    for name, fetch in SOURCES.items():
        try:
            got = fetch(registry, entity)
            candidates.extend(got)
            billed = sum(1 for c in got if c.get("billed"))
            if billed:
                result["billed"] += billed
                notes.append(f"{billed} paid posts from {name} "
                             f"(~${billed * X_PRICE_PER_POST:.2f})")
            elif name != "google_news" and got:
                notes.append(f"{len(got)} from {name}")
        except Exception as exc:
            msg = f"{name} failed: {type(exc).__name__}: {exc}"
            notes.append(msg)
            log.warning("%s for %s", msg, entity["name"])

    window_start = (datetime.now(timezone.utc) - timedelta(days=DUP_WINDOW_DAYS)).isoformat()
    recent = [dict(r) for r in q(
        db, "SELECT id, title, source_type FROM items"
            " WHERE entity_id = ? AND created_at >= ?",
        (entity["id"], window_start))]

    for cand in candidates:
        result["found"] += 1
        title, link = cand["title"], cand["url"]

        # Free disambiguation, before anything is stored or classified:
        # "State Bank of India ..." must not be filed under Bank of India.
        if not cand.get("attribution_confident") and \
                not registry.mentions(entity["id"], f"{title} {cand['snippet']}"):
            result["rejected"] += 1
            continue

        if one(db, "SELECT 1 FROM items WHERE entity_id=? AND url=?", (entity["id"], link)):
            continue
        if one(db, "SELECT 1 FROM item_sources s JOIN items i ON i.id=s.item_id"
                   " WHERE i.entity_id=? AND s.url=?", (entity["id"], link)):
            continue

        published = cand["published_at"] or datetime.now(timezone.utc).isoformat(timespec="seconds")

        # A video and an article about the same event are the same event, so
        # duplicate detection deliberately spans source types.
        #
        # Social posts are the exception: ten customers complaining about
        # blocked cards is ten data points, not one story covered ten times.
        # Volume IS the conduct signal, so near-identical complaints are kept
        # apart and only the exact-URL check above suppresses true repeats.
        dup_id, best = None, 0.0
        if cand["source_type"] != "social":
            for r in recent:
                if r.get("source_type") == "social":
                    continue
                score = title_similarity(title, r["title"])
                if score > best:
                    dup_id, best = r["id"], score
        if dup_id and best >= DUP_THRESHOLD:
            x(db, "INSERT INTO item_sources (item_id, url, source_name, title, published_at)"
                  " VALUES (?,?,?,?,?)",
              (dup_id, link, cand["source_name"], title, published))
            result["merged"] += 1
        else:
            new_id = x(
                db,
                "INSERT INTO items (entity_id, title, url, source_name, snippet,"
                " published_at, source_type) VALUES (?,?,?,?,?,?,?)",
                (entity["id"], title, link, cand["source_name"],
                 cand["snippet"][:500], published, cand["source_type"]),
            )
            recent.append({"id": new_id, "title": title,
                           "source_type": cand["source_type"]})
            result["added"] += 1

    result["note"] = "; ".join(notes) or None
    _log_fetch(db, entity["id"], result)
    return result


def _log_fetch(db, entity_id: int, result: dict) -> None:
    note = result["note"]
    if result.get("rejected"):
        rejected = f"{result['rejected']} rejected as another entity's news"
        note = f"{note}; {rejected}" if note else rejected
    x(db, "INSERT INTO fetch_log (entity_id, source, found, added, merged, note)"
          " VALUES (?,?,?,?,?,?)",
      (entity_id, "google_news_rss", result["found"], result["added"],
       result["merged"], note))


def run_cycle(entity_id: int | None = None) -> dict:
    """Fetch (all or one entity) then classify anything new. Opens its own
    DB connection — safe to call from a background thread."""
    if not fetch_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a fetch cycle is already running"}
    started = time.monotonic()
    totals = {"entities": 0, "found": 0, "added": 0, "merged": 0,
              "rejected": 0, "billed": 0, "classified": 0}
    try:
        db = connect()
        try:
            if entity_id:
                entities = q(db, "SELECT * FROM entities WHERE id = ?", (entity_id,))
            else:
                entities = q(db, "SELECT * FROM entities ORDER BY id")
            registry = load_registry(db)
            for n, entity in enumerate(entities):
                if n and FETCH_DELAY_SECONDS:
                    time.sleep(FETCH_DELAY_SECONDS)
                r = ingest_entity(db, entity, registry)
                totals["entities"] += 1
                for k in ("found", "added", "merged", "rejected", "billed"):
                    totals[k] += r[k]
            totals["classified"] = classify_new_items(db)
        finally:
            db.close()
    finally:
        fetch_lock.release()
    totals["seconds"] = round(time.monotonic() - started, 1)
    if totals["billed"]:
        totals["estimated_cost_usd"] = round(totals["billed"] * X_PRICE_PER_POST, 2)
    log.info("Fetch cycle done: %s", totals)
    return totals
