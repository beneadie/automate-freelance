#!/usr/bin/env python3
"""report.py

Read-only reporting for score.py's SQLite output.

    python report.py
    python report.py --population 200000
    python report.py --bands 65,50,35 --lead-score 50
    python report.py --outdir data/out
    python report.py --job-id 20260101-120000-ab12cd

Writes to --outdir (default data/out):
    all_wordpress.csv    every WordPress site found, highest score first
    band_80_100.csv      score >= 80
    band_60_80.csv       60 <= score < 80
    band_40_60.csv       40 <= score < 60
    band_20_40.csv       20 <= score < 40
    band_0_20.csv        score < 20
    not_wordpress.csv    checked 200 pages with no WordPress signature
    unreachable.csv      blocked / unreachable / http_error domains
    stats.txt            funnel, band distribution, confidence intervals
    stats.json           the same numbers, machine readable

Bands are half-open and never overlap. Non-WordPress outcomes are read from the
crawl ledger only, so they carry no page data. With --job-id, only domains from
that upload are counted and the candidate names come from that job's own
candidates.json; every output row gets a stable `<job_id>:<registrable>`
`lead_id` for later selection.

--population is the number of Irish businesses that already have a website;
nothing is extrapolated unless you pass it, because the right denominator is
your call, not the script's.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from typing import Any

import store

DEFAULT_BANDS = "80,60,40,20"
FAILED_STATUSES = ("blocked", "unreachable", "http_error")


def parse_bands(spec: str) -> list[tuple[str, float, float]]:
    """Turn "80,60,40,20" into [("band_80_100", 80, 101), ...].

    Each band is [lo, hi) so scores never land in two files, and the top band is
    open-ended to include 100.
    """
    cuts = sorted({int(x.strip()) for x in spec.split(",") if x.strip()}, reverse=True)
    bands: list[tuple[str, float, float]] = []
    previous = 101.0
    for cut in cuts:
        upper = 100 if previous == 101.0 else previous
        bands.append((f"band_{cut}_{int(upper)}", float(cut), previous))
        previous = float(cut)
    bands.append((f"band_0_{int(previous) if previous != 101 else 100}", 0.0, previous))
    return bands

SITE_COLUMNS = [
    "lead_id", "name", "town", "category", "domain", "url", "score", "tier",
    "wp_version", "woocommerce", "products", "posts", "pages",
    "email", "phone", "flags", "top_reasons", "penalties", "scored_at",
]


def band_of(score: float | None, bands: list[tuple[str, float, float]]) -> str:
    value = score if score is not None else 0.0
    for name, low, high in bands:
        if low <= value < high:
            return name
    return bands[-1][0]


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def clean_phone(value: str) -> str:
    if value.lower().startswith("tel:"):
        return value[4:]
    return value


def load_sites(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT registrable, host, url, status, wordpress, wp_version, woo_store, "
        "post_count, page_count, product_count, score, tier, flags, facts, reasons, "
        "penalties, scored_at FROM sites"
    ).fetchall()
    out = []
    for row in rows:
        facts = load_json(row["facts"], {})
        reasons = load_json(row["reasons"], [])
        penalties = load_json(row["penalties"], [])
        emails = facts.get("emails") or []
        phones = [clean_phone(p) for p in (facts.get("phones") or [])]
        positives = sorted((r for r in reasons if r.get("points", 0) > 0),
                           key=lambda r: r.get("points", 0), reverse=True)
        top = "; ".join(f"{r['label']} (+{r['points']:g})" for r in positives[:3])
        pen = "; ".join(f"{p['label']} ({p['points']:g})" for p in penalties)
        out.append({
            "registrable": row["registrable"],
            "domain": row["host"] or row["registrable"],
            "url": row["url"] or "",
            "score": row["score"],
            "tier": row["tier"] or "",
            "wp_version": row["wp_version"] or "",
            "woocommerce": "yes" if row["woo_store"] else "",
            "products": row["product_count"] if row["product_count"] is not None else "",
            "posts": row["post_count"] if row["post_count"] is not None else "",
            "pages": row["page_count"] if row["page_count"] is not None else "",
            "email": emails[0] if emails else "",
            "phone": phones[0] if phones else "",
            "flags": ", ".join(load_json(row["flags"], []) or []),
            "top_reasons": top,
            "penalties": pen,
            "scored_at": row["scored_at"] or "",
            "axis_raw": facts.get("axis_raw") or {},
        })
    out.sort(key=lambda r: (-(r["score"] or 0), r["domain"]))
    return out


def write_csv(path: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="Report on scored WordPress leads.")
    ap.add_argument("--db", default="data/leads.db")
    ap.add_argument("--candidates", default="data/candidates.json")
    ap.add_argument("--outdir", default="data/out")
    ap.add_argument("--job-id", help="only report domains belonging to this upload job")
    ap.add_argument("--population", type=int, default=0,
                    help="Irish businesses with a website, for market-size estimates")
    ap.add_argument("--bands", default=DEFAULT_BANDS,
                    help=f"comma-separated score cuts, high to low (default {DEFAULT_BANDS})")
    ap.add_argument("--lead-score", type=float, default=40.0,
                    help="minimum score counted as a lead (default 40)")
    args = ap.parse_args()
    bands = parse_bands(args.bands)

    if not os.path.exists(args.db):
        raise SystemExit(f"{args.db} not found; run score.py first")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    job_regs: set[str] | None = None
    job: dict[str, Any] | None = None
    if args.job_id:
        row = con.execute("SELECT * FROM jobs WHERE job_id = ?", (args.job_id,)).fetchone()
        if row is None:
            raise SystemExit(f"job {args.job_id!r} not found in {args.db}")
        job = dict(row)
        job_regs = store.job_domain_set(con, args.job_id)

    candidates: dict[str, Any] = {}
    if os.path.exists(args.candidates):
        with open(args.candidates, encoding="utf-8") as fh:
            candidates = json.load(fh)

    sites = load_sites(con)
    if job_regs is not None:
        sites = [s for s in sites if s["registrable"] in job_regs]
    for site in sites:
        meta = candidates.get(site["registrable"], {})
        site["name"] = meta.get("name", "")
        site["town"] = meta.get("town", "")
        site["category"] = meta.get("category", "")
        site["lead_id"] = (f"{args.job_id}:{site['registrable']}"
                           if args.job_id else site["registrable"])

    ledger = [dict(r) for r in con.execute(
        "SELECT registrable, status, tier, score, wordpress, checked_at FROM seen"
    )]
    if job_regs is not None:
        ledger = [r for r in ledger if r["registrable"] in job_regs]
    con.close()

    os.makedirs(args.outdir, exist_ok=True)

    write_csv(os.path.join(args.outdir, "all_wordpress.csv"), SITE_COLUMNS, sites)

    band_rows: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in bands}
    for site in sites:
        band_rows[band_of(site["score"], bands)].append(site)
    for band, rows in band_rows.items():
        write_csv(os.path.join(args.outdir, band + ".csv"), SITE_COLUMNS, rows)

    not_wp = [r for r in ledger if r["wordpress"] == 0]
    failed = [r for r in ledger if r["status"] in FAILED_STATUSES]
    write_csv(os.path.join(args.outdir, "not_wordpress.csv"),
              ["registrable", "status", "checked_at"], not_wp)
    write_csv(os.path.join(args.outdir, "unreachable.csv"),
              ["registrable", "status", "tier", "checked_at"], failed)

    # --- funnel -----------------------------------------------------------
    checked = len(ledger)
    deferred = max(0, len(job_regs) - checked) if job_regs is not None else 0
    wp = sum(1 for r in ledger if r["wordpress"] == 1)
    plain_200 = sum(1 for r in ledger if r["wordpress"] == 0)
    blocked = sum(1 for r in ledger if r["status"] == "blocked")
    unreachable = sum(1 for r in ledger if r["status"] == "unreachable")
    http_error = sum(1 for r in ledger if r["status"] == "http_error")
    scored = len(sites)
    band_counts = {b: len(rows) for b, rows in band_rows.items()}
    at_least_lead = sum(1 for s in sites if (s["score"] or 0) >= args.lead_score)
    lead_label = f"{args.lead_score:g}+"

    wp_rate = wp / plain_200 if plain_200 else 0.0
    wp_lo, wp_hi = wilson(wp, plain_200)

    lead_rate_all = at_least_lead / checked if checked else 0.0
    lead_lo_all, lead_hi_all = wilson(at_least_lead, checked)
    lead_rate_wp = at_least_lead / scored if scored else 0.0
    lead_lo_wp, lead_hi_wp = wilson(at_least_lead, scored)

    scores = [s["score"] for s in sites if s["score"] is not None]
    axis_totals: dict[str, list[float]] = {}
    for site in sites:
        for axis, value in (site.get("axis_raw") or {}).items():
            axis_totals.setdefault(axis, []).append(float(value))

    lines: list[str] = []
    add = lines.append
    add("LEAD REPORT")
    add("=" * 60)
    if args.job_id:
        add(f"Job {args.job_id}")
        if job and job.get("file_name"):
            add(f"Source {job['file_name']}")
        add("")
    add("Funnel")
    add(f"  domains checked        {checked}")
    add(f"  200 + WordPress        {wp}")
    add(f"  200 + not WordPress    {plain_200}")
    add(f"  blocked                {blocked}")
    add(f"  unreachable            {unreachable}")
    add(f"  http error             {http_error}")
    add(f"  wordpress row stored   {scored}")
    if args.job_id:
        add(f"  deferred               {deferred}")
    add("")
    add(f"WordPress share of reachable 200s   {pct(wp_rate)}  "
        f"(95% CI {pct(wp_lo)}-{pct(wp_hi)}, n={plain_200})")
    add("")
    add("Score bands (WordPress only)")
    for band, _, _ in bands:
        count = band_counts[band]
        share = count / scored if scored else 0.0
        add(f"  {band:<14} {count:>5}   {pct(share):>6}")
    if scores:
        scores.sort()
        median = scores[len(scores) // 2] if len(scores) % 2 else \
            (scores[len(scores) // 2 - 1] + scores[len(scores) // 2]) / 2
        add(f"  {'median':<14} {median:>5.1f}")
        add(f"  {'max':<14} {max(scores):>5.1f}")
    add("")
    add("Lead rates")
    add(f"  {lead_label} among all checked   {pct(lead_rate_all)}  "
        f"(95% CI {pct(lead_lo_all)}-{pct(lead_hi_all)})")
    add(f"  {lead_label} among WordPress     {pct(lead_rate_wp)}  "
        f"(95% CI {pct(lead_lo_wp)}-{pct(lead_hi_wp)})")
    if axis_totals:
        add("")
        add("Average axis score (WordPress only)")
        for axis in sorted(axis_totals):
            values = axis_totals[axis]
            add(f"  {axis:<16} {sum(values) / len(values):>5.1f}")

    if args.population:
        # Conservative: blocked and dead domains are not leads. The upper bound
        # assumes every blocked site turned out to be a lead.
        est_all = lead_rate_all * args.population
        upper_rate = (at_least_lead + blocked) / checked if checked else 0.0
        upper_lo, upper_hi = wilson(at_least_lead + blocked, checked)
        add("")
        add(f"Market size (population = {args.population:,} businesses with a site)")
        add(f"  measured                     {est_all:,.0f}  "
            f"(95% CI {lead_lo_all * args.population:,.0f}-{lead_hi_all * args.population:,.0f})")
        add(f"  upper bound (blocked = lead) {upper_rate * args.population:,.0f}  "
            f"(95% CI {upper_lo * args.population:,.0f}-{upper_hi * args.population:,.0f})")
        add("  the range is wide because n is small and blocked sites are unknown;")
        add("  it tightens as you run more batches.")
    else:
        add("")
        add("Pass --population N to extrapolate to market size.")

    text = "\n".join(lines) + "\n"
    with open(os.path.join(args.outdir, "stats.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)

    payload = {
        "job_id": args.job_id,
        "checked": checked,
        "deferred": deferred,
        "wordpress": wp,
        "not_wordpress": plain_200,
        "blocked": blocked,
        "unreachable": unreachable,
        "http_error": http_error,
        "wordpress_share": round(wp_rate, 4),
        "wordpress_share_ci": [round(wp_lo, 4), round(wp_hi, 4)],
        "bands": band_counts,
        "lead_score": args.lead_score,
        "at_least_lead": at_least_lead,
        "lead_rate_all": round(lead_rate_all, 4),
        "lead_rate_all_ci": [round(lead_lo_all, 4), round(lead_hi_all, 4)],
        "lead_rate_wordpress": round(lead_rate_wp, 4),
        "lead_rate_wordpress_ci": [round(lead_lo_wp, 4), round(lead_hi_wp, 4)],
        "population": args.population or None,
        "market_size": round(lead_rate_all * args.population) if args.population else None,
        "market_size_ci": [round(lead_lo_all * args.population), round(lead_hi_all * args.population)]
        if args.population else None,
        "market_size_upper": round((at_least_lead + blocked) / checked * args.population)
        if args.population and checked else None,
    }
    with open(os.path.join(args.outdir, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(text)
    print(f"written -> {args.outdir}/  (all_wordpress.csv + {len(bands)} band files + stats)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
