from __future__ import annotations

import os
import tempfile
import unittest

import store


def _save_site(con, registrable: str, score: float = 60.0,
               requested: str | None = None) -> None:
    store.save(con, {
        "registrable": registrable,
        "requested": requested or registrable,
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
    store.mark_seen(con, registrable, "scored", "A", score, True,
                    requested=requested or registrable)


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(os.path.join(self.tmp.name, "leads.db"))

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_create_dedupe_and_claim(self) -> None:
        self.assertTrue(store.create_job(self.con, "job-1", 42, 7, "a.csv", "hash-a", 10))
        self.assertFalse(store.create_job(self.con, "job-2", 42, 8, "a.csv", "hash-a", 10))
        self.assertEqual(store.job_by_hash(self.con, "hash-a")["job_id"], "job-1")

        job = store.claim_next_job(self.con)
        self.assertEqual(job["job_id"], "job-1")
        self.assertEqual(job["status"], "processing")
        # Already claimed jobs can be excluded, so one tick does not loop on it.
        self.assertIsNone(store.claim_next_job(self.con, exclude={"job-1"}))
        # A later tick may resume the unfinished job.
        resumed = store.claim_next_job(self.con)
        self.assertEqual(resumed["job_id"], "job-1")

    def test_counts_and_status(self) -> None:
        store.create_job(self.con, "job-1", 42, 7, "a.csv", "hash-a", 10)
        store.add_job_domains(self.con, "job-1", {"a.com", "b.com", "c.com"})
        _save_site(self.con, "a.com", 70.0)
        store.mark_seen(self.con, "b.com", "not_wordpress", "not_wordpress", None, False)

        counts = store.job_counts(self.con, "job-1", lead_score=40)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["checked"], 2)
        self.assertEqual(counts["wordpress"], 1)
        self.assertEqual(counts["leads"], 1)
        self.assertEqual(counts["deferred"], 1)

        store.save_job_counts(self.con, "job-1", counts)
        job = store.get_job(self.con, "job-1")
        self.assertEqual(job["checked"], 2)
        self.assertEqual(job["deferred"], 1)

        store.set_job_status(self.con, "job-1", "done", finished=True, report_path="x.csv")
        job = store.get_job(self.con, "job-1")
        self.assertEqual(job["status"], "done")
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(job["report_path"], "x.csv")

        self.assertTrue(store.retry_job(self.con, "job-1"))
        job = store.get_job(self.con, "job-1")
        self.assertEqual(job["status"], "received")
        self.assertIsNone(job["finished_at"])
        self.assertIsNone(job["error"])

    def test_redirect_is_counted_against_the_submitted_domain(self) -> None:
        # hanleys.ie redirects to djhanleys.ie. The job asks about hanleys.ie.
        store.create_job(self.con, "job-r", 42, 7, "galway.csv", "hash-r", 10)
        store.add_job_domains(self.con, "job-r", {"hanleys.ie", "plain.ie"})

        _save_site(self.con, "djhanleys.ie", 46.0, requested="hanleys.ie")
        _save_site(self.con, "plain.ie", 61.0)

        counts = store.job_counts(self.con, "job-r", lead_score=40)
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["checked"], 2)
        self.assertEqual(counts["wordpress"], 2)
        self.assertEqual(counts["leads"], 2)
        self.assertEqual(counts["deferred"], 0)

        # Both names resolve, so a later CSV naming either one is skipped.
        known = store.known_domains(self.con)
        self.assertIn("hanleys.ie", known)
        self.assertIn("djhanleys.ie", known)

    def test_port_in_redirect_host_is_not_an_identity(self) -> None:
        store.create_job(self.con, "job-p", 42, 7, "p.csv", "hash-p", 10)
        store.add_job_domains(self.con, "job-p", {"bankofireland.com"})
        _save_site(self.con, "bankofireland.com", 38.0,
                   requested="bankofireland.com")
        self.assertEqual(store.job_counts(self.con, "job-p")["deferred"], 0)

    def test_counts_are_not_inflated_by_a_redirect_alias(self) -> None:
        # One job domain can match two ledger rows: its own name and a
        # redirect alias. The funnel must still count the domain once.
        store.create_job(self.con, "job-d", 1, 1, "d.csv", "hash-d", 10)
        store.add_job_domains(self.con, "job-d", {"a.ie"})
        _save_site(self.con, "a.ie", 50.0)
        store.mark_seen(self.con, "a.ie:443", "scored", "B", 50.0, True, requested="a.ie")

        counts = store.job_counts(self.con, "job-d")
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["checked"], 1)
        self.assertEqual(counts["deferred"], 0)

    def test_migration_adds_requested_to_old_databases(self) -> None:
        columns = {row[1] for row in self.con.execute("PRAGMA table_info(sites)")}
        self.assertIn("requested", columns)
        seen_columns = {row[1] for row in self.con.execute("PRAGMA table_info(seen)")}
        self.assertIn("requested", seen_columns)
        # Connecting again must be idempotent.
        store.connect(os.path.join(self.tmp.name, "leads.db")).close()

    def test_kv(self) -> None:
        self.assertIsNone(store.get_kv(self.con, "telegram_offset"))
        store.set_kv(self.con, "telegram_offset", "100")
        self.assertEqual(store.get_kv(self.con, "telegram_offset"), "100")
        store.set_kv(self.con, "telegram_offset", "101")
        self.assertEqual(store.get_kv(self.con, "telegram_offset"), "101")


if __name__ == "__main__":
    unittest.main()
