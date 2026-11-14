#!/usr/bin/env python3
"""discover.py

Turn a directory CSV into a scored-pipeline domain list.

    python discover.py data/source.csv
    python discover.py data/source.csv --sample 200 --seed 42
    python discover.py data/source.csv --url-column "Web Address"
    python discover.py data/source.csv --out-dir data/jobs/20260101-120000-ab12cd

The CSV is not scored directly: score.py wants one domain per line. This reads
whatever column holds the website, normalises it, throws away social/parking
links, and dedupes on registrable domain so twelve branches of one garage become
one lead.

Outputs:
    data/domains.txt        one host per line, in seeded-shuffled order
    data/candidates.json    {registrable: {name, town, category, website}}

The shuffle is the important part. A directory is usually ordered by name or
county, so taking "the first 200" gives a biased cluster and a wrong hit rate.
Shuffling once, up front, means every --batch 200 run is an independent random
sample, so the per-batch rate is comparable and converges as you run more.
Pass --no-shuffle only if you deliberately want file order.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
from typing import Any
from urllib.parse import urlparse

import score

# Sites that are a business's social page, not its own site. Nothing to score.
SOCIAL_HOSTS = {
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "youtu.be", "pinterest.com", "tiktok.com",
    "wa.me", "api.whatsapp.com", "t.me", "snapchat.com", "threads.net",
    "business.site", "sites.google.com", "google.com", "goo.gl", "maps.app.goo.gl",
    "yelp.com", "yelp.ie", "tripadvisor.com", "tripadvisor.ie",
    "amazon.com", "amazon.co.uk", "ebay.com", "ebay.ie", "etsy.com",
    "wordpress.com", "blogspot.com", "wixsite.com", "weebly.com",
}

URL_HINTS = (
    "website", "web site", "web", "url", "site", "domain", "homepage",
    "web address", "webaddress", "link",
)
NAME_HINTS = ("business name", "company name", "trading name", "name", "business", "company", "title")
TOWN_HINTS = ("town", "city", "locality", "address", "county", "location", "area")
CATEGORY_HINTS = ("category", "sector", "type", "industry", "classification", "trade")

HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$")


def read_table(path: str) -> tuple[list[str], list[dict[str, str]]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=encoding) as fh:
                sample = fh.read(8192)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(fh, dialect=dialect)
                headers = [h.strip() for h in (reader.fieldnames or [])]
                rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k}
                        for row in reader]
                return headers, rows
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise SystemExit(f"could not read {path}: {last_error}")


def pick_column(headers: list[str], override: str | None, hints: tuple[str, ...]) -> str | None:
    if override:
        for header in headers:
            if header.lower() == override.lower():
                return header
        raise SystemExit(f"column {override!r} not found; headers are: {', '.join(headers)}")
    lowered = [h.lower() for h in headers]
    for hint in hints:
        if hint in lowered:
            return headers[lowered.index(hint)]
    for header, low in zip(headers, lowered):
        if any(hint in low for hint in hints if len(hint) > 3):
            return header
    return None


def normalise_host(value: str) -> str | None:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or not HOST_RE.match(host):
        return None
    if host in SOCIAL_HOSTS:
        return "!social"
    if any(host.endswith("." + bad) for bad in SOCIAL_HOSTS):
        return "!social"
    return host


def main() -> int:
    ap = argparse.ArgumentParser(description="Normalise a business directory CSV into score.py input.")
    ap.add_argument("csv", help="directory CSV with a website column")
    ap.add_argument("--url-column", help="exact name of the website column (auto-detected by default)")
    ap.add_argument("--name-col", help="column to use as the business name")
    ap.add_argument("--town-col", help="column to use as the town/locality")
    ap.add_argument("--category-col", help="column to use as the category")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="keep at most N domains after shuffling (0 = all)")
    ap.add_argument("--seed", type=int, default=42, help="shuffle seed (default 42)")
    ap.add_argument("--no-shuffle", action="store_true", help="keep original file order")
    ap.add_argument("--out-dir", default="data",
                    help="directory for domains.txt and candidates.json (default data)")
    ap.add_argument("--out-domains", help="override full path for domains.txt")
    ap.add_argument("--out-candidates", help="override full path for candidates.json")
    args = ap.parse_args()

    out_domains = args.out_domains or os.path.join(args.out_dir, "domains.txt")
    out_candidates = args.out_candidates or os.path.join(args.out_dir, "candidates.json")

    headers, rows = read_table(args.csv)
    if not headers:
        raise SystemExit(f"{args.csv} has no header row")

    url_col = pick_column(headers, args.url_column, URL_HINTS)
    if url_col is None:
        raise SystemExit(
            "could not find a website column. Headers seen: " + ", ".join(headers)
            + "\nRe-run with --url-column \"<name>\"."
        )
    name_col = pick_column(headers, args.name_col, NAME_HINTS)
    town_col = pick_column(headers, args.town_col, TOWN_HINTS)
    cat_col = pick_column(headers, args.category_col, CATEGORY_HINTS)

    hosts: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    seen_reg: set[str] = set()
    no_website = invalid = social = duplicate = 0

    for row in rows:
        raw = row.get(url_col, "")
        if not raw:
            no_website += 1
            continue
        host = normalise_host(raw)
        if host == "!social":
            social += 1
            continue
        if host is None:
            invalid += 1
            continue
        reg = score.registrable_domain(score.host_of(host))
        if reg in seen_reg:
            duplicate += 1
            continue
        seen_reg.add(reg)
        hosts.append(host)
        candidates[reg] = {
            "name": row.get(name_col, "") if name_col else "",
            "town": row.get(town_col, "") if town_col else "",
            "category": row.get(cat_col, "") if cat_col else "",
            "website": host,
        }

    if not hosts:
        raise SystemExit("no usable websites found in the CSV")

    if not args.no_shuffle:
        random.Random(args.seed).shuffle(hosts)

    kept = hosts
    if args.sample:
        kept = hosts[: args.sample]

    for path in (out_domains, out_candidates):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    with open(out_domains, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    with open(out_candidates, "w", encoding="utf-8") as fh:
        json.dump(candidates, fh, indent=2, ensure_ascii=False)

    print(f"csv rows        {len(rows)}")
    print(f"no website      {no_website}")
    print(f"social/parking  {social}")
    print(f"unparseable     {invalid}")
    print(f"duplicate reg.  {duplicate}")
    print(f"unique domains  {len(hosts)}")
    print(f"written         {len(kept)} -> {out_domains}")
    print(f"                candidates -> {out_candidates}")
    if not args.no_shuffle:
        print(f"order           shuffled, seed={args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
