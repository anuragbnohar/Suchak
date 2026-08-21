"""Ingestion: Google News RSS per entity.

One source type keeps the week-one prototype simple — Google News already
aggregates the Indian financial press (ET, Mint, Business Standard,
Moneycontrol, regional outlets), so per-outlet feeds add little at this
stage. New source types plug in as functions that yield the same
normalized dicts.
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


def ingest_entity(db, entity, registry: Registry | None = None) -> dict:
    registry = registry or load_registry(db)
    url = google_news_url(registry, entity["id"])
    result = {"found": 0, "added": 0, "merged": 0, "rejected": 0, "note": None}
    try:
        resp = httpx.get(
            url, timeout=20, follow_redirects=True,
            headers={"User-Agent": "Suchak/0.1 (supervisory prototype)"},
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as exc:
        result["note"] = f"fetch failed: {type(exc).__name__}: {exc}"
        log.warning("Fetch failed for %s: %s", entity["name"], exc)
        _log_fetch(db, entity["id"], result)
        return result

    window_start = (datetime.now(timezone.utc) - timedelta(days=DUP_WINDOW_DAYS)).isoformat()
    recent = q(
        db,
        "SELECT id, title FROM items WHERE entity_id = ? AND created_at >= ?",
        (entity["id"], window_start),
    )
    recent = [dict(r) for r in recent]

    for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
        title = _strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        result["found"] += 1

        # Free disambiguation, before anything is stored or classified:
        # "State Bank of India ..." must not be filed under Bank of India.
        summary_text = _strip_html(entry.get("summary", ""))
        if not registry.mentions(entity["id"], f"{title} {summary_text}"):
            result["rejected"] += 1
            continue

        if one(db, "SELECT 1 FROM items WHERE entity_id=? AND url=?", (entity["id"], link)):
            continue
        if one(db, "SELECT 1 FROM item_sources s JOIN items i ON i.id=s.item_id"
                   " WHERE i.entity_id=? AND s.url=?", (entity["id"], link)):
            continue

        source_name = _entry_source(entry, link)
        published = _entry_published(entry)
        snippet = summary_text[:500]

        dup_id, best = None, 0.0
        for r in recent:
            score = title_similarity(title, r["title"])
            if score > best:
                dup_id, best = r["id"], score
        if dup_id and best >= DUP_THRESHOLD:
            x(db, "INSERT INTO item_sources (item_id, url, source_name, title, published_at)"
                  " VALUES (?,?,?,?,?)", (dup_id, link, source_name, title, published))
            result["merged"] += 1
        else:
            new_id = x(
                db,
                "INSERT INTO items (entity_id, title, url, source_name, snippet, published_at)"
                " VALUES (?,?,?,?,?,?)",
                (entity["id"], title, link, source_name, snippet,
                 published or datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            recent.append({"id": new_id, "title": title})
            result["added"] += 1

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
