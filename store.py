"""store.py

Persistence for score.py: SQLite + optional JSONL.

`sites` holds one full row per WordPress site (upsert, so re-runs update rather
than duplicate), plus the raw REST index in a separate table so re-scoring and
the future preview generator never need to re-fetch. Non-WordPress domains are
deliberately not stored there.

`seen` is a minimal crawl ledger: one row per domain actually checked, with no
page content or signals. It lets --batch move forward across runs and keeps the
funnel counts honest. JSONL is an append-only export for when you want a flat
file.

`jobs` + `job_domains` queue uploads for the Telegram runner: a job is one CSV
and its own set of domains, so reports can be scoped to a single upload while
scores are still reused across jobs. `kv` stores crawl state such as the
Telegram update offset.
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
    requested        TEXT,
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

-- Crawl ledger. One minimal row per domain actually checked, including the
-- ones that were not WordPress (which never touch `sites`). Holds no page
-- content, no facts and no reasons: it exists so --batch advances instead of
-- re-fetching the same 200 non-WordPress domains forever, and so the funnel
-- (checked -> 200 -> WordPress) stays exact.
CREATE TABLE IF NOT EXISTS seen (
    registrable  TEXT PRIMARY KEY,
    requested    TEXT,
    status       TEXT,
    tier         TEXT,
    score        REAL,
    wordpress    INTEGER,
    first_seen   TEXT,
    checked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_status ON seen(status);
CREATE INDEX IF NOT EXISTS idx_seen_wordpress ON seen(wordpress);

-- Upload jobs. One row per CSV received from Telegram (or --ingest-file),
-- tracking it from receipt through processing to delivery. `job_domains` maps
-- every domain in that CSV (including ones already checked by an earlier job)
-- so a report can be scoped to exactly one upload.
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    chat_id       INTEGER,
    message_id    INTEGER,
    file_name     TEXT,
    sha256        TEXT UNIQUE,
    bytes         INTEGER,
    status        TEXT NOT NULL DEFAULT 'received',
    received_at   TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    total_domains INTEGER NOT NULL DEFAULT 0,
    checked       INTEGER NOT NULL DEFAULT 0,
    wordpress     INTEGER NOT NULL DEFAULT 0,
    leads         INTEGER NOT NULL DEFAULT 0,
    deferred      INTEGER NOT NULL DEFAULT 0,
    blocked       INTEGER NOT NULL DEFAULT 0,
    report_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, received_at);

CREATE TABLE IF NOT EXISTS job_domains (
    job_id       TEXT NOT NULL,
    registrable  TEXT NOT NULL,
    PRIMARY KEY (job_id, registrable)
);
CREATE INDEX IF NOT EXISTS idx_job_domains_registrable ON job_domains(registrable);

-- Small key/value store for crawl state that is not domain-specific, such as
-- the Telegram getUpdates offset.
CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

COLUMNS = [
    "registrable", "requested", "host", "url", "status", "http_status", "wordpress",
    "wp_signatures", "wp_version", "rest_status", "woo_store", "route_count",
    "namespace_count", "post_count", "page_count", "product_count", "score",
    "tier", "flags", "score_version", "first_seen", "scored_at", "facts",
    "reasons", "penalties",
]

JSON_FIELDS = {"wp_signatures", "flags", "facts", "reasons", "penalties"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    # `requested` records the domain the user asked about, which is not always
    # the domain the crawl ended on: a directory entry like hanleys.ie
    # redirects to djhanleys.ie, and job scoping must stay on the submitted
    # name. Added after the first release, so existing DBs need the ALTERs.
    _ensure_column(con, "sites", "requested", "TEXT")
    _ensure_column(con, "seen", "requested", "TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_seen_requested ON seen(requested)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sites_requested ON sites(requested)")
    # Backfill the ledger from any rows scored before the ledger existed.
    con.execute(
        "INSERT OR IGNORE INTO seen (registrable, status, tier, score, wordpress, first_seen, checked_at) "
        "SELECT registrable, status, tier, score, COALESCE(wordpress, 1), "
        "       COALESCE(first_seen, scored_at), COALESCE(scored_at, first_seen) FROM sites"
    )
    con.commit()
    return con


def known_domains(con: sqlite3.Connection) -> set[str]:
    """Every domain already checked, WordPress or not.

    Both identifiers count: the submitted name (so the same CSV does not crawl
    again) and the final name (so a second directory entry pointing at the same
    site is not crawled twice).
    """
    return {
        row[0]
        for row in con.execute(
            "SELECT registrable FROM seen "
            "UNION SELECT requested FROM seen WHERE requested IS NOT NULL"
        )
    }


def retryable_domains(con: sqlite3.Connection) -> set[str]:
    """Domains whose last attempt failed in a way a retry might fix."""
    domains: set[str] = set()
    for row in con.execute(
        "SELECT registrable, requested FROM seen "
        "WHERE status IN ('blocked', 'unreachable', 'http_error')"
    ):
        domains.update(value for value in row if value)
    return domains


def resolve_domain(con: sqlite3.Connection, domain: str) -> str | None:
    """Find the stored final domain for a submitted one (identity lookup)."""
    row = con.execute(
        "SELECT registrable FROM seen WHERE registrable = ? "
        "UNION SELECT registrable FROM seen WHERE requested = ? LIMIT 1",
        (domain, domain),
    ).fetchone()
    return row[0] if row else None


def mark_seen(
    con: sqlite3.Connection,
    registrable: str | None,
    status: str,
    tier: str,
    score: float | None,
    wordpress: bool | None,
    requested: str | None = None,
    keep_better: bool = False,
) -> None:
    if not registrable:
        return
    if keep_better:
        # A rescan must not erase a good result because the site timed out this
        # time. Transient failures keep the previous "scored" row.
        existing = con.execute(
            "SELECT status FROM seen WHERE registrable = ?", (registrable,)
        ).fetchone()
        if existing and existing[0] == "scored" and status in (
            "unreachable", "blocked", "http_error"
        ):
            return
    now = _now()
    wp = None if wordpress is None else int(wordpress)
    con.execute(
        "INSERT INTO seen (registrable, requested, status, tier, score, wordpress, first_seen, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(registrable) DO UPDATE SET "
        "requested = COALESCE(excluded.requested, seen.requested), "
        "status = excluded.status, tier = excluded.tier, score = excluded.score, "
        "wordpress = excluded.wordpress, checked_at = excluded.checked_at",
        (registrable, requested, status, tier, score, wp, now, now),
    )
    con.commit()


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


# --- upload jobs ---------------------------------------------------------


def job_by_hash(con: sqlite3.Connection, sha256: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM jobs WHERE sha256 = ?", (sha256,)).fetchone()
    return dict(row) if row else None


def get_job(con: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(con: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM jobs ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_job(
    con: sqlite3.Connection,
    job_id: str,
    chat_id: int | None,
    message_id: int | None,
    file_name: str,
    sha256: str,
    size: int,
) -> bool:
    """Insert a queued upload. Returns False if the hash is already known."""
    cur = con.execute(
        "INSERT OR IGNORE INTO jobs "
        "(job_id, chat_id, message_id, file_name, sha256, bytes, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'received', ?)",
        (job_id, chat_id, message_id, file_name, sha256, size, _now()),
    )
    con.commit()
    return cur.rowcount > 0


def claim_next_job(con: sqlite3.Connection,
                   exclude: set[str] | None = None) -> dict[str, Any] | None:
    """Return the oldest received job, else the oldest unfinished one.

    Claiming is not locked: the runner is a single cron process, so only one
    consumer exists at a time. Railway skips an overlapping tick anyway.
    `exclude` keeps one tick from picking the same deferred job over and over.
    """
    skip = tuple(sorted(exclude or ()))
    received_sql = "SELECT * FROM jobs WHERE status = 'received'"
    processing_sql = "SELECT * FROM jobs WHERE status = 'processing'"
    if skip:
        marks = ", ".join("?" for _ in skip)
        received_sql += f" AND job_id NOT IN ({marks})"
        processing_sql += f" AND job_id NOT IN ({marks})"
    row = con.execute(received_sql + " ORDER BY received_at LIMIT 1", skip).fetchone()
    if row is None:
        row = con.execute(processing_sql + " ORDER BY started_at LIMIT 1", skip).fetchone()
    if row is None:
        return None
    now = _now()
    con.execute(
        "UPDATE jobs SET status = 'processing', started_at = COALESCE(started_at, ?) "
        "WHERE job_id = ?",
        (now, row["job_id"]),
    )
    con.commit()
    return get_job(con, row["job_id"])


def set_job_status(
    con: sqlite3.Connection,
    job_id: str,
    status: str,
    error: str | None = None,
    finished: bool = False,
    report_path: str | None = None,
) -> None:
    con.execute(
        "UPDATE jobs SET status = ?, error = ?, "
        "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, "
        "report_path = COALESCE(?, report_path) "
        "WHERE job_id = ?",
        (status, error, int(finished), _now() if finished else None, report_path, job_id),
    )
    con.commit()


def retry_job(con: sqlite3.Connection, job_id: str) -> bool:
    cur = con.execute(
        "UPDATE jobs SET status = 'received', error = NULL, started_at = NULL, "
        "finished_at = NULL, report_path = NULL WHERE job_id = ?",
        (job_id,),
    )
    con.commit()
    return cur.rowcount > 0


def add_job_domains(con: sqlite3.Connection, job_id: str, registrables: set[str]) -> int:
    con.executemany(
        "INSERT OR IGNORE INTO job_domains (job_id, registrable) VALUES (?, ?)",
        [(job_id, reg) for reg in sorted(registrables)],
    )
    con.commit()
    return len(registrables)


def job_domain_set(con: sqlite3.Connection, job_id: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT registrable FROM job_domains WHERE job_id = ?", (job_id,)
        )
    }


def job_counts(con: sqlite3.Connection, job_id: str, lead_score: float = 40.0) -> dict[str, int]:
    row = con.execute(
        "SELECT COUNT(DISTINCT jd.registrable) AS total, "
        "  COUNT(DISTINCT CASE WHEN s.registrable IS NOT NULL THEN jd.registrable END) AS checked, "
        "  COUNT(DISTINCT CASE WHEN s.wordpress = 1 THEN jd.registrable END) AS wordpress, "
        "  COUNT(DISTINCT CASE WHEN st.score >= ? THEN jd.registrable END) AS leads, "
        "  COUNT(DISTINCT CASE WHEN s.status IN ('blocked', 'unreachable', 'http_error') "
        "       THEN jd.registrable END) AS blocked "
        "FROM job_domains jd "
        "LEFT JOIN seen s ON s.registrable = jd.registrable OR s.requested = jd.registrable "
        "LEFT JOIN sites st ON st.registrable = jd.registrable OR st.requested = jd.registrable "
        "WHERE jd.job_id = ?",
        (lead_score, job_id),
    ).fetchone()
    total = int(row["total"] or 0)
    counts = {
        "total": total,
        "checked": int(row["checked"] or 0),
        "wordpress": int(row["wordpress"] or 0),
        "leads": int(row["leads"] or 0),
        "blocked": int(row["blocked"] or 0),
    }
    counts["deferred"] = max(0, total - counts["checked"])
    return counts


def save_job_counts(con: sqlite3.Connection, job_id: str, counts: dict[str, int]) -> None:
    con.execute(
        "UPDATE jobs SET total_domains = ?, checked = ?, wordpress = ?, "
        "leads = ?, deferred = ?, blocked = ? WHERE job_id = ?",
        (
            counts.get("total", 0), counts.get("checked", 0),
            counts.get("wordpress", 0), counts.get("leads", 0),
            counts.get("deferred", 0), counts.get("blocked", 0), job_id,
        ),
    )
    con.commit()


# --- generic crawl state -------------------------------------------------


def get_kv(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_kv(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    con.commit()
