"""X (Twitter) posts via Bright Data's Web Scraper API.

Bright Data runs the browser farm and absorbs X's blocking, which is
exactly what killed the self-hosted scraper: there is no account of ours
to restrict. Their API is asynchronous -- trigger a collection, poll the
snapshot, download JSON -- so one social fetch may only *start* a
collection and the next one harvests it.

The "X (formerly Twitter) - Posts" scraper in their library offers NO
keyword search (confirmed on the real dashboard): its only discovery
mode is a profile URL. So collection reads the timeline of the bank's
own X handle -- the care/support handle is the strongest choice, since
its timeline is complaint conversations by construction. An entity
without a handle cannot be collected and says so in the fetch note.

Free plans meter by record, so this module is defensive about credits:
a snapshot that was not ready in time is remembered in a local file and
polled again on the next fetch instead of re-triggered, and the per-run
record cap is low. Field names differ between Bright Data's scrapers,
so parsing is tolerant and the probe prints one raw record verbatim --
the mapping gets corrected against reality, not guessed.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("suchak.brightdata")

BUILD = "2026-09-01.1-profile-discovery"

KEY = os.environ.get("SUCHAK_BRIGHTDATA_KEY", "")
# Bright Data's library id for "X (formerly Twitter) - Posts". It is the
# same public id for every customer (it names the scraper, not the
# account), so it ships as the default; the env var overrides it if a
# different scraper is ever chosen in the dashboard.
DATASET = os.environ.get("SUCHAK_BRIGHTDATA_DATASET", "gd_lwxkxvnf1cynvib9co")
ENABLED = bool(KEY)

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


def profile_url_for(entity: dict) -> str:
    """The profile whose timeline is collected. Only a handle works: this
    scraper has no keyword search, so an entity without one is skipped
    with advice rather than silently returning nothing."""
    handle = (entity.get("x_handle") or "").strip().lstrip("@")
    if not handle:
        raise BrightDataUnavailable(
            f"{entity.get('name', 'entity')} has no X handle set. Bright "
            "Data's X scraper can only read a profile's timeline (it has "
            "no keyword search), so add the bank's care/support handle "
            "(like HDFCBank_Cares) via Entities -> Edit.")
    return f"https://x.com/{handle}"


def trigger(profile_url: str, days: int | None = None) -> str:
    """Start a discover-by-profile-URL collection; returns the snapshot id.

    start/end dates are the one input field not yet confirmed against a
    real run, so a rejected payload retries once with the bare URL rather
    than failing the whole fetch over a date-format guess."""
    body: dict = {"url": profile_url}
    if days:
        now = datetime.now(timezone.utc)
        body["start_date"] = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        body["end_date"] = now.strftime("%Y-%m-%d")
    attempts = [body] if len(body) == 1 else [body, {"url": profile_url}]
    resp = None
    for payload in attempts:
        resp = httpx.post(
            f"{API}/trigger",
            params={"dataset_id": DATASET, "include_errors": "true",
                    "type": "discover_new", "discover_by": "profile_url",
                    "limit_per_input": str(MAX_RECORDS)},
            headers=_headers(),
            json=[payload],
            timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise BrightDataUnavailable(
                f"Bright Data refused the API key (HTTP {resp.status_code}): "
                f"{resp.text[:200]}")
        if resp.status_code < 400:
            break
        log.warning("Trigger rejected (HTTP %s): %s", resp.status_code,
                    resp.text[:200])
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
    profile = profile_url_for(entity)
    state = _load_state()
    key = str(entity["id"])
    snapshot = state.get(key)
    fresh_trigger = False

    if not snapshot:
        snapshot = trigger(profile, days)
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
                           "profile": profile, "fresh_trigger": fresh_trigger})

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
        # The timeline belongs to the entity's own handle, so every post
        # is attributed by construction -- same as the earlier collectors
        # that read replies to the care handle.
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
