#!/usr/bin/env python3
"""validate.py

Corpus harness for score.py. Runs a labeled set of URLs through the real
collect()/score() pipeline and checks each against its expected outcome, so
changes to detection or weights are provable instead of guessed.

    python validate.py
    python validate.py --only woocommerce
    python validate.py --record fixtures/     # snapshot full reports for review

Exit code is non-zero if any case fails, so it can run in CI.

Cases live in corpus.json. Add a URL for every failure mode you care about:
WP classic, WP block, Woo simple, Woo with hard extensions, blocked, challenge,
non-WP, unreachable. `expect` supports:
    status          exact status string
    status_in       status is one of a list
    wordpress       true / false / null
    woo_store       true / false
    tier_in         tier is one of a list
    has_flag        flag is present
    signature       WP signature token is present
    min_products    product count >= n
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import score

DEFAULT_CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.json")


def load_corpus(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def slugify(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")


def evaluate(rep: score.SiteReport, expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    rest = rep.facts.get("rest", {})

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    if "status" in expect:
        check(rep.status == expect["status"], f"status={rep.status}, expected {expect['status']}")
    if "status_in" in expect:
        check(rep.status in expect["status_in"], f"status={rep.status}, expected one of {expect['status_in']}")
    if "wordpress" in expect:
        check(rep.wordpress == expect["wordpress"], f"wordpress={rep.wordpress}, expected {expect['wordpress']}")
    if "woo_store" in expect:
        check(bool(rest.get("woo_store")) == expect["woo_store"],
              f"woo_store={bool(rest.get('woo_store'))}, expected {expect['woo_store']}")
    if "tier_in" in expect:
        check(rep.tier in expect["tier_in"], f"tier={rep.tier}, expected one of {expect['tier_in']}")
    if "has_flag" in expect:
        check(expect["has_flag"] in rep.flags, f"flag '{expect['has_flag']}' missing (flags={rep.flags})")
    if "signature" in expect:
        check(expect["signature"] in rep.wp_signatures,
              f"signature '{expect['signature']}' missing (signatures={rep.wp_signatures})")
    if "min_products" in expect:
        check((rest.get("products") or 0) >= expect["min_products"],
              f"products={rest.get('products')}, expected >= {expect['min_products']}")

    legacy = rep.facts.get("legacy_markers", [])
    if "legacy_marker" in expect:
        check(expect["legacy_marker"] in legacy,
              f"legacy marker '{expect['legacy_marker']}' missing (markers={legacy})")
    if "min_legacy_markers" in expect:
        check(len(legacy) >= expect["min_legacy_markers"],
              f"legacy markers={len(legacy)}, expected >= {expect['min_legacy_markers']} (markers={legacy})")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate score.py against a labeled corpus.")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS, help="path to corpus.json")
    ap.add_argument("--only", help="only run cases whose URL contains this substring")
    ap.add_argument("--record", help="directory to save full reports as JSON")
    ap.add_argument("--ignore-robots", action="store_true")
    args = ap.parse_args()

    cases = load_corpus(args.corpus)
    if args.only:
        cases = [c for c in cases if args.only in c["url"]]

    passed = failed = 0
    for case in cases:
        url = case["url"]
        rep = score.collect(url, args.ignore_robots, business_live=case.get("assume_business_live"))
        score.score(rep)
        failures = evaluate(rep, case.get("expect", {}))

        if failures:
            failed += 1
            print(f"[FAIL] {url}  ({rep.status}, tier={rep.tier}, score={rep.score})")
            for f in failures:
                print(f"         - {f}")
        else:
            passed += 1
            print(f"[PASS] {url}  ({rep.status}, tier={rep.tier}, score={rep.score})")

        if args.record:
            os.makedirs(args.record, exist_ok=True)
            path = os.path.join(args.record, slugify(url) + ".json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rep.to_dict(), fh, indent=2, default=str)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
