"""SQLite storage. One file, zero setup — right-sized for the prototype.

Connections are opened per request/thread (SQLite connections are not
thread-safe to share). WAL mode lets the background fetcher write while
the web app reads.
"""
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("suchak.db")

DB_PATH = os.environ.get(
    "SUCHAK_DB", str(Path(__file__).resolve().parent.parent / "suchak.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('member','lead','superadmin')),
    entity_id     INTEGER REFERENCES entities(id),
    risk_areas    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    aliases    TEXT NOT NULL DEFAULT '[]',
    exclude_terms TEXT NOT NULL DEFAULT '[]',
    -- news languages to search for this entity, as Google News edition
    -- codes. Per entity on purpose: a Nagpur cooperative bank is covered in
    -- Marathi, a national bank is not, and fetching every language for
    -- every entity multiplies volume and cost for nothing.
    languages     TEXT NOT NULL DEFAULT '["en"]',
    x_handle   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entities(id),
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    source_name    TEXT,
    source_type    TEXT NOT NULL DEFAULT 'news',
    source_tier    TEXT NOT NULL DEFAULT '',
    snippet        TEXT,
    published_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new','classified','reviewed','dismissed')),
    -- classification verdict
    relevance      REAL,
    risk_areas     TEXT DEFAULT '[]',
    severity       TEXT,
    actionability  TEXT,
    geography      TEXT,
    summary        TEXT,
    factor_matches TEXT DEFAULT '[]',
    relationships  TEXT DEFAULT '[]',
    complaint_topics TEXT DEFAULT '[]',
    classifier     TEXT,
    model          TEXT,
    classified_at  TEXT,
    gated_out      INTEGER NOT NULL DEFAULT 0,
    gate_reason    TEXT,
    -- human review (the labels the system learns from)
    reviewed_by       INTEGER REFERENCES users(id),
    reviewed_at       TEXT,
    review_relevant   INTEGER,
    review_severity   TEXT,
    review_risk_areas TEXT,
    review_complaint_topics TEXT,
    review_actionable INTEGER,
    review_action     TEXT,
    review_notes      TEXT,
    -- follow-up tracking: a review that says "action required" opens a
    -- to-do here, and the To-do page closes it. NULL action_status means
    -- the item carries no follow-up at all.
    action_status     TEXT CHECK (action_status IN ('open','done')),
    action_owner      INTEGER REFERENCES users(id),
    action_due        TEXT,
    action_closed_at  TEXT,
    action_closed_by  INTEGER REFERENCES users(id),
    action_close_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_entity ON items(entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_entity_url ON items(entity_id, url);

-- Every review ever recorded on an item, append-only. The review_* columns
-- on items mirror the most recent row here: the whole app (queue order,
-- severity override, dashboards, the learning loop) reads the current
-- verdict from items, while this table keeps who said what, and when.
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    relevant   INTEGER,
    severity   TEXT,
    risk_areas TEXT NOT NULL DEFAULT '[]',
    complaint_topics TEXT NOT NULL DEFAULT '[]',
    actionable INTEGER,
    action     TEXT,
    notes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(item_id, id);

-- additional outlets reporting the same underlying event
CREATE TABLE IF NOT EXISTS item_sources (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    source_name  TEXT,
    title        TEXT,
    published_at TEXT,
    source_tier  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_item_sources_item ON item_sources(item_id);

CREATE TABLE IF NOT EXISTS factors (
    id         INTEGER PRIMARY KEY,
    -- Scope, widest last: a kind applies to every entity of that kind,
    -- and both columns NULL applies to every entity there is.
    entity_id  INTEGER REFERENCES entities(id),
    entity_kind TEXT,
    name       TEXT NOT NULL,
    conditions TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- small key/value store for policy text editable in the admin UI
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by INTEGER REFERENCES users(id)
);

-- Patterns found across an entity's social-media grievances: a product or
-- process that keeps drawing complaints, with the evidence and a
-- recommendation. Derived data, regenerated on demand -- each run replaces
-- the entity's previous set, so there is no history to migrate.
CREATE TABLE IF NOT EXISTS insights (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entities(id),
    generated_at  TEXT NOT NULL,
    generated_by  INTEGER REFERENCES users(id),
    model         TEXT,
    window_days   INTEGER,
    n_grievances  INTEGER,
    product       TEXT NOT NULL,
    pattern       TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    severity      TEXT,
    item_ids      TEXT NOT NULL DEFAULT '[]'
);

/* Which items a reader has ticked off on the RD View. Per user, not per
   item: one Regional Director having read a story says nothing about
   whether another has, and the supervisory record itself is untouched --
   this is a reading mark, not a verdict. */
CREATE TABLE IF NOT EXISTS item_reads (
    item_id INTEGER NOT NULL REFERENCES items(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    read_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id        INTEGER PRIMARY KEY,
    ran_at    TEXT NOT NULL DEFAULT (datetime('now')),
    entity_id INTEGER REFERENCES entities(id),
    source    TEXT,
    found     INTEGER DEFAULT 0,
    added     INTEGER DEFAULT 0,
    merged    INTEGER DEFAULT 0,
    note      TEXT
);

-- A wrong password, remembered long enough to make the next guess cost
-- something. On a laptop nobody could reach the sign-in, so a guess was
-- free; on a public address that is no longer true.
CREATE TABLE IF NOT EXISTS login_failures (
    id       INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    ip       TEXT NOT NULL,
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures
    ON login_failures(username, ip, at);
"""


# Query parameters that identify a referrer, never the page. Stripping
# them is what makes a link shared from a phone the same link as the one
# already stored.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ncid", "cmpid",
    "ref_src", "ref_url", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id",
}
_X_HOSTS = {"x.com", "twitter.com"}
# both forms the app has built: /someone/status/123 and the
# handle-less /i/web/status/123
_X_STATUS = re.compile(r"^/(?:i/web|[^/]+)/status/(\d+)")


def canonical_url(url: str) -> str:
    """A link reduced to what identifies the thing it points at.

    Two links to one post must compare equal or it is stored twice. The
    same tweet is reachable as /someone/status/123 and /i/web/status/123 --
    which link the app built depended on whether author handles had been
    bought that day, so toggling that setting could store a tweet again
    under its other name. Reducing every X status link to one form settles
    it for stored rows too, not only for new ones.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = parts.path or ""
    drop = set(_TRACKING_PARAMS)
    if host in _X_HOSTS:
        host = "x.com"
        # ?t=&s= ride on every "copy link" from the X app
        drop |= {"s", "t"}
        match = _X_STATUS.match(path)
        if match:
            path = f"/i/status/{match.group(1)}"
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in drop and not k.lower().startswith("utm_")]
    if len(path) > 1:
        path = path.rstrip("/")
    scheme = (parts.scheme or "https").lower()
    # the fragment is dropped: it names a place inside the page, not a page
    return urlunsplit((scheme, host, path, urlencode(kept), ""))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# columns added after the first release; applied to existing databases
MIGRATIONS = [
    ("entities", "exclude_terms", "TEXT NOT NULL DEFAULT '[]'"),
    ("items", "gated_out", "INTEGER NOT NULL DEFAULT 0"),
    # NULL = attributed by alias match; 'rejected' = no alias matched, held
    # on the queue's Rejected tab for a human to overturn; 'human' = a team
    # member confirmed the item is this entity's, which outranks the gate.
    ("items", "attribution", "TEXT"),
    ("items", "gate_reason", "TEXT"),
    ("items", "source_type", "TEXT NOT NULL DEFAULT 'news'"),
    ("entities", "x_handle", "TEXT"),
    # The RBI regional office whose region holds the entity's headquarters.
    # Free text on purpose: office names are a roster, not a constraint.
    ("entities", "rbi_office", "TEXT"),
    # Headquarters, as typed on the add-entity screen. The district is
    # what resolves the RBI office; the location is context for readers.
    # A reviewer's ruling that a social post, grievance or not, is no use
    # for pattern-finding. NULL = counts; a reason code = set aside.
    ("items", "set_aside", "TEXT"),
    ("items", "set_aside_by", "TEXT"),
    ("entities", "hq_location", "TEXT"),
    ("entities", "hq_district", "TEXT"),
    # A Regional Director's beat. Set on a user instead of entity_id, it
    # scopes them to every entity of that office. A separate column rather
    # than a new role value because SQLite cannot widen the role CHECK on
    # existing databases -- the same reason items grew `attribution`.
    ("users", "rbi_office", "TEXT"),
    ("items", "review_severity", "TEXT"),
    ("items", "complaint_topics", "TEXT DEFAULT '[]'"),
    ("items", "source_tier", "TEXT NOT NULL DEFAULT ''"),
    # follow-up tracking. Added without the CHECK constraint that the fresh
    # schema carries: SQLite cannot add a constrained column to an existing
    # table, and every write goes through _set_action() anyway.
    ("items", "action_status", "TEXT"),
    ("items", "action_owner", "INTEGER REFERENCES users(id)"),
    ("items", "action_due", "TEXT"),
    ("items", "action_closed_at", "TEXT"),
    ("items", "action_closed_by", "INTEGER REFERENCES users(id)"),
    ("items", "action_close_note", "TEXT"),
    ("entities", "languages", "TEXT NOT NULL DEFAULT '[\"en\"]'"),
    # Attached sources carry their own trust tier, so the queue's trusted
    # filter can find a story whose trusted report arrived second.
    ("item_sources", "source_tier", "TEXT NOT NULL DEFAULT ''"),
    # A reviewer's correction of the complaint categories. NULL = no
    # ruling (the classifier's list shows); '[]' = ruled not a grievance.
    ("items", "review_complaint_topics", "TEXT"),
    ("reviews", "complaint_topics", "TEXT NOT NULL DEFAULT '[]'"),
    # An alert aimed at a class of entity rather than one of them or all
    # of them: "every Urban Cooperative Bank". NULL alongside a NULL
    # entity_id still means every entity, so existing alerts are unchanged.
    ("factors", "entity_kind", "TEXT"),
    # Identity, so the same complaint is recognised as the same complaint.
    # The source's own id for the post (a tweet id, a Reddit entry id).
    # More stable than the link: the same tweet has several valid links.
    ("items", "source_uid", "TEXT"),
    # The link reduced to what identifies it -- tracking parameters and
    # mobile prefixes removed. Compared instead of `url`, which stays as
    # fetched so the card still opens what the reader expects.
    ("items", "url_key", "TEXT"),
    # Who complained, as "<source>:<their id there>". Counted rather than
    # displayed: it is what separates "47 posts" from "31 complainants",
    # and one angry customer posting twenty times is still one customer.
    ("items", "author_key", "TEXT"),
    # The conversation a post belongs to, and the post it quotes, where
    # the source says so. Stored now, used to collapse threads later.
    ("items", "thread_key", "TEXT"),
    ("items", "quoted_key", "TEXT"),
    # the attached outlets are checked against a new link too, so they
    # need the same reduced form
    ("item_sources", "url_key", "TEXT"),
    # An account that may no longer sign in. Removal is a disabling, not a
    # deletion: the reviews this person recorded are supervisory record,
    # and erasing the row would take their name off their own rulings.
    ("users", "disabled", "INTEGER NOT NULL DEFAULT 0"),
    # An account that may look at everything it is entitled to see and
    # change none of it -- for showing the work to somebody without
    # handing them the Fetch button, which spends money. A column rather
    # than a fourth role for the same reason rbi_office is a column:
    # SQLite cannot widen the role CHECK on a database that exists.
    ("users", "read_only", "INTEGER NOT NULL DEFAULT 0"),
]


# Indexes over migrated columns. SCHEMA is executed before _migrate, so an
# index naming a column added by a migration cannot live there -- on an
# existing database that column does not exist yet and startup would fail.
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_action ON items(action_status, entity_id)",
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()


def _backfill_actions(con: sqlite3.Connection) -> None:
    """Give reviews recorded before follow-up tracking existed a to-do.

    Self-limiting rather than run-once-flagged: every path that sets
    review_actionable=1 now also opens an action, and the only path that
    clears an action (a review saying "not actionable") sets
    review_actionable=0 at the same time. So after the first pass no row
    can match this again.
    """
    con.execute(
        "UPDATE items SET action_status='open', action_owner=reviewed_by"
        " WHERE review_actionable = 1 AND action_status IS NULL"
    )


def _backfill_reviews(con: sqlite3.Connection) -> None:
    """Seed the history with the one review each item already carried.

    Reviews recorded before this table existed live only in the items
    row, so without this an item reviewed last week would show an empty
    history. Items that already have history rows are skipped, which
    makes this safe to run on every start.

    Only items carrying an actual ruling are seeded. The queue's Reviewed
    tick marks an item reviewed without recording one, and inventing a
    history entry for it would put a review nobody wrote into the
    supervisory trail -- the one record in this app that must never say
    something a person did not.
    """
    con.execute(
        "INSERT INTO reviews (item_id, user_id, created_at, relevant, severity,"
        " risk_areas, actionable, action, notes)"
        " SELECT id, reviewed_by, reviewed_at, review_relevant, review_severity,"
        "        COALESCE(review_risk_areas, '[]'), review_actionable, review_action,"
        "        review_notes"
        " FROM items"
        " WHERE reviewed_at IS NOT NULL"
        "   AND review_relevant IS NOT NULL"
        "   AND id NOT IN (SELECT item_id FROM reviews)"
    )


def _drop_phantom_reviews(con: sqlite3.Connection) -> None:
    """Remove review-history entries no person ever wrote.

    For two builds the queue's Reviewed tick set an item's status without
    recording a ruling, and the backfill above then seeded a history row
    for it -- a row in the supervisory trail saying a review happened
    when only a box was ticked. Such a row is identifiable exactly: the
    item carries no verdict, and the row carries no verdict either, which
    a real review never does (the form always writes relevant as 0 or 1).
    Runs on every start and finds nothing once the estate is clean.
    """
    con.execute(
        "DELETE FROM reviews WHERE relevant IS NULL AND severity IS NULL"
        "  AND action IS NULL AND notes IS NULL"
        "  AND item_id IN (SELECT id FROM items WHERE review_relevant IS NULL)"
    )


def _backfill_social_window(con: sqlite3.Connection) -> None:
    """Gate the social items a pre-window fetch already stored.

    The first real fetch ran before social sources were limited to the
    last 365 days, so the queue holds years-old forum complaints wearing
    their fetch time as a date. Self-limiting, like the other backfills:
    the collectors no longer store undated or out-of-window social items,
    and classification now gates no-grievance social posts itself, so
    after one pass nothing can match again. Reviewed items are left
    alone -- a human's judgment outranks a window.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    con.execute(
        "UPDATE items SET gated_out=1,"
        " gate_reason='social: undated or older than the 365-day window'"
        " WHERE source_type='social' AND gated_out=0 AND status!='reviewed'"
        " AND (published_at IS NULL OR published_at < ?)", (cutoff,))
    con.execute(
        "UPDATE items SET gated_out=1,"
        " gate_reason='social post with no grievance in it'"
        " WHERE source_type='social' AND gated_out=0 AND status!='reviewed'"
        " AND classifier='llm'"
        " AND (complaint_topics IS NULL OR complaint_topics IN ('', '[]'))")


def _backfill_url_keys(con) -> None:
    """Give every stored row the reduced form of its link.

    Without this the identity check would only work for rows fetched from
    now on, and the oldest rows -- the ones most likely to be met again
    under a second link -- would be exactly the ones it could not
    recognise. Runs once per row: the column is only NULL before this.
    """
    rows = con.execute("SELECT id, url FROM items"
                       " WHERE url_key IS NULL AND url IS NOT NULL").fetchall()
    if rows:
        con.executemany("UPDATE items SET url_key = ? WHERE id = ?",
                        [(canonical_url(r["url"]), r["id"]) for r in rows])
        log.info("Recorded the comparable form of %d stored link(s)", len(rows))
    src = con.execute("SELECT id, url FROM item_sources"
                      " WHERE url_key IS NULL AND url IS NOT NULL").fetchall()
    if src:
        con.executemany("UPDATE item_sources SET url_key = ? WHERE id = ?",
                        [(canonical_url(r["url"]), r["id"]) for r in src])


def init_db() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        for stmt in POST_MIGRATION_INDEXES:
            con.execute(stmt)
        _backfill_actions(con)
        _backfill_reviews(con)
        _drop_phantom_reviews(con)
        _backfill_social_window(con)
        _backfill_url_keys(con)
        con.commit()
    finally:
        con.close()


def remove_entity(db: sqlite3.Connection, entity_id: int) -> None:
    """Delete an entity and everything belonging to it, in one transaction.

    Lives here rather than in a route so the Entities page and
    scripts/set_roster.py cannot drift apart on what "removing an entity"
    means. Kept deliberately: user accounts (their team link is cleared),
    global factors, and every setting.
    """
    with db:
        db.execute("DELETE FROM reviews WHERE item_id IN"
                   " (SELECT id FROM items WHERE entity_id = ?)", (entity_id,))
        db.execute("DELETE FROM item_sources WHERE item_id IN"
                   " (SELECT id FROM items WHERE entity_id = ?)", (entity_id,))
        db.execute("DELETE FROM item_reads WHERE item_id IN"
                   " (SELECT id FROM items WHERE entity_id = ?)", (entity_id,))
        db.execute("DELETE FROM items WHERE entity_id = ?", (entity_id,))
        db.execute("DELETE FROM factors WHERE entity_id = ?", (entity_id,))
        db.execute("DELETE FROM fetch_log WHERE entity_id = ?", (entity_id,))
        db.execute("UPDATE users SET entity_id = NULL WHERE entity_id = ?", (entity_id,))
        db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))


def get_setting(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db: sqlite3.Connection, key: str, value: str, user_id: int) -> None:
    db.execute(
        "INSERT INTO settings (key, value, updated_by) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=datetime('now'), updated_by=excluded.updated_by",
        (key, value, user_id),
    )
    db.commit()


def q(db: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    return db.execute(sql, params).fetchall()


def one(db: sqlite3.Connection, sql: str, params=()):
    return db.execute(sql, params).fetchone()


def x(db: sqlite3.Connection, sql: str, params=()) -> int:
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid
