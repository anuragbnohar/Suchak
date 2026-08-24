"""Consumer grievances about the regulated entities, read from
consumercomplaints.in.

This is the closest thing to a public complaints register that India has
for retail banking: people post there precisely because they got nowhere
with the bank. For the supervisory question the Social media screen asks
-- what are customers complaining about -- it is better source material
than social chatter, because every entry is already a complaint.

The site is read as plain HTML. There is no API and no key, so nothing
here can be rate-limited against an account. What can break is the
markup: the parser targets the classes the site uses for its results
list, and a redesign would change them. That failure is made loud rather
than silent -- a page that loads but yields nothing raises, because
"no complaints about this bank" is a conclusion a supervisor could act
on and it must never be produced by a quiet parser failure.
"""
import html.parser
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from .matching import Registry

log = logging.getLogger("suchak.forums")

BUILD = "2026-08-24.1-consumercomplaints"

ENABLED = os.environ.get("SUCHAK_FORUMS", "1").strip().lower() not in ("0", "false", "no")

BASE = "https://www.consumercomplaints.in"
SEARCH_URL = BASE + "/"

# The site serves ordinary HTML to an ordinary browser agent; a library
# default gets a challenge page instead.
USER_AGENT = os.environ.get(
    "SUCHAK_FORUMS_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_ITEMS = max(5, min(int(os.environ.get("SUCHAK_FORUMS_MAX", "50")), 200))
MAX_PAGES = max(1, min(int(os.environ.get("SUCHAK_FORUMS_PAGES", "2")), 10))
PAUSE_SECONDS = float(os.environ.get("SUCHAK_FORUMS_PAUSE", "1.5"))
TIMEOUT = 30

# Class prefixes the results list uses. Matched on prefix because the site
# appends modifiers ("...__title is-read") to the same block.
TITLE_CLASS = "complaint-box-results__title"
TEXT_CLASS = "complaint-box-results__text"

LAST_DIAGNOSIS: dict = {}


class ForumUnavailable(RuntimeError):
    """The site would not serve readable results. Distinct from an empty
    result: the caller must not record "no complaints" when nothing was
    read, and must not do so when the markup changed either."""


class _Results(html.parser.HTMLParser):
    """Pull (title, href, body) triples out of the results list.

    The site emits each result as a titled anchor followed by a text
    block, so the two are paired in document order rather than by walking
    a container -- that survives the wrapper markup changing around them.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._title_depth = 0
        self._text_depth = 0
        self._buf: list[str] = []
        self._href = ""
        self._pending: dict | None = None
        self._dates: list[str] = []
        self._in_time = False

    @staticmethod
    def _has(attrs: dict, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in (attrs.get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._title_depth:
            self._title_depth += 1
        elif self._has(a, TITLE_CLASS):
            self._title_depth = 1
            self._buf = []
            self._href = a.get("href") or ""
        elif self._text_depth:
            self._text_depth += 1
        elif self._has(a, TEXT_CLASS):
            self._text_depth = 1
            self._buf = []
        # The site marks the posting date with <time datetime="...">, which
        # is the only machine-readable date on the page.
        if tag == "time":
            self._in_time = True
            if a.get("datetime"):
                self._dates.append(a["datetime"])

    def handle_endtag(self, tag):
        if tag == "time":
            self._in_time = False
        if self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                title = " ".join("".join(self._buf).split())
                # Titles arrive prefixed with an em dash separator.
                title = re.sub(r"^[\s—–-]+", "", title)
                self._pending = {"title": title, "href": self._href}
                self._buf = []
        elif self._text_depth:
            self._text_depth -= 1
            if self._text_depth == 0:
                body = " ".join("".join(self._buf).split())
                if self._pending:
                    self._pending["body"] = body
                    self.rows.append(self._pending)
                    self._pending = None
                self._buf = []

    def handle_data(self, data):
        if self._title_depth or self._text_depth:
            self._buf.append(data)
        elif self._in_time:
            self._dates.append(data.strip())


def _fetch(params: dict) -> str:
    try:
        resp = httpx.get(SEARCH_URL, params=params,
                         headers={"User-Agent": USER_AGENT},
                         timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ForumUnavailable(
            f"Could not reach consumercomplaints.in: {exc}") from exc
    if resp.status_code == 429:
        raise ForumUnavailable(
            "consumercomplaints.in rate-limited the request (HTTP 429). "
            "Wait a few minutes, or raise SUCHAK_FORUMS_PAUSE.")
    if resp.status_code >= 400:
        raise ForumUnavailable(
            f"consumercomplaints.in answered HTTP {resp.status_code}.")
    if "html" not in resp.headers.get("content-type", "").lower():
        raise ForumUnavailable(
            "consumercomplaints.in returned "
            f"{resp.headers.get('content-type', 'no content-type')} "
            "instead of a page, which means a challenge or block rather "
            "than results.")
    return resp.text


def _parse_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    # <time datetime="..."> is ISO; the visible text is not.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d %b %Y", "%B %d, %Y",
                    "%d %B %Y", "%b %d, %Y %H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def search(registry: Registry, entity_id: int, days: int | None = None,
           limit: int = MAX_ITEMS, on_progress=None) -> list[dict]:
    """Complaints filed against this entity, newest page first.

    The search is by name because that is the only handle the site offers;
    attribution back to the entity is still done by the caller's matcher,
    so a result naming a different bank is dropped there as it would be
    for any other source.
    """
    ent = registry.entities[entity_id]
    # The site indexes companies under their common name, not the legal
    # one, so the first alias beats "... Ltd." for finding the page.
    aliases = [a for a in ent["aliases"] if a.isascii()] or [ent["name"]]
    term = aliases[0]

    seen: set[str] = set()
    items: list[dict] = []
    errors: list[str] = []
    pages = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None

    for page in range(1, MAX_PAGES + 1):
        if len(items) >= limit:
            break
        if pages:
            time.sleep(PAUSE_SECONDS)
        pages += 1
        label = f"page {page}"
        if on_progress:
            on_progress(label, "searching")
        params = {"search": term}
        if page > 1:
            params["page"] = page
        try:
            body = _fetch(params)
        except ForumUnavailable as exc:
            errors.append(f"{label}: {exc}")
            if on_progress:
                on_progress(label, f"FAILED -- {exc}")
            break

        parser = _Results()
        parser.feed(body)
        dates = [d for d in (_parse_date(d) for d in parser._dates) if d]
        before = len(items)
        for i, row in enumerate(parser.rows):
            href = row.get("href") or ""
            url = href if href.startswith("http") else urllib.parse.urljoin(BASE, href)
            if not row.get("title") or not href or url in seen:
                continue
            published = dates[i] if i < len(dates) else None
            if cutoff and published:
                try:
                    if datetime.fromisoformat(published) < cutoff:
                        continue
                except ValueError:
                    pass
            seen.add(url)
            items.append({
                "title": row["title"],
                "url": url,
                "source_name": "consumercomplaints.in",
                "snippet": (row.get("body") or "")[:1500],
                "published_at": published,
                "source_type": "social",
            })
        if on_progress:
            on_progress(label, f"{len(items) - before} new "
                               f"({len(parser.rows)} on page)")
        if not parser.rows:
            break

    LAST_DIAGNOSIS.clear()
    LAST_DIAGNOSIS.update({"term": term, "pages": pages, "found": len(items),
                           "errors": errors})

    if errors and not items:
        raise ForumUnavailable(errors[0].split(": ", 1)[-1])
    # A page that loaded but yielded nothing is the markup changing, not a
    # bank without complaints. consumercomplaints.in carries complaints
    # about every large Indian bank; zero from a 200 means the parser is
    # reading the wrong element.
    if not items and pages:
        raise ForumUnavailable(
            "The results page loaded but no complaints could be read from "
            f"it. The site's markup has probably changed -- the parser "
            f"targets '{TITLE_CLASS}' and '{TEXT_CLASS}'. Run "
            "`python -m scripts.probe_sources --forums-only` to see the "
            "current structure.")

    return items[:limit]
