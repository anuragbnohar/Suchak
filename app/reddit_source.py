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
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from .matching import Registry, build_query

log = logging.getLogger("suchak.reddit")

BUILD = "2026-08-24.1-reddit"

# No credential, so this is on unless deliberately turned off.
ENABLED = os.environ.get("SUCHAK_REDDIT", "1").strip().lower() not in ("0", "false", "no")

SEARCH_URL = "https://www.reddit.com/search.json"
SUB_SEARCH_URL = "https://www.reddit.com/r/{sub}/search.json"

# Reddit blocks anonymous requests that send a library default. A
# descriptive agent naming the project is what its API rules ask for.
USER_AGENT = os.environ.get(
    "SUCHAK_REDDIT_UA",
    "python:suchak-supervisory-prototype:0.1 (RBI SSM research; contact via repo)")

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


def _get(url: str, params: dict) -> dict:
    """One search request, with Reddit's refusals named rather than
    collapsed into an empty list."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
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
            f"Reddit refused the request (HTTP {resp.status_code}). This is "
            "usually the User-Agent or the network the request came from -- "
            "Reddit blocks anonymous reads from many hosting providers. "
            "From an ordinary home or office connection it normally works.")
    if resp.status_code >= 400:
        raise RedditUnavailable(
            f"Reddit answered HTTP {resp.status_code}: {resp.text[:200]}")

    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        # A block or interstitial page arrives as HTML with a 200.
        raise RedditUnavailable(
            f"Reddit returned {ctype or 'no content-type'} instead of JSON, "
            "which means a block or interstitial page rather than search "
            f"results: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RedditUnavailable(f"Reddit sent unreadable JSON: {exc}") from exc


def _posts(payload: dict) -> list[dict]:
    children = (payload.get("data") or {}).get("children") or []
    return [c.get("data") or {} for c in children if c.get("kind") == "t3"]


def _to_item(post: dict) -> dict | None:
    title = (post.get("title") or "").strip()
    permalink = post.get("permalink") or ""
    if not title or not permalink:
        return None
    created = post.get("created_utc")
    published = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                 if created else None)
    sub = post.get("subreddit") or "reddit"
    # selftext is the complaint itself on Reddit; the title is often just a
    # label ("HDFC did it again"). Both go to the classifier.
    body = (post.get("selftext") or "").strip()
    return {
        "title": title,
        "url": f"https://www.reddit.com{permalink}",
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
             "t": window, "raw_json": 1, "type": "link"},
            "site-wide")

    for sub in SUBREDDITS:
        collect(SUB_SEARCH_URL.format(sub=sub),
                {"q": query, "sort": "new", "limit": PER_REQUEST,
                 "t": window, "raw_json": 1, "restrict_sr": 1},
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
