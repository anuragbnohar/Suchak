"""Customer complaints about the regulated entities, read from Reddit.

Reddit serves the JSON that backs its own website at the same URLs with a
`.json` suffix, with no key and no account. That matters after the X
attempt: there is no credential to be rate-limited on, no session to keep
alive, and nothing that can be restricted for looking automated. The cost
is that Reddit refuses anonymous requests carrying a default User-Agent,
so this sends a descriptive one and waits between calls.

What comes back is not filtered for grievances here. The classifier makes
that judgement, as it does for the X collector: a complaint phrased
unusually ("worst experience of my life", no complaint vocabulary at all)
would be dropped by a keyword filter and caught by the model.
"""
import html as html_mod
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .matching import Registry, build_query

log = logging.getLogger("suchak.reddit")

BUILD = "2026-08-24.2-reddit-rss"

# No credential, so this is on unless deliberately turned off.
ENABLED = os.environ.get("SUCHAK_REDDIT", "1").strip().lower() not in ("0", "false", "no")

# Reddit refuses anonymous .json reads with HTTP 403 -- from a hosting
# provider and from an ordinary office connection alike. A route sweep
# across six variants found exactly one that answers: the Atom feed on
# www, requested with a browser agent. That is what this uses.
SEARCH_URL = "https://www.reddit.com/search.rss"
SUB_SEARCH_URL = "https://www.reddit.com/r/{sub}/search.rss"

USER_AGENT = os.environ.get(
    "SUCHAK_REDDIT_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Subreddits where Indian retail banking complaints actually collect. The
# site-wide pass catches most of it; these are searched separately because
# a post inside a finance subreddit is on-topic even when the site-wide
# relevance ranking buries it.
SUBREDDITS = [s.strip() for s in os.environ.get(
    "SUCHAK_REDDIT_SUBS",
    "india,personalfinanceindia,IndiaInvestments,CreditCardsIndia,"
    "legaladviceindia,IndianStreetBets").split(",") if s.strip()]

MAX_POSTS = max(10, min(int(os.environ.get("SUCHAK_REDDIT_MAX", "50")), 200))
PER_REQUEST = 100          # Reddit's ceiling for one search page
PAUSE_SECONDS = float(os.environ.get("SUCHAK_REDDIT_PAUSE", "2"))
TIMEOUT = 25

LAST_DIAGNOSIS: dict = {}


class RedditUnavailable(RuntimeError):
    """Reddit would not serve the search. Distinct from an empty result:
    the caller must not record 'no complaints' when nothing was read."""


def _window(days: int | None) -> str:
    """Reddit takes a coarse bucket, not a date."""
    if not days:
        return "all"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return "all"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_mod.unescape(text or "")).strip()


def _get(url: str, params: dict):
    """One search request, with Reddit's refusals named rather than
    collapsed into an empty list."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/atom+xml, application/xml, text/xml"}
    try:
        resp = httpx.get(url, params=params, headers=headers,
                         timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise RedditUnavailable(f"Could not reach Reddit: {exc}") from exc

    if resp.status_code == 429:
        raise RedditUnavailable(
            "Reddit rate-limited the request (HTTP 429). Wait a few minutes "
            "and fetch again, or raise SUCHAK_REDDIT_PAUSE.")
    if resp.status_code in (403, 401):
        raise RedditUnavailable(
            f"Reddit refused the request (HTTP {resp.status_code}). Reddit "
            "blocks anonymous reads on most of its routes; this collector "
            "uses the one that answers (the Atom feed on www). If that is "
            "now refused too, run `python -m scripts.probe_sources "
            "--entity <name> --routes` to find a route that still works.")
    if resp.status_code >= 400:
        raise RedditUnavailable(
            f"Reddit answered HTTP {resp.status_code}: {resp.text[:200]}")

    ctype = resp.headers.get("content-type", "").lower()
    if not ("xml" in ctype or "rss" in ctype or "atom" in ctype):
        # A block or interstitial page arrives as HTML with a 200.
        raise RedditUnavailable(
            f"Reddit returned {ctype or 'no content-type'} instead of a feed, "
            "which means a block or interstitial page rather than search "
            f"results: {_strip_html(resp.text)[:200]}")

    feed = feedparser.parse(resp.text)
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RedditUnavailable(
            f"Reddit sent an unreadable feed: {getattr(feed, 'bozo_exception', '')}")
    return feed


def _posts(feed) -> list:
    return list(getattr(feed, "entries", []) or [])


_SUB_RE = re.compile(r"/r/([A-Za-z0-9_]+)/")


def _to_item(entry) -> dict | None:
    """One Atom entry as a stored item.

    Reddit's feed carries the post body in `content` as escaped HTML; the
    title alone is often just a label ("HDFC did it again"), so both go to
    the classifier.
    """
    title = _strip_html(entry.get("title", ""))
    link = entry.get("link") or ""
    if not title or not link:
        return None

    body = ""
    content = entry.get("content") or []
    if content:
        body = _strip_html(content[0].get("value", ""))
    elif entry.get("summary"):
        body = _strip_html(entry.get("summary"))

    published = None
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            published = datetime(*parsed[:6], tzinfo=timezone.utc).isoformat(
                timespec="seconds")
            break

    match = _SUB_RE.search(link)
    sub = match.group(1) if match else "reddit"
    return {
        "title": title,
        "url": link,
        "source_name": f"r/{sub}",
        "snippet": body[:1500],
        "published_at": published,
        "source_type": "social",
    }


def search(registry: Registry, entity_id: int, days: int | None = None,
           limit: int = MAX_POSTS, on_progress=None) -> list[dict]:
    """Posts naming this entity, site-wide and in the Indian finance
    subreddits, newest first and deduplicated by permalink.

    `on_progress(label, outcome)` is called around each search. Seven
    requests with a pause between them take long enough that a caller
    printing nothing until the end looks hung, so it can report as it goes.
    """
    # Reddit understands the same quoted-OR-with-exclusions syntax Google
    # News does, so the entity's aliases and its rivals' exclusions carry
    # over unchanged.
    query = build_query(registry, entity_id, max_aliases=4, or_token="OR")
    window = _window(days)
    seen: set[str] = set()
    items: list[dict] = []
    errors: list[str] = []
    passes = 0

    def collect(url: str, params: dict, label: str) -> None:
        nonlocal passes
        if len(items) >= limit:
            return
        if passes:
            time.sleep(PAUSE_SECONDS)
        passes += 1
        if on_progress:
            on_progress(label, "searching")
        try:
            payload = _get(url, params)
        except RedditUnavailable as exc:
            errors.append(f"{label}: {exc}")
            if on_progress:
                on_progress(label, f"FAILED -- {exc}")
            return
        before = len(items)
        for post in _posts(payload):
            item = _to_item(post)
            if not item or item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)
        if on_progress:
            on_progress(label, f"{len(items) - before} new "
                               f"({len(_posts(payload))} returned)")

    collect(SEARCH_URL,
            {"q": query, "sort": "new", "limit": PER_REQUEST,
             "t": window, "type": "link"},
            "site-wide")

    for sub in SUBREDDITS:
        collect(SUB_SEARCH_URL.format(sub=sub),
                {"q": query, "sort": "new", "limit": PER_REQUEST,
                 "t": window, "restrict_sr": 1},
                f"r/{sub}")

    LAST_DIAGNOSIS.clear()
    LAST_DIAGNOSIS.update({
        "query": query, "window": window, "passes": passes,
        "found": len(items), "errors": errors,
    })

    # Every pass failing is a collector failure, not a quiet week. Reporting
    # it as an empty result would put "no complaints" in front of a
    # supervisor on the strength of nothing having been read.
    if errors and not items and len(errors) == passes:
        raise RedditUnavailable(errors[0].split(": ", 1)[-1])
    if errors:
        log.warning("Reddit: %d of %d searches failed for entity %s",
                    len(errors), passes, entity_id)

    items.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return items[:limit]
