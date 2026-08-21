"""SQLite storage. One file, zero setup — right-sized for the prototype.

Connections are opened per request/thread (SQLite connections are not
thread-safe to share). WAL mode lets the background fetcher write while
the web app reads.
"""
import os
import sqlite3
from pathlib import Path

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
    review_actionable INTEGER,
    review_action     TEXT,
    review_notes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_entity ON items(entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_entity_url ON items(entity_id, url);

-- additional outlets reporting the same underlying event
CREATE TABLE IF NOT EXISTS item_sources (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    source_name  TEXT,
    title        TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_item_sources_item ON item_sources(item_id);

CREATE TABLE IF NOT EXISTS factors (
    id         INTEGER PRIMARY KEY,
    entity_id  INTEGER REFERENCES entities(id),   -- NULL = applies to all entities
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
"""


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
    ("items", "gate_reason", "TEXT"),
    ("items", "source_type", "TEXT NOT NULL DEFAULT 'news'"),
    ("entities", "x_handle", "TEXT"),
    ("items", "review_severity", "TEXT"),
    ("items", "complaint_topics", "TEXT DEFAULT '[]'"),
    ("items", "source_tier", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()


def init_db() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


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
