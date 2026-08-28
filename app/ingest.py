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

Broadcast sources are different in kind: one feed covers every regulated
entity, so they are fetched ONCE per sweep and each item is routed to the
entities it mentions via the same longest-match registry that keeps news
attribution honest. All free.

  rbi          RBI press releases RSS (enforcement actions, penalties).
  nse          NSE corporate announcements RSS.
  bse          BSE corporate announcements JSON (the endpoint the BSE
               website itself uses; there is no separate documented API).
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
from .matching import Registry, build_query, near_miss
from .similarity import (alias_tokens, distinctive_overlap, event_similarity,
                         strip_publisher)
from . import forums, reddit_source, x_scrape
from .trust import load_trusted_norms, tier_for

log = logging.getLogger("suchak.ingest")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
# Google News publishes a separate edition per language, and an entity is
# searched in each language configured on it. Codes not listed here are
# skipped with a warning rather than guessed at.
NEWS_EDITIONS = {
    "en": ("en-IN", "IN", "IN:en"),
    "hi": ("hi", "IN", "IN:hi"),
    "mr": ("mr", "IN", "IN:mr"),
    "gu": ("gu", "IN", "IN:gu"),
    "bn": ("bn", "IN", "IN:bn"),
    "ta": ("ta", "IN", "IN:ta"),
    "te": ("te", "IN", "IN:te"),
    "kn": ("kn", "IN", "IN:kn"),
    "ml": ("ml", "IN", "IN:ml"),
}
DEFAULT_LANGUAGES = ["en"]
# How many of an entity's aliases go into the search query.
NEWS_QUERY_ALIASES = int(os.environ.get("SUCHAK_QUERY_ALIASES", "6"))
# Google News returns at most ~100 results per query, so this is the ceiling
# rather than a throttle.
MAX_ENTRIES_PER_FEED = int(os.environ.get("SUCHAK_MAX_ENTRIES", "100"))
# How far back each feed asks for news, on every fetch. Fetching is
# incremental -- already-stored URLs are skipped and re-reported stories
# merge into the item they duplicate -- so a rolling window costs only
# what is genuinely new. Raise it for a one-off backfill. 0 = whatever
# the source considers current.
LOOKBACK_DAYS = int(os.environ.get("SUCHAK_LOOKBACK_DAYS", "7"))
# Windows offered next to each Fetch button. A single fetch can widen its own
# window without changing the default for anything else -- useful for a small
# entity that goes quiet for weeks, where the standing 7 days says nothing.
LOOKBACK_CHOICES = (7, 30, 90, 365)

# How many near-miss rejects per entity per fetch may be settled by
# reading the article itself. The headline says "Jalna Co-op Bank"; the
# body says the full registered name; only the body knows.
BODY_CHECKS = max(0, min(int(os.environ.get("SUCHAK_BODY_CHECKS", "3")), 10))

# Social complaints age differently from news: a complaint forum's value is
# the pattern across months, and a bank the size of these gets few posts a
# week, so the news window (7 days by default) would return almost nothing.
# One year, always -- the lookback picker widens news, not this.
SOCIAL_LOOKBACK_DAYS = int(os.environ.get("SUCHAK_SOCIAL_LOOKBACK_DAYS", "365"))


def effective_days(days: int | None) -> int:
    """The window this fetch should use: a valid override, else the default."""
    return days if days in LOOKBACK_CHOICES else LOOKBACK_DAYS
# Pause between entity feeds so a 33-entity sweep is not seen as abuse.
FETCH_DELAY_SECONDS = float(os.environ.get("SUCHAK_FETCH_DELAY", "1.5"))
DUP_WINDOW_DAYS = 7
# Calibrated on real headline variants of one event vs. different events:
# same-event pairs score 0.35-0.60 here, different events under 0.31. The
# overlap floor stops a high cosine built on one or two shared words.
DUP_THRESHOLD = 0.40
DUP_MIN_SHARED = 3

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_KEY = os.environ.get("SUCHAK_YOUTUBE_KEY", "")
# search.list costs 100 quota units; the free daily allowance is 10,000, so
# 33 banks cost 3,300 units per sweep. maxResults is capped at 50 by the API.
YOUTUBE_COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
# Comments under an entity's videos are grievance-bearing social text, so
# they run in the social channel. A search.list call costs 100 quota units
# -- the expensive part -- while reading a video's comments costs 1, so a
# comments run is one search plus pennies.
YT_COMMENTS = os.environ.get("SUCHAK_YT_COMMENTS", "1").strip().lower() not in ("0", "false", "no")
YT_COMMENT_VIDEOS = max(1, min(int(os.environ.get("SUCHAK_YT_COMMENT_VIDEOS", "6")), 15))
YT_COMMENTS_PER_VIDEO = max(5, min(int(os.environ.get("SUCHAK_YT_COMMENTS_PER_VIDEO", "20")), 100))
YT_COMMENTS_MAX = max(10, min(int(os.environ.get("SUCHAK_YT_COMMENTS_MAX", "60")), 200))

YOUTUBE_MAX_RESULTS = min(int(os.environ.get("SUCHAK_YOUTUBE_MAX", "25")), 50)

# --- X / Twitter -------------------------------------------------------------
# The only source that costs money, and it bills per post RETURNED, so the
# query is written to be narrow and the result count is capped hard. Recent
# search only covers the last 7 days regardless of SUCHAK_LOOKBACK_DAYS.
X_SEARCH = "https://api.x.com/2/tweets/search/recent"
# X is OFF unless explicitly switched on -- a bearer token alone is not
# enough, so a token left in the environment can never start spending by
# accident. Set SUCHAK_X_ENABLED=1 (and a token) to turn it back on.
X_ENABLED = os.environ.get("SUCHAK_X_ENABLED", "").strip().lower() in ("1", "true", "yes")
X_BEARER = os.environ.get("SUCHAK_X_BEARER", "")
# Hard ceiling on posts per entity per sweep. At $0.005/post this is your
# spend control: 100 posts = $0.50 per bank per sweep, whatever happens.
X_MAX_POSTS = max(10, min(int(os.environ.get("SUCHAK_X_MAX_POSTS", "50")), 1000))
X_PRICE_PER_POST = float(os.environ.get("SUCHAK_X_PRICE_PER_POST", "0.005"))
# complaints | care_handle | both
# care_handle: posts addressed TO the entity's grievance handle. That is
# where customers actually complain, and it needs no name matching -- nobody
# writes out "HDFC Bank Ltd." when replying to @HDFCBank_Cares.
X_STRATEGY = os.environ.get("SUCHAK_X_STRATEGY", "care_handle")
X_LANGS = [c.strip() for c in os.environ.get("SUCHAK_X_LANGS", "en,hi").split(",") if c.strip()]
X_RECENT_SEARCH_DAYS = 7

# --- Broadcast feeds: RBI + exchanges ---------------------------------------
# One feed covers all entities; fetched once per sweep, items routed by
# mention. Set any URL to "" to disable that source. These hosts were not
# reachable from the build sandbox, so verify each URL once on first run --
# a failure shows up on the Entities page rather than crashing the sweep.
RBI_PRESS_RSS = os.environ.get(
    "SUCHAK_RBI_RSS", "https://www.rbi.org.in/pressreleases_rss.xml")
NSE_ANN_RSS = os.environ.get(
    "SUCHAK_NSE_RSS",
    "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml")
BSE_ANN_API = os.environ.get(
    "SUCHAK_BSE_API", "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w")
# Items taken per broadcast feed per sweep.
BROADCAST_MAX = int(os.environ.get("SUCHAK_BROADCAST_MAX", "200"))

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

# Vocabulary that marks a post as a grievance rather than market chatter.
# Kept tight on purpose: every extra term returns more posts, and every post
# returned is billed.
X_COMPLAINT_TERMS = [t.strip() for t in os.environ.get(
    "SUCHAK_X_COMPLAINT_TERMS",
    'complaint,grievance,fraud,cheated,refund,unauthorized,debited,blocked,'
    'harassment,mis-sold,misselling,"not working","no response","customer care",'
    '"worst service"'
).split(",") if t.strip()]

# source types whose items never merge by title similarity (see dedup note)
NO_MERGE_TYPES = {"social", "filing"}

# only one fetch cycle at a time
fetch_lock = threading.Lock()


def load_registry(db) -> Registry:
    return Registry(q(db, "SELECT * FROM entities"))


def entity_languages(entity) -> list[str]:
    """Languages configured on this entity, falling back to English. Unknown
    codes are dropped here so one bad value cannot break a whole sweep."""
    try:
        raw = json.loads(entity["languages"] or "[]")
    except (KeyError, IndexError, TypeError, ValueError):
        raw = []
    langs = [c for c in raw if c in NEWS_EDITIONS]
    for c in raw:
        if c not in NEWS_EDITIONS:
            log.warning("Unknown news language %r on %s -- skipped",
                        c, entity["name"])
    return langs or DEFAULT_LANGUAGES


def aliases_for_language(aliases: list[str], lang: str) -> list[str]:
    """Put the spellings that suit this edition first. Only the first few
    aliases reach the query, so an entity carrying both Latin and Devanagari
    forms would otherwise send Latin ones to the Marathi edition and never
    search in the script that edition indexes."""
    native = [a for a in aliases if not a.isascii()]
    latin = [a for a in aliases if a.isascii()]
    return latin + native if lang == "en" else native + latin


def google_news_url(registry: Registry, entity_id: int, lang: str = "en",
                    days: int | None = None,
                    aliases: list[str] | None = None) -> str:
    """Feed URL for one entity in one language: its aliases, minus the longer
    names that contain them, limited to the lookback window. `aliases`
    overrides the entity's own list -- the probe uses it to test one
    spelling at a time, since Google's answer to a six-phrase OR is not the
    union of its answers to each phrase."""
    # max_aliases is 3 by default. An entity the press names several ways --
    # "Nagpur Nagarik Bank", "Nagpur Nagarik Sah Bank" -- needs more than
    # three in the query, or the feed never returns the headline that
    # attribution would have accepted.
    ordered = (aliases if aliases is not None else
               aliases_for_language(registry.entities[entity_id]["aliases"], lang))
    query = build_query(registry, entity_id, days=effective_days(days) or None,
                        max_aliases=NEWS_QUERY_ALIASES, aliases=ordered)
    hl, gl, ceid = NEWS_EDITIONS.get(lang, NEWS_EDITIONS["en"])
    return GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query),
                                  hl=hl, gl=gl, ceid=ceid)


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


def fetch_google_news(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Google News RSS: free, no key, aggregates the Indian press.

    One feed per language configured on the entity. A language edition that
    fails is logged and skipped rather than losing the languages that
    worked. MAX_ENTRIES_PER_FEED caps the entity's combined result, not each
    edition, so adding a language never raises the per-entity ceiling that
    bounds classification spend.
    """
    items, seen = [], set()
    for n, lang in enumerate(entity_languages(entity)):
        if n and FETCH_DELAY_SECONDS:
            time.sleep(FETCH_DELAY_SECONDS)
        url = google_news_url(registry, entity["id"], lang, days)
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True,
                             headers={"User-Agent": "Suchak/0.1 (supervisory prototype)"})
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Google News (%s) failed for %s: %s: %s",
                        lang, entity["name"], type(exc).__name__, exc)
            continue
        for entry in feedparser.parse(resp.text).entries:
            title = _strip_html(entry.get("title", ""))
            link = entry.get("link", "")
            if not title or not link or link in seen:
                continue
            seen.add(link)
            items.append({
                "title": title,
                "url": link,
                "source_name": _entry_source(entry, link),
                "snippet": _strip_html(entry.get("summary", "")),
                "published_at": _entry_published(entry),
                "source_type": "news",
            })
    return items[:MAX_ENTRIES_PER_FEED]


def fetch_youtube(registry: Registry, entity, days: int | None = None) -> list[dict]:
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
    window = effective_days(days)
    if window:
        since = datetime.now(timezone.utc) - timedelta(days=window)
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


def fetch_x(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """X/Twitter recent search, scoped to customer complaints.

    Paid, billed per post returned. One request per entity per sweep, never
    paginated, so the cost per sweep is bounded by X_MAX_POSTS and cannot
    run away if a query turns out broader than expected.
    """
    if not (X_ENABLED and X_BEARER):
        return []

    since = datetime.now(timezone.utc) - timedelta(
        days=min(effective_days(days) or X_RECENT_SEARCH_DAYS, X_RECENT_SEARCH_DAYS))
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


def _within_lookback(published_iso: str | None) -> bool:
    if not published_iso or not LOOKBACK_DAYS:
        return True
    try:
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


def _rss_broadcast(url: str, source_name: str, source_type: str) -> list[dict]:
    resp = httpx.get(url, timeout=25, follow_redirects=True,
                     headers={"User-Agent": _BROWSER_UA})
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries[:BROADCAST_MAX]:
        title = _strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        published = _entry_published(entry)
        if not _within_lookback(published):
            continue
        items.append({
            "title": title,
            "url": link,
            "source_name": source_name,
            "snippet": _strip_html(entry.get("summary", "")),
            "published_at": published,
            "source_type": source_type,
        })
    return items


def fetch_rbi() -> list[dict]:
    """RBI press releases: penalties, enforcement, directions. The feed
    covers everything RBI publishes; routing keeps only items that name a
    tracked entity."""
    if not RBI_PRESS_RSS:
        return []
    return _rss_broadcast(RBI_PRESS_RSS, "Reserve Bank of India", "regulatory")


def fetch_nse() -> list[dict]:
    """NSE corporate announcements RSS (all listed companies)."""
    if not NSE_ANN_RSS:
        return []
    return _rss_broadcast(NSE_ANN_RSS, "NSE", "filing")


def _parse_bse_dt(value: str | None) -> str | None:
    """BSE timestamps are naive IST like '2026-08-21T14:30:00.72'."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value[:19])
    except ValueError:
        return None
    ist = timezone(timedelta(hours=5, minutes=30))
    return dt.replace(tzinfo=ist).astimezone(timezone.utc).isoformat(timespec="seconds")


def fetch_bse() -> list[dict]:
    """BSE corporate announcements.

    BSE publishes no documented feed; this is the JSON endpoint the BSE
    website itself calls, so treat it as changeable and keep every field
    access defensive.
    """
    if not BSE_ANN_API:
        return []
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=min(LOOKBACK_DAYS or 2, 30))
    params = {
        "strCat": "-1",
        "strPrevDate": since.strftime("%Y%m%d"),
        "strScrip": "",
        "strSearch": "P",
        "strToDate": now.strftime("%Y%m%d"),
        "strType": "C",
    }
    resp = httpx.get(BSE_ANN_API, params=params, timeout=25, headers={
        "User-Agent": _BROWSER_UA,
        "Referer": "https://www.bseindia.com/",
        "Accept": "application/json",
    })
    resp.raise_for_status()
    rows = (resp.json() or {}).get("Table") or []

    items = []
    for row in rows[:BROADCAST_MAX]:
        subject = _strip_html(row.get("NEWSSUB") or row.get("HEADLINE") or "")
        company = _strip_html(row.get("SLONGNAME") or "")
        if not subject and not company:
            continue
        # the resolver routes by name, so make sure the company name is in
        # the title even when NEWSSUB omits it
        title = subject if company.lower() in subject.lower() \
            else f"{company} - {subject}".strip(" -")
        attach = row.get("ATTACHMENTNAME")
        link = row.get("NSURL") or (
            f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}"
            if attach else "")
        if not link:
            link = f"https://www.bseindia.com/corporates/ann.html#{row.get('NEWSID', '')}"
        items.append({
            "title": title,
            "url": link,
            "source_name": "BSE",
            "snippet": _strip_html(row.get("HEADLINE") or "") or subject,
            "published_at": _parse_bse_dt(row.get("NEWS_DT") or row.get("DT_TM")),
            "source_type": "filing",
        })
    return items


def fetch_x_scrape(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Complaints addressed to the entity's grievance handle, read from a
    signed-in browser session rather than the paid API.

    Off unless SUCHAK_X_SCRAPE=1. Costs nothing per post, so the ceiling
    here is about how much classification the fetch will trigger, not about
    a bill from X.
    """
    if not x_scrape.ENABLED:
        return []
    handle = (entity["x_handle"] or "").strip().lstrip("@")
    if not handle:
        log.info("No X handle for %s -- skipping the browser collector", entity["name"])
        return []
    # -filter:replies would drop the complaints themselves; retweets are the
    # only thing worth excluding at the query.
    return x_scrape.scrape_query(f"to:{handle} -filter:retweets", x_scrape.MAX_POSTS)


def fetch_youtube_comments(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Comments under the entity's recent videos.

    The video supplies what the comment lacks: a comment saying "worst
    service, my money is stuck" never names the bank, but it sits under a
    video that does. So only videos that themselves pass the alias check
    contribute comments, and those comments are marked confidently
    attributed -- the same rule that admits posts sent to the bank's own
    X handle. Comments run in the social channel: they are grievances or
    noise, and the classifier's no-grievance gate decides which.
    """
    if not YOUTUBE_KEY or not YT_COMMENTS:
        return []
    window = effective_days(days)
    since = None
    params = {
        "key": YOUTUBE_KEY,
        "q": build_query(registry, entity["id"], or_token="|"),
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": YT_COMMENT_VIDEOS,
        "regionCode": "IN",
    }
    if window:
        since = datetime.now(timezone.utc) - timedelta(days=window)
        params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    resp = httpx.get(YOUTUBE_SEARCH, params=params, timeout=20)
    if resp.status_code == 403:
        raise RuntimeError(f"YouTube API refused the search: {resp.text[:200]}")
    resp.raise_for_status()

    items: list[dict] = []
    for entry in resp.json().get("items", []):
        if len(items) >= YT_COMMENTS_MAX:
            break
        video_id = (entry.get("id") or {}).get("videoId")
        snip = entry.get("snippet") or {}
        video_title = _strip_html(snip.get("title", ""))
        description = _strip_html(snip.get("description", ""))
        if not video_id or not video_title:
            continue
        # Only a video that names this entity may lend its context to its
        # comments; otherwise a rival bank's video would donate grievances.
        if not registry.mentions(entity["id"], f"{video_title} {description}"):
            continue
        try:
            c_resp = httpx.get(YOUTUBE_COMMENTS_URL, params={
                "key": YOUTUBE_KEY, "part": "snippet", "videoId": video_id,
                "maxResults": YT_COMMENTS_PER_VIDEO, "order": "relevance",
                "textFormat": "plainText",
            }, timeout=20)
        except httpx.HTTPError as exc:
            log.info("Comments unreachable for video %s: %s", video_id, exc)
            continue
        if c_resp.status_code == 403 and "commentsDisabled" in c_resp.text:
            continue                      # that video's choice, not an error
        if c_resp.status_code == 403:
            raise RuntimeError(
                f"YouTube API refused comments: {c_resp.text[:200]}")
        if c_resp.status_code >= 400:
            log.info("Comments error %s for video %s", c_resp.status_code, video_id)
            continue
        for thread in c_resp.json().get("items", []):
            if len(items) >= YT_COMMENTS_MAX:
                break
            top = (((thread.get("snippet") or {}).get("topLevelComment") or {})
                   .get("snippet") or {})
            text = " ".join((top.get("textDisplay") or "").split())
            comment_id = (thread.get("snippet") or {}).get("topLevelComment", {}).get("id")                 or thread.get("id")
            published = top.get("publishedAt")
            if not text or not comment_id:
                continue
            # The video passed the window; its old comments must too.
            if since and published:
                try:
                    when = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if when < since:
                        continue
                except ValueError:
                    pass
            items.append({
                "title": f'Comment on: {video_title}'[:200],
                "url": f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
                "source_name": "YouTube comment",
                "snippet": text[:1500],
                "published_at": published,
                "source_type": "social",
                # The video's alias match is the attribution; the comment
                # text alone would fail the name check, as posts to the
                # bank's own X handle do.
                "attribution_confident": True,
            })
    return items


def fetch_reddit(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Posts naming the entity on Reddit, site-wide and in the Indian
    finance subreddits. Always the last SOCIAL_LOOKBACK_DAYS, whatever
    the news lookback: `days` widens news feeds, not social ones.

    Needs no key and no account, so unlike the X collector there is nothing
    here that can be restricted for looking automated. What it returns is
    unfiltered: the classifier decides which posts are grievances.
    """
    if not reddit_source.ENABLED:
        return []
    return reddit_source.search(registry, entity["id"], SOCIAL_LOOKBACK_DAYS,
                                reddit_source.MAX_POSTS)


def fetch_forums(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Complaints filed against the entity on consumercomplaints.in --
    dated ones from the last SOCIAL_LOOKBACK_DAYS only. Undated entries
    are dropped there: the site dates recent complaints and leaves old
    ones blank, so undated cannot be shown to be recent.

    Every entry there is already a grievance, so this is the densest
    complaint source the app has -- but the classifier still decides
    severity and topic, and the matcher still checks attribution.
    """
    if not forums.ENABLED:
        return []
    return forums.search(registry, entity["id"], SOCIAL_LOOKBACK_DAYS,
                         forums.MAX_ITEMS)


def fetch_yt_comments_social(registry: Registry, entity, days: int | None = None) -> list[dict]:
    """Social-channel wrapper: comments always cover the social window,
    like the other complaint sources, whatever the news lookback."""
    return fetch_youtube_comments(registry, entity, SOCIAL_LOOKBACK_DAYS)


SOURCES = {
    "google_news": fetch_google_news,
    "youtube": fetch_youtube,
    "x": fetch_x,
    "x_scrape": fetch_x_scrape,
    "reddit": fetch_reddit,
    "forums": fetch_forums,
    "youtube_comments": fetch_yt_comments_social,
}

# Two channels a fetch can run: press coverage and customer complaints.
# They answer different supervisory questions on different cadences, so
# the Entities page offers each its own button; "all" remains what the
# background sweep runs.
SOURCE_CHANNELS = {
    "google_news": "news", "youtube": "news",
    "x": "social", "x_scrape": "social", "reddit": "social", "forums": "social",
    "youtube_comments": "social",
}
CHANNELS = ("all", "news", "social")

BROADCAST_SOURCES = {
    "rbi": fetch_rbi,
    "nse": fetch_nse,
    "bse": fetch_bse,
}


def fetch_broadcast_sources(db, registry: Registry) -> dict[int, list[dict]]:
    """Fetch each broadcast feed once and route items to the entities they
    mention. Returns {entity_id: [candidates]}; logs per-feed status to
    fetch_log with a NULL entity."""
    routed: dict[int, list[dict]] = {}
    enabled = {"rbi": RBI_PRESS_RSS, "nse": NSE_ANN_RSS, "bse": BSE_ANN_API}
    for name, fetch in BROADCAST_SOURCES.items():
        if not enabled.get(name):
            continue
        note, found, kept = None, 0, 0
        try:
            items = fetch()
            found = len(items)
            for item in items:
                eids = registry.resolve(f"{item['title']} {item['snippet']}")
                for eid in eids:
                    kept += 1
                    routed.setdefault(eid, []).append({
                        **item,
                        # routing already ran the resolver; the per-item
                        # check in ingest_entity would just repeat it
                        "attribution_confident": True,
                    })
            note = f"routed {kept} item(s) to tracked entities"
        except Exception as exc:
            note = f"fetch failed: {type(exc).__name__}: {exc}"
            log.warning("Broadcast source %s: %s", name, note)
        x(db, "INSERT INTO fetch_log (entity_id, source, found, added, merged, note)"
              " VALUES (NULL,?,?,0,0,?)", (name, found, note))
    return routed


def _article_names_entity(registry: Registry, entity_id: int, url: str) -> bool:
    """Whether the article behind this URL names the entity in its body.

    The feed's snippet is a fragment; the press shortens names in
    headlines and spells them out in the text. Google News links often
    land on an interstitial rather than the publisher, so one hop to the
    first non-Google link is followed. Any failure returns False -- the
    caller then rejects exactly as it always did, so this can only ever
    rescue an item, never admit one on an error.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    try:
        resp = httpx.get(url, headers=headers, timeout=12, follow_redirects=True)
        if resp.status_code >= 400:
            return False
        if registry.mentions(entity_id, _strip_html(resp.text)):
            return True
        if "news.google.com" in str(resp.url):
            m = re.search(r'href="(https?://(?!news\.google\.com|www\.google\.com)'
                          r'[^"]+)"', resp.text)
            if m:
                hop = httpx.get(html.unescape(m.group(1)), headers=headers,
                                timeout=12, follow_redirects=True)
                if hop.status_code < 400 and \
                        registry.mentions(entity_id, _strip_html(hop.text)):
                    return True
    except httpx.HTTPError:
        pass
    return False


def ingest_entity(db, entity, registry: Registry | None = None,
                  extra_candidates: list[dict] | None = None,
                  days: int | None = None, channel: str = "all") -> dict:
    """Fetch one entity's sources and store what survives disambiguation
    and de-duplication. `extra_candidates` carries items already routed to
    this entity from broadcast feeds (RBI, exchanges). `days` widens this
    fetch's window only; `channel` limits it to one kind of source --
    "news", "social", or "all"."""
    registry = registry or load_registry(db)
    result = {"found": 0, "added": 0, "merged": 0, "rejected": 0,
              "billed": 0, "body_confirmed": 0, "note": None}
    notes = []
    body_checks = 0

    if channel == "news":
        notes.append("news sources only")
    elif channel == "social":
        # The lookback picker widens news; social always covers its own
        # fixed year, so a days note here would describe the wrong window.
        notes.append(f"social sources only (last {SOCIAL_LOOKBACK_DAYS} days)")

    window = effective_days(days)
    if window != LOOKBACK_DAYS and channel != "social":
        notes.append(f"searched {window} days")

    trusted_norms = load_trusted_norms(db)
    entity_stop = alias_tokens(json.loads(entity["aliases"]))
    candidates = list(extra_candidates or [])
    if candidates:
        notes.append(f"{len(candidates)} from regulator/exchange feeds")
    for name, fetch in SOURCES.items():
        if channel != "all" and SOURCE_CHANNELS[name] != channel:
            continue
        try:
            got = fetch(registry, entity, days)
            candidates.extend(got)
            billed = sum(1 for c in got if c.get("billed"))
            if billed:
                result["billed"] += billed
                notes.append(f"{billed} paid posts from {name} "
                             f"(~${billed * X_PRICE_PER_POST:.2f})")
            elif name == "x_scrape" and x_scrape.ENABLED and entity["x_handle"]:
                # Always noted, zero included. A DOM change makes this
                # collector return nothing, which is indistinguishable from a
                # quiet week unless the count is stated every time.
                notes.append(f"{len(got)} from X (browser)")
            elif name == "forums" and forums.ENABLED:  # stated even at zero
                # Stated even at zero, like Reddit: the collector raises
                # when it read nothing, so a zero really is "searched and
                # matched nothing".
                notes.append(f"{len(got)} from consumercomplaints.in")
            elif name == "youtube_comments" and YOUTUBE_KEY and YT_COMMENTS:
                # Stated even at zero, like the other social sources.
                notes.append(f"{len(got)} from YouTube comments")
            elif name == "reddit" and reddit_source.ENABLED:
                # Stated even at zero. Reddit raises when it could not read
                # anything, so a zero here really does mean "searched and
                # matched nothing" -- but only if the count is always shown.
                notes.append(f"{len(got)} from Reddit")
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
        # URL dedup runs before attribution so a reject stored on the last
        # fetch is not stored again on this one.
        if one(db, "SELECT 1 FROM items WHERE entity_id=? AND url=?", (entity["id"], link)):
            continue
        if one(db, "SELECT 1 FROM item_sources s JOIN items i ON i.id=s.item_id"
                   " WHERE i.entity_id=? AND s.url=?", (entity["id"], link)):
            continue

        if not cand.get("attribution_confident") and \
                not registry.mentions(entity["id"], f"{title} {cand['snippet']}"):
            near = near_miss(title, cand["snippet"] or "",
                             registry.entities[entity["id"]]["aliases"])
            # A headline lexically this close deserves one more look before
            # rejection: the press shortens names in headlines and spells
            # them out in the text, so the article body -- not the feed's
            # snippet -- is what settles "Jalna Co-op Bank". Budgeted, news
            # only, and an unreachable page rejects as before: reading the
            # body can rescue an item, never admit one on an error.
            if (near and near[0] >= 0.6 and cand["source_type"] == "news"
                    and body_checks < BODY_CHECKS):
                body_checks += 1
                if _article_names_entity(registry, entity["id"], link):
                    result["body_confirmed"] += 1
                    log.info("Kept for %s after reading the body: %r",
                             entity["name"], title[:120])
                    near = None
            if near is not None:
                result["rejected"] += 1
                # The count alone cannot be acted on. Naming the headline
                # turns "6 rejected" into a question someone can answer --
                # usually "the press spells the name differently".
                log.info("Rejected for %s (no alias match): %r",
                         entity["name"], title[:120])
                if near and near[0] >= 0.6:
                    prev = result.get("near_miss")
                    if prev is None or near[0] > prev["score"]:
                        result["near_miss"] = {"score": near[0], "title": title,
                                               "alias": near[1],
                                               "missing": near[3]}
                # A rejected headline is a judgement, and judgements get an
                # appeal: the item is stored on the queue's Rejected tab,
                # where a human can rule it IS this entity's and send it to
                # classification. Social rejects stay unstored -- a
                # site-wide Reddit search rejects mostly true noise, and
                # volume would bury the genuine near-misses.
                if cand["source_type"] != "social":
                    x(db, "INSERT INTO items (entity_id, title, url, source_name,"
                          " snippet, published_at, source_type, source_tier,"
                          " status, gated_out, gate_reason, attribution,"
                          " classifier, relevance, risk_areas, severity,"
                          " actionability, summary)"
                          " VALUES (?,?,?,?,?,?,?,?,'classified',1,?,"
                          "'rejected','attribution',0,'[]','low','monitor',?)",
                      (entity["id"], title, link, cand["source_name"],
                       cand["snippet"][:500], cand["published_at"],
                       cand["source_type"],
                       tier_for(cand["source_type"], cand["source_name"], link,
                                trusted_norms),
                       "another entity's news — no stored alias appears in "
                       "the headline or snippet", title))
                continue

        # the publisher suffix Google News appends differs per outlet, so it
        # poisons similarity and clutters the queue; the outlet is shown
        # separately anyway
        title = strip_publisher(title, cand["source_name"])

        # A missing date is approximately "now" for a news feed read inside
        # its own recency window -- but an invented date on a social
        # complaint is how a 2018 grievance wore "17h ago" in the review
        # queue and slipped past the 365-day cleanup. Social items keep an
        # honest NULL; their collectors drop undated posts anyway, so this
        # is the guard, not the mechanism.
        published = cand["published_at"]
        if not published and cand["source_type"] != "social":
            published = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # A video and an article about the same event are the same event, so
        # duplicate detection deliberately spans source types -- an RBI
        # penalty release merges with the news coverage of that penalty.
        #
        # Two exceptions. Social posts: ten customers complaining about
        # blocked cards is ten data points, not one story covered ten times
        # -- volume IS the conduct signal. Exchange filings: titles are
        # formulaic ("Announcement under Regulation 30..."), so two distinct
        # filings can share a title; only the exact-URL check may merge them.
        dup_id, best, best_title = None, 0.0, ""
        if cand["source_type"] not in NO_MERGE_TYPES:
            for r in recent:
                if r.get("source_type") in NO_MERGE_TYPES:
                    continue
                score = event_similarity(title, r["title"], entity_stop)
                if score > best:
                    dup_id, best, best_title = r["id"], score, r["title"]
        if dup_id and best >= DUP_THRESHOLD and \
                distinctive_overlap(title, best_title, entity_stop) >= DUP_MIN_SHARED:
            x(db, "INSERT INTO item_sources (item_id, url, source_name, title, published_at)"
                  " VALUES (?,?,?,?,?)",
              (dup_id, link, cand["source_name"], title, published))
            # keep this variant's wording in the pool, mapped to the same
            # primary: a third outlet's angle may resemble it more than the
            # primary's headline (transitive clustering)
            recent.append({"id": dup_id, "title": title,
                           "source_type": cand["source_type"]})
            result["merged"] += 1
        else:
            new_id = x(
                db,
                "INSERT INTO items (entity_id, title, url, source_name, snippet,"
                " published_at, source_type, source_tier) VALUES (?,?,?,?,?,?,?,?)",
                (entity["id"], title, link, cand["source_name"],
                 cand["snippet"][:500], published, cand["source_type"],
                 tier_for(cand["source_type"], cand["source_name"], link,
                          trusted_norms)),
            )
            recent.append({"id": new_id, "title": title,
                           "source_type": cand["source_type"]})
            result["added"] += 1

    result["note"] = "; ".join(notes) or None
    _log_fetch(db, entity["id"], result, channel)
    return result


def _log_fetch(db, entity_id: int, result: dict,
               channel: str = "all") -> None:
    note = result["note"]
    if result.get("body_confirmed"):
        kept = (f"{result['body_confirmed']} kept by reading the article "
                "body (name absent from the headline)")
        note = f"{note}; {kept}" if note else kept
    if result.get("rejected"):
        rejected = f"{result['rejected']} rejected as another entity's news"
        near = result.get("near_miss")
        if near:
            head = near["title"][:80] + ("…" if len(near["title"]) > 80 else "")
            if near["missing"]:
                why = (f"almost '{near['alias']}' but without "
                       f"{'/'.join(near['missing'])}")
            else:
                why = (f"contains every word of '{near['alias']}' "
                       "but not as one phrase")
            rejected += (f'. Closest: "{head}" — {why}. If that is this '
                         "entity, add the press's spelling as an alias.")
        rejected += " Rejects are held on the queue's Rejected tab."
        note = f"{note}; {rejected}" if note else rejected
    x(db, "INSERT INTO fetch_log (entity_id, source, found, added, merged, note)"
          " VALUES (?,?,?,?,?,?)",
      (entity_id, f"fetch:{channel}", result["found"], result["added"],
       result["merged"], note))


def run_cycle(entity_id: int | None = None, days: int | None = None,
              channel: str = "all") -> dict:
    """Fetch (all or one entity) then classify anything new. Opens its own
    DB connection — safe to call from a background thread.

    `days` widens the per-entity feeds for this run only. Broadcast feeds
    (RBI, exchanges) keep their own window: one fetch serves every entity,
    so widening them for one entity's sake would re-scan the lot.
    `channel` limits the run to news or social sources; broadcast feeds
    are press coverage, so a social-only run skips them entirely.
    """
    if channel not in CHANNELS:
        channel = "all"
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
            if channel == "social":
                routed = {}
                totals["routed"] = 0
            else:
                routed = fetch_broadcast_sources(db, registry)
                totals["routed"] = sum(len(v) for v in routed.values())
            for n, entity in enumerate(entities):
                if n and FETCH_DELAY_SECONDS:
                    time.sleep(FETCH_DELAY_SECONDS)
                r = ingest_entity(db, entity, registry,
                                  extra_candidates=routed.get(entity["id"], []),
                                  days=days, channel=channel)
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
