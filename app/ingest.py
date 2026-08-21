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


SOURCES = {
    "google_news": fetch_google_news,
    "youtube": fetch_youtube,
}


def ingest_entity(db, entity, registry: Registry | None = None) -> dict:
    """Fetch every enabled source for one entity and store what survives
    disambiguation and de-duplication."""
    registry = registry or load_registry(db)
    result = {"found": 0, "added": 0, "merged": 0, "rejected": 0, "note": None}
    notes = []

    candidates = []
    for name, fetch in SOURCES.items():
        try:
            got = fetch(registry, entity)
            candidates.extend(got)
            if name == "youtube" and got:
                notes.append(f"{len(got)} from youtube")
        except Exception as exc:
            msg = f"{name} failed: {type(exc).__name__}: {exc}"
            notes.append(msg)
            log.warning("%s for %s", msg, entity["name"])

    window_start = (datetime.now(timezone.utc) - timedelta(days=DUP_WINDOW_DAYS)).isoformat()
    recent = [dict(r) for r in q(
        db, "SELECT id, title FROM items WHERE entity_id = ? AND created_at >= ?",
        (entity["id"], window_start))]

    for cand in candidates:
        result["found"] += 1
        title, link = cand["title"], cand["url"]

        # Free disambiguation, before anything is stored or classified:
        # "State Bank of India ..." must not be filed under Bank of India.
        if not registry.mentions(entity["id"], f"{title} {cand['snippet']}"):
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
        dup_id, best = None, 0.0
        for r in recent:
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
            recent.append({"id": new_id, "title": title})
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
              "rejected": 0, "classified": 0}
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
                for k in ("found", "added", "merged", "rejected"):
                    totals[k] += r[k]
            totals["classified"] = classify_new_items(db)
        finally:
            db.close()
    finally:
        fetch_lock.release()
    totals["seconds"] = round(time.monotonic() - started, 1)
    log.info("Fetch cycle done: %s", totals)
    return totals
