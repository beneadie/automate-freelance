#!/usr/bin/env python3
"""run_tick.py

One Railway cron tick: ingest Telegram uploads, then process queued jobs.

    python run_tick.py
    python run_tick.py --dry-run --ingest-file data/sample.csv
    python run_tick.py --dry-run --ingest-file data/sample.csv --no-process
    python run_tick.py --rescore 20260101-120000-ab12cd   # re-crawl one job
    python run_tick.py --resend  20260101-120000-ab12cd   # re-send without crawling

A tick is intentionally short-lived and exits cleanly so Railway's cron
scheduler can start the next one. Jobs that do not finish within
DOMAINS_PER_TICK remain `processing` and resume on the next tick; because
score.py's crawl ledger is global, already-checked domains are never fetched
twice.

Environment:
    TELEGRAM_BOT_TOKEN          bot token from BotFather (required unless --dry-run)
    TELEGRAM_ALLOWED_CHAT_IDS   comma-separated chat IDs allowed to use the bot
    DATA_DIR                    persistent root (default: data)
    DB_PATH                     override SQLite path (default: $DATA_DIR/leads.db)
    SCORE_WORKERS               concurrent fetches per tick (default: 4)
    DOMAINS_PER_TICK             domains scored per job per tick (default: 150)
    MAX_JOBS_PER_TICK           jobs touched per tick (default: 3)
    LEAD_SCORE                  score threshold counted as a lead (default: 40)
    TOP_SUMMARY                 sites in the final digest message (default: 20, 0 disables)

Reports sent back to Telegram contain WordPress sites only, delivered as one
message per site, best score first. Non-WordPress, blocked and deferred domains
appear only as counts in the summary, so the funnel stays honest without noise.

Settings are read from the environment, then from a `.env` file next to this
script. Real environment variables always win, so Railway secrets are not
overridden by a stray local file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import score
import store
from telegram_api import TelegramClient, TelegramError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
SEND_INTERVAL = 1.05      # Telegram tolerates roughly one message/second per chat
SEND_RETRIES = 3          # extra attempts after a 429 on a single message
HELP = (
    "Send me a business-directory CSV and I will score it at the next run.\n\n"
    "I check every domain, then send one message per WordPress site, best "
    "score first.\n\n"
    "/status - recent jobs\n"
    "/retry <job_id> - run a job again\n"
    "/rescore <job_id> - re-crawl a job with the current scoring\n"
    "/help - this message\n\n"
    "CSV up to 20 MB. Excel files are not read: use File > Save As > CSV."
)


@dataclass
class Config:
    data_dir: str
    db_path: str
    upload_dir: str
    allowed_chats: set[int] = field(default_factory=set)
    workers: int = 4
    domains_per_tick: int = 150
    max_jobs_per_tick: int = 3
    lead_score: float = 40.0
    dry_run: bool = False


class LoggingClient:
    """Stand-in for TelegramClient in --dry-run: prints instead of sending."""

    def send_message(self, chat_id: int | str, text: str) -> None:
        print(f"[dry-run message -> {chat_id}]\n{text}\n")


def load_dotenv(path: str | None = None) -> None:
    """Load KEY=VALUE lines from .env without overriding the real environment.

    Deployment secrets must win, so an existing os.environ entry is never
    replaced. Missing files are ignored, which keeps --dry-run dependency-free.
    """
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_chat_ids(raw: str) -> set[int]:
    chats: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        try:
            chats.add(int(part))
        except ValueError:
            continue
    return chats


def build_config(args: argparse.Namespace) -> Config:
    data_dir = os.path.abspath(args.data_dir or os.environ.get("DATA_DIR", "data"))
    db_path = os.path.abspath(
        args.db or os.environ.get("DB_PATH", os.path.join(data_dir, "leads.db"))
    )
    return Config(
        data_dir=data_dir,
        db_path=db_path,
        upload_dir=os.path.join(data_dir, "uploads"),
        allowed_chats=_parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        workers=args.workers if args.workers is not None else _env_int("SCORE_WORKERS", 4),
        domains_per_tick=(args.domains_per_tick if args.domains_per_tick is not None
                          else _env_int("DOMAINS_PER_TICK", 150)),
        max_jobs_per_tick=(args.max_jobs if args.max_jobs is not None
                           else _env_int("MAX_JOBS_PER_TICK", 3)),
        lead_score=_env_float("LEAD_SCORE", 40.0),
        dry_run=args.dry_run,
    )


# --- ingestion -----------------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _notify(client: Any, chat_id: int | None, text: str) -> bool:
    """Send a message, swallowing failures. Returns True when it went out."""
    if chat_id in (None, 0):
        return False
    try:
        client.send_message(chat_id, text)
        return True
    except TelegramError as exc:
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return False


def announce_chat(client: Any, con: Any, chat_id: int | None) -> None:
    """Reply once to an unknown chat with its ID, so onboarding needs no tools.

    Without this the allowlist is a chicken-and-egg problem: you cannot be told
    your chat ID by the bot that refuses to talk to you. The kv flag keeps it to
    one message per chat, and nothing but the ID is revealed.
    """
    if chat_id in (None, 0):
        return
    key = f"notified_chat_{chat_id}"
    if store.get_kv(con, key):
        return
    sent = _notify(
        client, chat_id,
        f"Your chat ID is {chat_id}.\n\n"
        "Add it to TELEGRAM_ALLOWED_CHAT_IDS and I will score the CSV files "
        "you send me.",
    )
    if sent:
        store.set_kv(con, key, "1")


def _csv_name(name: str) -> str:
    """Undo Telegram/Android decorations like Galway.csv.xlsx."""
    lowered = name.lower()
    if lowered.endswith(".csv"):
        return name
    for suffix in (".xlsx", ".xls", ".xlsm", ".txt", ".tsv"):
        if lowered.endswith(suffix) and lowered[: -len(suffix)].endswith(".csv"):
            return name[: -len(suffix)]
    return name


def sniff_kind(path: str, sample: int = 8192) -> str:
    """Classify a file by content: 'csv', 'excel' or 'unknown'.

    Android and some Telegram clients append `.xlsx` and set an Excel MIME
    type even when the bytes are plain CSV, so the extension cannot be
    trusted. Real workbooks are rejected explicitly rather than parsed.
    """
    with open(path, "rb") as fh:
        head = fh.read(sample)
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0"):
        return "excel"
    if b"\x00" in head:
        return "unknown"
    text = ""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = head.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first = next((line for line in text.splitlines() if line.strip()), "")
    if first and any(delimiter in first for delimiter in (",", ";", "\t", "|")):
        return "csv"
    return "unknown"


def ingest_file(client: Any, con: Any, cfg: Config, source: str,
                chat_id: int | None = None, message_id: int | None = None,
                original_name: str | None = None) -> str | None:
    """Move a CSV into the queue. Returns the job ID, or None on rejection."""
    file_name = _csv_name(original_name or os.path.basename(source))
    size = os.path.getsize(source)
    if size > MAX_DOCUMENT_BYTES:
        _notify(client, chat_id, f"{file_name}: {size / 1e6:.1f} MB exceeds the 20 MB limit.")
        return None

    kind = sniff_kind(source)
    if kind == "excel":
        _notify(client, chat_id,
                f"{file_name}: that is an Excel workbook, which I cannot read. "
                "In Excel use File > Save As > CSV, then send that file.")
        return None
    if kind != "csv":
        _notify(client, chat_id,
                f"{file_name}: I could not find CSV rows in that file. "
                "Send a .csv with a header row and a website column.")
        return None

    digest = _sha256(source)
    existing = store.job_by_hash(con, digest)
    if existing:
        _notify(client, chat_id,
                f"{file_name}: already received as job {existing['job_id']} "
                f"({existing['status']}). Use /retry {existing['job_id']} to run it again.")
        return existing["job_id"]

    job_id = _new_job_id()
    job_dir = os.path.join(cfg.upload_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    dest = os.path.join(job_dir, "source.csv")
    if os.path.abspath(source) != os.path.abspath(dest):
        shutil.copyfile(source, dest)

    if not store.create_job(con, job_id, chat_id, message_id, file_name, digest, size):
        shutil.rmtree(job_dir, ignore_errors=True)
        _notify(client, chat_id, f"{file_name}: duplicate upload, ignored.")
        return None

    _notify(client, chat_id,
            f"{file_name}: received as job {job_id}. "
            f"{cfg.domains_per_tick} domains will be scored per run; "
            "I will send one message per WordPress site when it is done.")
    return job_id


def ingest_document(client: Any, con: Any, cfg: Config, message: dict) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    document = message.get("document") or {}
    name = _csv_name(document.get("file_name") or "upload.csv")
    size = int(document.get("file_size") or 0)
    if size and size > MAX_DOCUMENT_BYTES:
        _notify(client, chat_id, f"{name}: {size / 1e6:.1f} MB exceeds the 20 MB limit.")
        return

    os.makedirs(cfg.upload_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".incoming-", suffix=".csv", dir=cfg.upload_dir)
    os.close(fd)
    try:
        client.download_file(document["file_id"], temp_path)
        ingest_file(client, con, cfg, temp_path, chat_id,
                    message.get("message_id"), original_name=name)
    except TelegramError as exc:
        _notify(client, chat_id, f"{name}: could not download the file ({exc}). Try again.")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def command_reply(con: Any, cfg: Config, text: str) -> str:
    words = text.split()
    command = words[0].split("@", 1)[0].lower() if words else ""
    if command in ("/start", "/help"):
        return HELP
    if command == "/status":
        jobs = store.list_jobs(con, limit=8)
        if not jobs:
            return "No jobs yet. Send me a CSV."
        lines = ["Recent jobs:"]
        for job in jobs:
            # Stored counts are written when the crawl exits, so a running job
            # would otherwise report 0/0 and look dead. Compute those live.
            if job["status"] in ("received", "processing"):
                live = store.job_counts(con, job["job_id"], cfg.lead_score)
                job["total_domains"] = live["total"]
                job["checked"] = live["checked"]
                job["wordpress"] = live["wordpress"]
                job["leads"] = live["leads"]
            lines.append(
                f"{job['job_id']}  {job['status']:<10}  "
                f"{job['checked']}/{job['total_domains']} checked  "
                f"{job['wordpress']} WP  {job['leads']} leads"
                + (f"  error: {job['error']}" if job["error"] else "")
            )
        return "\n".join(lines)
    if command == "/retry":
        if len(words) < 2:
            return "Usage: /retry <job_id>"
        job_id = words[1]
        if not store.get_job(con, job_id):
            return f"No job {job_id}."
        store.retry_job(con, job_id)
        return f"Job {job_id} queued again."
    if command == "/rescore":
        if len(words) < 2:
            return "Usage: /rescore <job_id>"
        job_id = words[1]
        job = store.get_job(con, job_id)
        if job is None:
            return f"No job {job_id}."
        store.retry_job(con, job_id)
        store.set_kv(con, f"rescore_{job_id}", "1")
        store.set_kv(con, f"sending_{job_id}", "0")
        return (f"Job {job_id} queued for re-scoring. The next run re-crawls all "
                f"{job.get('total_domains') or 'its'} domains with the current model.")
    return HELP


def poll_telegram(client: Any, con: Any, cfg: Config) -> int:
    raw = store.get_kv(con, "telegram_offset")
    offset = int(raw) if raw and raw.isdigit() else None
    try:
        updates = client.get_updates(offset=offset, timeout=0)
    except TelegramError as exc:
        print(f"getUpdates failed: {exc}", file=sys.stderr)
        return 0

    handled = 0
    for update in updates:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        try:
            if chat_id not in cfg.allowed_chats:
                announce_chat(client, con, chat_id)
                continue
            text = (message.get("text") or "").strip()
            if text:
                _notify(client, chat_id, command_reply(con, cfg, text))
            elif message.get("document"):
                ingest_document(client, con, cfg, message)
                handled += 1
        except Exception as exc:  # one bad update must not wedge the queue
            print(f"update {update.get('update_id')} failed: {exc}", file=sys.stderr)
            _notify(client, chat_id, f"That upload failed: {exc}")
        finally:
            # Advance even on failure: re-sending is a better recovery than a
            # poison message blocking every future upload.
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                store.set_kv(con, "telegram_offset", str(update_id + 1))
    return handled


# --- processing ----------------------------------------------------------


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        detail = " | ".join(tail) or f"exit {proc.returncode}"
        raise RuntimeError(f"{os.path.basename(cmd[1])}: {detail}")
    return proc.stdout


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _score_of(row: dict[str, str]) -> float:
    try:
        return float(row.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def report_rows(path: str) -> list[dict[str, str]]:
    """Read report.py's all_wordpress.csv, best score first."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda row: (-_score_of(row), _clean(row.get("domain"))))
    return rows


def site_message(rank: int, total: int, row: dict[str, str]) -> str:
    """One compact message for one WordPress site. Plain text, no markdown."""
    score = _clean(row.get("score"))
    try:
        score = f"{float(score):g}"
    except ValueError:
        score = score or "?"
    lines = [f"#{rank} of {total} · {score} pts · Tier {_clean(row.get('tier')) or '-'}"]

    name = _clean(row.get("name"))
    if name:
        lines.append(name)
    place = " · ".join(p for p in (_clean(row.get("town")),
                                   _clean(row.get("category"))) if p)
    if place:
        lines.append(place)
    domain = _clean(row.get("domain"))
    if domain:
        lines.append(domain)

    tech = []
    version = _clean(row.get("wp_version"))
    if version:
        tech.append(f"WP {version}")
    tech.append("Woo" if _clean(row.get("woocommerce")) else "no Woo")
    for key, label in (("posts", "posts"), ("pages", "pages"), ("products", "products")):
        value = _clean(row.get(key))
        if value and value != "0":
            tech.append(f"{value} {label}")
    lines.append(" · ".join(tech))

    contact = " · ".join(p for p in (_clean(row.get("email")),
                                     _clean(row.get("phone"))) if p)
    if contact:
        lines.append(contact)
    reasons = _clean(row.get("top_reasons"))
    if reasons:
        lines.append(reasons)
    penalties = _clean(row.get("penalties"))
    if penalties:
        lines.append(f"penalties: {penalties}")
    return "\n".join(lines)


def send_site_messages(client: Any, chat_id: int | str, rows: list[dict[str, str]],
                       sleep: Any = time.sleep, interval: float = SEND_INTERVAL,
                       start: int = 0, on_sent: Any = None) -> int:
    """Send one message per site, paced under Telegram's per-chat flood limit.

    A 429 carries retry_after; sleeping that long is what keeps a 100-message
    report from being silently truncated by a temporary block. Transient
    socket errors get a short backoff instead of aborting the whole report.
    `start` resumes after a partially delivered run, and `on_sent` records each
    success so the next attempt never repeats a message.
    """
    total = len(rows)
    for index, row in enumerate(rows[start:], start + 1):
        text = site_message(index, total, row)
        for attempt in range(SEND_RETRIES + 1):
            try:
                client.send_message(chat_id, text)
                break
            except TelegramError as exc:
                if attempt >= SEND_RETRIES:
                    raise
                if exc.retry_after is not None:
                    sleep(min(float(exc.retry_after) + 1.0, 120.0))
                else:
                    sleep(min(2.0 * (attempt + 1), 10.0))
        if on_sent is not None:
            on_sent(index)
        if interval and index < total:
            sleep(interval)
    return total


def result_text(job: dict, counts: dict[str, int], cfg: Config) -> str:
    """Short summary used for failures and intermediate progress notes."""
    return "\n".join([
        f"Job {job['job_id']} — {job['status']}",
        f"File: {job['file_name']}",
        f"Domains: {counts['checked']}/{counts['total']} checked"
        + (f" ({counts['deferred']} deferred)" if counts["deferred"] else ""),
        f"WordPress: {counts['wordpress']}",
        f"Leads (score >= {cfg.lead_score:g}): {counts['leads']}",
        f"Blocked/unreachable: {counts['blocked']}",
    ])


def top_summary_message(job: dict, counts: dict[str, int],
                        rows: list[dict[str, str]], cfg: Config) -> str:
    """One actionable digest of the best sites, sent after the detail messages."""
    limit = _env_int("TOP_SUMMARY", 20)
    if limit <= 0 or not rows:
        return ""
    shown = rows[:limit]
    lines = [
        f"TOP {len(shown)} — job {job['job_id']}",
        f"{counts['checked']}/{counts['total']} checked · {counts['wordpress']} WordPress · "
        f"{counts['leads']} leads (score >= {cfg.lead_score:g})",
        "",
    ]
    for index, row in enumerate(shown, 1):
        score = _clean(row.get("score"))
        try:
            score = f"{float(score):g}"
        except ValueError:
            score = score or "?"
        name = _clean(row.get("name"))[:36]
        head = (f"{index}. {score} · {_clean(row.get('tier'))} · "
                f"{name} — {_clean(row.get('domain'))}")
        contact = _clean(row.get("email")) or _clean(row.get("phone"))
        if contact:
            head += f" · {contact[:34]}"
        lines.append(head)
        reasons = _clean(row.get("top_reasons"))
        if reasons:
            lines.append(f"   {reasons[:100]}")
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"{len(rows) - len(shown)} more in the site messages above.")
    return "\n".join(lines)


def send_report(client: Any, con: Any, job: dict, counts: dict[str, int],
                report_path: str | None, cfg: Config) -> int:
    """Deliver one message per WordPress site. No attachment is ever sent."""
    chat_id = job.get("chat_id")
    rows = report_rows(report_path) if report_path and os.path.exists(report_path) else []
    progress_key = f"sending_{job['job_id']}"
    delivered = int(store.get_kv(con, progress_key) or 0)
    delivered = max(0, min(delivered, len(rows)))

    header = "\n".join([
        f"Job {job['job_id']} — {_clean(job.get('file_name'))}",
        f"{counts['checked']}/{counts['total']} domains checked · "
        f"{counts['wordpress']} WordPress · {counts['leads']} leads "
        f"(score >= {cfg.lead_score:g}) · {counts['blocked']} blocked",
        (f"Sending {len(rows)} WordPress site(s), best first." if rows
         else "No WordPress sites in this file."),
    ])

    if chat_id in (None, 0):
        print(f"[local job] {job['job_id']}: {counts['wordpress']} wordpress, "
              f"{counts['leads']} leads")
        for index, row in enumerate(rows, 1):
            print(site_message(index, len(rows), row))
            print()
        digest = top_summary_message(job, counts, rows, cfg)
        if digest:
            print(digest)
        return 0

    if delivered == 0 and not _notify(client, chat_id, header):
        return 0
    if delivered and rows:
        _notify(client, chat_id,
                f"Resuming {job['job_id']} at #{delivered + 1} of {len(rows)}.")
    try:
        sent = send_site_messages(
            client, chat_id, rows,
            interval=0.0 if cfg.dry_run else SEND_INTERVAL,
            start=delivered,
            on_sent=lambda count: store.set_kv(con, progress_key, str(count)),
        )
    except TelegramError as exc:
        # Scoring already succeeded, so the job stays retryable and cheap: the
        # crawl ledger is warm and delivery resumes from the last sent message.
        _notify(client, chat_id,
                f"Sending stopped at #{delivered} of {len(rows)} ({exc}).\n"
                f"/retry {job['job_id']} will continue from there.")
        raise
    digest = top_summary_message(job, counts, rows, cfg)
    if digest:
        _notify(client, chat_id, digest)
    else:
        _notify(client, chat_id,
                f"Done — {job['job_id']}: {sent} WordPress site(s) sent, "
                f"{counts['leads']} at or above {cfg.lead_score:g}.")
    store.set_kv(con, progress_key, "0")  # finished: a manual /retry resends all
    return sent


def process_job(client: Any, con: Any, cfg: Config, job: dict,
                force: bool = False) -> bool:
    """Run one job. Returns True when it is fully finished."""
    job_id = job["job_id"]
    job_dir = os.path.join(cfg.upload_dir, job_id)
    source = os.path.join(job_dir, "source.csv")
    if not os.path.isfile(source):
        raise RuntimeError(f"source file missing: {source}")
    print(f"{job_id}: processing {job.get('file_name')}")

    domains_path = os.path.join(job_dir, "domains.txt")
    if not os.path.exists(domains_path):
        _run([sys.executable, os.path.join(BASE_DIR, "discover.py"),
              source, "--out-dir", job_dir])
    if not os.path.exists(domains_path):
        raise RuntimeError("discover.py produced no domains.txt")

    with open(domains_path, encoding="utf-8") as fh:
        hosts = [line.strip() for line in fh if line.strip()]
    registrables = {score.registrable_domain(score.host_of(h)) for h in hosts if h}
    if not registrables:
        raise RuntimeError("no usable domains in the uploaded CSV")
    store.add_job_domains(con, job_id, registrables)
    counts = store.job_counts(con, job_id, cfg.lead_score)
    if counts["total"] == 0:
        raise RuntimeError("job has no domains")

    if counts["deferred"] or force:
        cmd = [sys.executable, os.path.join(BASE_DIR, "score.py"),
               "-f", domains_path, "--db", cfg.db_path,
               "--workers", str(cfg.workers),
               "--batch", str(0 if force else cfg.domains_per_tick), "--quiet",
               "--candidates", os.path.join(job_dir, "candidates.json")]
        if force:
            cmd.append("--force")
        _run(cmd)
        counts = store.job_counts(con, job_id, cfg.lead_score)
        store.save_job_counts(con, job_id, counts)
        job = store.get_job(con, job_id) or job
        if counts["deferred"]:
            store.set_job_status(con, job_id, "processing")
            _notify(client, job.get("chat_id"),
                    f"Job {job_id}: {counts['checked']}/{counts['total']} checked, "
                    f"{counts['deferred']} deferred to the next run.")
            print(f"{job_id}: {counts['checked']}/{counts['total']} checked, "
                  f"{counts['deferred']} deferred")
            return False

    render_and_send(client, con, cfg, job)
    return True


def render_and_send(client: Any, con: Any, cfg: Config, job: dict) -> dict[str, int]:
    """Regenerate the job report from stored scores, then deliver it."""
    job_id = job["job_id"]
    job_dir = os.path.join(cfg.upload_dir, job_id)
    report_dir = os.path.join(job_dir, "out")
    _run([sys.executable, os.path.join(BASE_DIR, "report.py"),
          "--db", cfg.db_path,
          "--candidates", os.path.join(job_dir, "candidates.json"),
          "--outdir", report_dir,
          "--job-id", job_id,
          "--lead-score", str(cfg.lead_score)])

    counts = store.job_counts(con, job_id, cfg.lead_score)
    report_path = os.path.join(report_dir, "all_wordpress.csv")
    store.save_job_counts(con, job_id, counts)
    store.set_job_status(con, job_id, "done", finished=True, report_path=report_path)
    job = store.get_job(con, job_id) or job
    send_report(client, con, job, counts, report_path, cfg)
    print(f"{job_id}: done, {counts['wordpress']} wordpress, {counts['leads']} leads")
    return counts


def process_jobs(client: Any, con: Any, cfg: Config) -> int:
    finished = 0
    attempted: set[str] = set()
    for _ in range(max(0, cfg.max_jobs_per_tick)):
        job = store.claim_next_job(con, exclude=attempted)
        if job is None:
            break
        attempted.add(job["job_id"])
        force = store.get_kv(con, f"rescore_{job['job_id']}") == "1"
        if force:
            store.set_kv(con, f"rescore_{job['job_id']}", "0")
            store.set_kv(con, f"sending_{job['job_id']}", "0")
        try:
            if process_job(client, con, cfg, job, force=force):
                finished += 1
        except Exception as exc:
            store.set_job_status(con, job["job_id"], "failed", error=str(exc), finished=True)
            print(f"{job['job_id']}: failed: {exc}", file=sys.stderr)
            job = store.get_job(con, job["job_id"]) or job
            _notify(client, job.get("chat_id"), result_text(job, {
                "total": job.get("total_domains") or 0,
                "checked": job.get("checked") or 0,
                "wordpress": job.get("wordpress") or 0,
                "leads": job.get("leads") or 0,
                "blocked": job.get("blocked") or 0,
                "deferred": job.get("deferred") or 0,
            }, cfg) + f"\n\nFailed: {exc}")
    return finished


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one Telegram ingest + scoring tick.")
    ap.add_argument("--data-dir", help="persistent root (default $DATA_DIR or data)")
    ap.add_argument("--db", help="SQLite path (default $DATA_DIR/leads.db)")
    ap.add_argument("--workers", type=int, help="concurrent fetches per tick")
    ap.add_argument("--domains-per-tick", type=int, help="domains scored per job per tick")
    ap.add_argument("--max-jobs", type=int, help="jobs touched per tick")
    ap.add_argument("--dry-run", action="store_true", help="do not call Telegram")
    ap.add_argument("--ingest-file", help="queue a local CSV instead of polling Telegram")
    ap.add_argument("--chat-id", type=int, default=0, help="chat ID to attribute --ingest-file")
    ap.add_argument("--no-process", action="store_true", help="ingest only, do not score")
    ap.add_argument("--rescore", metavar="JOB_ID",
                    help="re-crawl and re-send one existing job (new scoring)")
    ap.add_argument("--resend", metavar="JOB_ID",
                    help="re-render and re-send an existing report without crawling")
    args = ap.parse_args()

    load_dotenv()
    cfg = build_config(args)
    if not cfg.dry_run and not cfg.allowed_chats:
        print(
            "warning: TELEGRAM_ALLOWED_CHAT_IDS is empty, so commands and "
            "uploads will be ignored. Send the bot any message; it replies "
            "with your chat ID once. Put that in .env, then run again.",
            file=sys.stderr,
        )
    os.makedirs(cfg.upload_dir, exist_ok=True)

    con = store.connect(cfg.db_path)
    client: Any = LoggingClient()
    try:
        if not cfg.dry_run:
            try:
                client = TelegramClient()
            except TelegramError as exc:
                raise SystemExit(f"telegram: {exc}") from None
            ingested = poll_telegram(client, con, cfg)
            print(f"telegram: {ingested} upload(s) queued")

        if args.ingest_file:
            job_id = ingest_file(client, con, cfg, os.path.abspath(args.ingest_file),
                                 chat_id=args.chat_id,
                                 original_name=os.path.basename(args.ingest_file))
            print(f"local: queued {job_id}" if job_id else "local: nothing queued")

        if args.rescore:
            job = store.get_job(con, args.rescore)
            if job is None:
                raise SystemExit(f"no job {args.rescore!r}")
            store.retry_job(con, args.rescore)
            store.set_kv(con, f"sending_{args.rescore}", "0")
            store.set_job_status(con, args.rescore, "processing")
            job = store.get_job(con, args.rescore) or job
            finished = process_job(client, con, cfg, job, force=True)
            print(f"rescore: {'finished' if finished else 'deferred'}")
            return 0

        if args.resend:
            job = store.get_job(con, args.resend)
            if job is None:
                raise SystemExit(f"no job {args.resend!r}")
            store.set_kv(con, f"sending_{args.resend}", "0")
            render_and_send(client, con, cfg, job)
            return 0

        if not args.no_process:
            finished = process_jobs(client, con, cfg)
            print(f"tick: {finished} job(s) finished")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
