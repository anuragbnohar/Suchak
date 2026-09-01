"""X (Twitter) posts via Bright Data's Web Scraper API.

Bright Data runs the browser farm and absorbs X's blocking, which is
exactly what killed the self-hosted scraper: there is no account of ours
to restrict. Their API is asynchronous -- trigger a collection, poll the
snapshot, download JSON -- so one social fetch may only *start* a
collection and the next one harvests it.

Free plans meter by record, so this module is defensive about credits:
a snapshot that was not ready in time is remembered in a local file and
polled again on the next fetch instead of re-triggered, and the per-run
record cap is low. Field names differ between Bright Data's X scrapers,
so parsing is tolerant and the probe prints one raw record verbatim --
the mapping gets corrected against reality, not guessed.
"""
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("suchak.brightdata")

BUILD = "2026-08-26.1-brightdata"

KEY = os.environ.get("SUCHAK_BRIGHTDATA_KEY", "")
# The gd_... id of the X scraper chosen in the Bright Data dashboard
# (Web Scrapers -> the X/Twitter scraper -> API request builder).
DATASET = os.environ.get("SUCHAK_BRIGHTDATA_DATASET", "")
ENABLED = bool(KEY and DATASET)

API = "https://api.brightdata.com/datasets/v3"
MAX_RECORDS = max(10, min(int(os.environ.get("SUCHAK_BD_MAX", "50")), 500))
WAIT_SECONDS = max(30, min(int(os.environ.get("SUCHAK_BD_WAIT", "150")), 600))
POLL_SECONDS = max(5, min(int(os.environ.get("SUCHAK_BD_POLL", "10")), 60))
TIMEOUT = 30

# Pending snapshots, keyed by entity id. A file rather than the DB because
# source fetchers do not carry a DB handle; gitignored like the X session.
STATE_PATH = os.environ.get(
    "SUCHAK_BD_STATE",
    str(Path(__file__).resolve().parent.parent / ".bd_snapshots.json"))

LAST_DIAGNOSIS: dict = {}


class BrightDataUnavailable(RuntimeError):
    """Bright Data refused or failed; never returned as an empty result."""


class SnapshotPending(RuntimeError):
    """Triggered but not ready in time; the id is saved and the next social
    fetch harvests it without spending fresh credits."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json"}


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError as exc:
        log.warning("Could not persist snapshot state: %s", exc)


def search_url_for(entity: dict, aliases: list[str]) -> str:
    """What to ask X for. A care handle is the strongest signal -- replies
    to it are complaints by construction. Without one, the entity's most
    common press spelling."""
    handle = (entity.get("x_handle") or "").strip().lstrip("@")
    if handle:
        query = f"to:{handle} -filter:retweets"
    else:
        first = next((a for a in aliases if a.isascii()), entity["name"])
        query = f'"{first}"'
    return ("https://x.com/search?q="
            + urllib.parse.quote(query) + "&f=live")


def trigger(search_url: str) -> str:
    """Start a collection; returns the snapshot id."""
    resp = httpx.post(
        f"{API}/trigger",
        params={"dataset_id": DATASET, "include_errors": "true",
                "type": "discover_new", "discover_by": "search_url",
                "limit_per_input": str(MAX_RECORDS)},
        headers=_headers(),
        json=[{"url": search_url}],
        timeout=TIMEOUT)
    if resp.status_code in (401, 403):
        raise BrightDataUnavailable(
            f"Bright Data refused the API key (HTTP {resp.status_code}): "
            f"{resp.text[:200]}")
    if resp.status_code >= 400:
        raise BrightDataUnavailable(
            f"Bright Data trigger failed (HTTP {resp.status_code}): "
            f"{resp.text[:300]}")
    snapshot = (resp.json() or {}).get("snapshot_id")
    if not snapshot:
        raise BrightDataUnavailable(
            f"Trigger answered without a snapshot id: {resp.text[:200]}")
    return snapshot


def snapshot_status(snapshot_id: str) -> str:
    resp = httpx.get(f"{API}/progress/{snapshot_id}",
                     headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise BrightDataUnavailable(
            f"Progress check failed (HTTP {resp.status_code}): {resp.text[:200]}")
    return (resp.json() or {}).get("status", "unknown")


def download(snapshot_id: str) -> list[dict]:
    resp = httpx.get(f"{API}/snapshot/{snapshot_id}",
                     params={"format": "json"},
                     headers=_headers(), timeout=60)
    if resp.status_code >= 400:
        raise BrightDataUnavailable(
            f"Snapshot download failed (HTTP {resp.status_code}): "
            f"{resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        # some datasets stream NDJSON
        data = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    return data if isinstance(data, list) else [data]


def parse_post(rec: dict) -> dict | None:
    """One Bright Data record as a stored item. Key names differ between
    their X scrapers, so every field tries the spellings seen in the wild;
    the probe prints a raw record so this map gets corrected, not guessed."""
    text = (rec.get("description") or rec.get("post_text") or rec.get("text")
            or rec.get("content") or "")
    text = " ".join(str(text).split())
    url = (rec.get("url") or rec.get("post_url") or rec.get("link") or "")
    if not text or not url:
        return None
    when = (rec.get("date_posted") or rec.get("timestamp")
            or rec.get("created_at") or rec.get("date"))
    published = None
    if when:
        try:
            published = datetime.fromisoformat(
                str(when).replace("Z", "+00:00")).astimezone(timezone.utc) \
                .isoformat(timespec="seconds")
        except ValueError:
            pass
    user = rec.get("user_posted") or rec.get("author") or rec.get("user") or {}
    if isinstance(user, dict):
        author = user.get("username") or user.get("screen_name") or user.get("name") or "x"
    else:
        author = str(user) or "x"
    return {
        "title": text if len(text) <= 120 else text[:117].rstrip() + "...",
        "url": url,
        "source_name": f"@{str(author).lstrip('@')}",
        "snippet": text[:1500],
        "published_at": published,
        "source_type": "social",
    }


def collect(entity: dict, aliases: list[str], days: int | None = None) -> list[dict]:
    """Posts for one entity: harvest the pending snapshot if one exists,
    else trigger a new collection and wait a bounded time for it."""
    state = _load_state()
    key = str(entity["id"])
    snapshot = state.get(key)
    fresh_trigger = False
    search = search_url_for(entity, aliases)

    if not snapshot:
        snapshot = trigger(search)
        fresh_trigger = True
        state[key] = snapshot
        _save_state(state)

    deadline = time.monotonic() + WAIT_SECONDS
    status = snapshot_status(snapshot)
    while status == "running" and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        status = snapshot_status(snapshot)

    LAST_DIAGNOSIS.clear()
    LAST_DIAGNOSIS.update({"snapshot": snapshot, "status": status,
                           "search": search, "fresh_trigger": fresh_trigger})

    if status == "running":
        raise SnapshotPending(
            f"Bright Data is still collecting (snapshot {snapshot}). It is "
            "saved: the next social fetch will pick it up without spending "
            "fresh credits.")
    if status != "ready":
        # a failed snapshot must not wedge the entity forever
        state.pop(key, None)
        _save_state(state)
        raise BrightDataUnavailable(
            f"Snapshot {snapshot} ended as {status!r} -- it was cleared; "
            "the next fetch will trigger a new collection.")

    records = download(snapshot)
    state.pop(key, None)
    _save_state(state)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    items, skipped = [], 0
    for rec in records[:MAX_RECORDS]:
        item = parse_post(rec)
        if not item:
            skipped += 1
            continue
        if cutoff and item["published_at"]:
            try:
                if datetime.fromisoformat(item["published_at"]) < cutoff:
                    continue
            except ValueError:
                pass
        # Replies to the entity's own care handle are attributed by
        # construction, like the API and browser collectors before this.
        if (entity.get("x_handle") or "").strip():
            item["attribution_confident"] = True
        items.append(item)
    LAST_DIAGNOSIS.update({"records": len(records), "parsed": len(items),
                           "unparsable": skipped})
    if records and not items and skipped == len(records):
        raise BrightDataUnavailable(
            "Every record downloaded but none could be parsed -- the "
            "dataset's field names differ from the map. Run the probe with "
            "--brightdata to print a raw record, and the map gets fixed "
            "against it.")
    return items
