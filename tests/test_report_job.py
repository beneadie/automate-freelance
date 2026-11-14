from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

import store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _save_site(con, registrable: str, score: float) -> None:
    store.save(con, {
        "registrable": registrable,
        "host": registrable,
        "url": f"https://{registrable}/",
        "status": "scored",
        "http_status": 200,
        "wordpress": 1,
        "wp_signatures": ["wp-includes"],
        "wp_version": "6.5",
        "rest_status": "ok",
        "woo_store": 0,
        "route_count": 10,
        "namespace_count": 2,
        "post_count": 5,
        "page_count": 5,
        "product_count": 0,
        "score": score,
        "tier": "A",
        "flags": [],
        "score_version": "v1",
        "facts": {"emails": [f"hi@{registrable}"]},
        "reasons": [],
        "penalties": [],
    })
    store.mark_seen(con, registrable, "scored", "A", score, True)


class ReportJobScopeTests(unittest.TestCase):
    def test_report_only_contains_job_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "leads.db")
            con = store.connect(db)
            store.create_job(con, "job-1", 1, 1, "one.csv", "hash-1", 10)
            store.create_job(con, "job-2", 1, 2, "two.csv", "hash-2", 10)
            store.add_job_domains(con, "job-1", {"alpha.example"})
            store.add_job_domains(con, "job-2", {"beta.example"})
            _save_site(con, "alpha.example", 80.0)
            _save_site(con, "beta.example", 70.0)
            store.mark_seen(con, "gamma.example", "not_wordpress", "not_wordpress", None, False)
            con.close()

            candidates = os.path.join(tmp, "candidates.json")
            with open(candidates, "w", encoding="utf-8") as fh:
                json.dump({"alpha.example": {"name": "Alpha", "town": "Galway",
                                             "category": "Garage"}}, fh)

            out = os.path.join(tmp, "out")
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "report.py"),
                 "--db", db, "--candidates", candidates, "--outdir", out,
                 "--job-id", "job-1"],
                cwd=BASE_DIR, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            with open(os.path.join(out, "all_wordpress.csv"), encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["domain"], "alpha.example")
            self.assertEqual(rows[0]["lead_id"], "job-1:alpha.example")
            self.assertEqual(rows[0]["name"], "Alpha")

            with open(os.path.join(out, "stats.json"), encoding="utf-8") as fh:
                stats = json.load(fh)
            self.assertEqual(stats["job_id"], "job-1")
            self.assertEqual(stats["checked"], 1)
            self.assertEqual(stats["wordpress"], 1)

    def test_missing_job_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "leads.db")
            store.connect(db).close()
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "report.py"),
                 "--db", db, "--outdir", os.path.join(tmp, "out"),
                 "--job-id", "nope"],
                cwd=BASE_DIR, capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
