"""store.py

Persistence for score.py: SQLite + optional JSONL.

SQLite holds one row per registrable domain (upsert, so re-runs update rather
than duplicate), plus the raw REST index in a separate table so re-scoring and
the future preview generator never need to re-fetch. JSONL is an append-only
export for when you want a flat file.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    registrable      TEXT PRIMARY KEY,
    host             TEXT,
    url              TEXT,
    status           TEXT,
    http_status      INTEGER,
    wordpress        INTEGER,
    wp_signatures    TEXT,
    wp_version       TEXT,
    rest_status      TEXT,
    woo_store        INTEGER,
    route_count      INTEGER,
    namespace_count  INTEGER,
    post_count       INTEGER,
    page_count       INTEGER,
    product_count    INTEGER,
    score            REAL,
    tier             TEXT,
    flags            TEXT,
    score_version    TEXT,
    first_seen       TEXT,
    scored_at        TEXT,
    facts            TEXT,
    reasons          TEXT,
    penalties        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_score ON sites(score DESC);
CREATE INDEX IF NOT EXISTS idx_sites_tier ON sites(tier);
CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status);

CREATE TABLE IF NOT EXISTS rest_index (
    registrable  TEXT PRIMARY KEY,
    fetched_at   TEXT,
    body         BLOB
);
"""

COLUMNS = [
    "registrable", "host", "url", "status", "http_status", "wordpress",
    "wp_signatures", "wp_version", "rest_status", "woo_store", "route_count",
    "namespace_count", "post_count", "page_count", "product_count", "score",
    "tier", "flags", "score_version", "first_seen", "scored_at", "facts",
    "reasons", "penalties",
]

JSON_FIELDS = {"wp_signatures", "flags", "facts", "reasons", "penalties"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def known_domains(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT registrable FROM sites")}


def save(con: sqlite3.Connection, row: dict[str, Any], rest_body: bytes | None = None) -> None:
    now = _now()
    reg = row.get("registrable")
    if not reg:
        return

    existing = con.execute("SELECT first_seen FROM sites WHERE registrable = ?", (reg,)).fetchone()
    first_seen = existing[0] if existing else now

    values = {}
    for col in COLUMNS:
        if col == "first_seen":
            values[col] = first_seen
        elif col == "scored_at":
            values[col] = now
        elif col in JSON_FIELDS:
            values[col] = json.dumps(row.get(col), default=str)
        else:
            values[col] = row.get(col)

    placeholders = ", ".join("?" for _ in COLUMNS)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "registrable")
    con.execute(
        f"INSERT INTO sites ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(registrable) DO UPDATE SET {assignments}",
        [values[c] for c in COLUMNS],
    )

    if rest_body is not None:
        con.execute(
            "INSERT INTO rest_index (registrable, fetched_at, body) VALUES (?, ?, ?) "
            "ON CONFLICT(registrable) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
            (reg, now, gzip.compress(rest_body)),
        )

    con.commit()


def append_jsonl(path: str, row: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
