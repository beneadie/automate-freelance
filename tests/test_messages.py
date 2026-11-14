from __future__ import annotations

import csv
import os
import tempfile
import unittest
from unittest import mock

import run_tick
import store
from telegram_api import TelegramError


class RecordingClient:
    """Sends nothing; records what would have been sent."""

    def __init__(self) -> None:
        self.messages: list[tuple[object, str]] = []

    def send_message(self, chat_id: int | str, text: str) -> None:
        self.messages.append((chat_id, text))


class FlakyClient(RecordingClient):
    def __init__(self, failures: int, retry_after: int = 2) -> None:
        super().__init__()
        self.failures = failures
        self.retry_after = retry_after

    def send_message(self, chat_id: int | str, text: str) -> None:
        if self.failures:
            self.failures -= 1
            raise TelegramError("sendMessage: Too Many Requests", self.retry_after)
        super().send_message(chat_id, text)


COLUMNS = [
    "lead_id", "name", "town", "category", "domain", "url", "score", "tier",
    "wp_version", "woocommerce", "products", "posts", "pages", "email", "phone",
    "flags", "top_reasons", "penalties", "scored_at",
]


def write_report(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_row(**overrides: str) -> dict[str, str]:
    row = {
        "lead_id": "job-1:claddaghmotors.ie",
        "name": "Claddagh Motors",
        "town": "Galway",
        "category": "Garage",
        "domain": "claddaghmotors.ie",
        "url": "https://claddaghmotors.ie/",
        "score": "72.0",
        "tier": "A",
        "wp_version": "6.4",
        "woocommerce": "",
        "products": "",
        "posts": "42",
        "pages": "8",
        "email": "info@claddaghmotors.ie",
        "phone": "091 123 456",
        "flags": "",
        "top_reasons": "plugin_surface (+10); content_volume (+8)",
        "penalties": "",
        "scored_at": "2026-11-14T10:00:00+00:00",
    }
    row.update(overrides)
    return row


class SiteMessageTests(unittest.TestCase):
    def test_full_row_reads_well(self) -> None:
        text = run_tick.site_message(12, 118, make_row())
        self.assertEqual(text.splitlines()[0], "#12 of 118 · 72 pts · Tier A")
        self.assertIn("Claddagh Motors", text)
        self.assertIn("Galway · Garage", text)
        self.assertIn("claddaghmotors.ie", text)
        self.assertIn("WP 6.4 · no Woo · 42 posts · 8 pages", text)
        self.assertIn("info@claddaghmotors.ie · 091 123 456", text)
        self.assertIn("plugin_surface (+10)", text)

    def test_blank_fields_leave_no_empty_lines(self) -> None:
        row = make_row(
            name="", town="", category="", wp_version="", posts="0",
            pages="", email="", phone="", top_reasons="", tier="",
        )
        text = run_tick.site_message(1, 1, row)
        self.assertEqual(text.splitlines()[0], "#1 of 1 · 72 pts · Tier -")
        self.assertNotIn("\n\n", text)
        self.assertTrue(all(line.strip() for line in text.splitlines()))
        self.assertIn("no Woo", text)

    def test_woocommerce_and_products(self) -> None:
        text = run_tick.site_message(2, 5, make_row(woocommerce="yes", products="120"))
        self.assertIn("Woo · 42 posts · 8 pages · 120 products", text)

    def test_bad_score_does_not_crash(self) -> None:
        text = run_tick.site_message(1, 1, make_row(score=""))
        self.assertIn("? pts", text)


class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.report = os.path.join(self.tmp.name, "all_wordpress.csv")
        self.cfg = run_tick.Config(
            data_dir=self.tmp.name,
            db_path=os.path.join(self.tmp.name, "leads.db"),
            upload_dir=os.path.join(self.tmp.name, "uploads"),
            allowed_chats={1},
            dry_run=True,  # keeps pacing at zero; separate test covers the sleep
        )
        self.con = store.connect(self.cfg.db_path)
        self.job = {"job_id": "job-1", "file_name": "galway.csv", "chat_id": 1,
                    "status": "done"}
        self.counts = {"total": 3, "checked": 3, "wordpress": 2, "leads": 1,
                       "blocked": 1, "deferred": 0}

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_one_message_per_wordpress_site_best_first(self) -> None:
        write_report(self.report, [
            make_row(domain="weaker.ie", name="Weaker", score="41.0"),
            make_row(domain="stronger.ie", name="Stronger", score="88.0"),
        ])
        client = RecordingClient()
        sent = run_tick.send_report(client, self.con, self.job, self.counts, self.report, self.cfg)

        self.assertEqual(sent, 2)
        self.assertEqual(len(client.messages), 4)  # header + 2 sites + footer
        self.assertTrue(all(chat_id == 1 for chat_id, _ in client.messages))

        header, first, second, footer = [text for _, text in client.messages]
        self.assertIn("Job job-1", header)
        self.assertIn("3/3 domains checked", header)
        self.assertIn("2 WordPress", header)
        self.assertIn("Sending 2 WordPress site(s), best first.", header)
        self.assertTrue(first.startswith("#1 of 2 · 88 pts"))
        self.assertIn("Stronger", first)
        self.assertTrue(second.startswith("#2 of 2 · 41 pts"))
        self.assertIn("Weaker", second)
        self.assertTrue(footer.startswith("TOP 2"))
        self.assertIn("2 WordPress", footer)

    def test_no_wordpress_sends_only_header_and_footer(self) -> None:
        write_report(self.report, [])
        counts = dict(self.counts, wordpress=0, leads=0)
        client = RecordingClient()
        sent = run_tick.send_report(client, self.con, self.job, counts, self.report, self.cfg)

        self.assertEqual(sent, 0)
        self.assertEqual(len(client.messages), 2)
        self.assertIn("No WordPress sites in this file.", client.messages[0][1])

    def test_client_is_never_asked_for_documents(self) -> None:
        write_report(self.report, [make_row()])
        client = RecordingClient()  # has no send_document at all
        run_tick.send_report(client, self.con, self.job, self.counts, self.report, self.cfg)
        self.assertEqual(len(client.messages), 3)

    def test_retries_after_rate_limit(self) -> None:
        sleeps: list[float] = []
        client = FlakyClient(failures=1, retry_after=2)
        sent = run_tick.send_site_messages(
            client, 1, [make_row()], sleep=sleeps.append, interval=1.05)

        self.assertEqual(sent, 1)
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(len(client.messages), 1)

    def test_paces_messages_under_flood_limit(self) -> None:
        sleeps: list[float] = []
        client = RecordingClient()
        rows = [make_row(domain=f"site{i}.ie") for i in range(3)]
        run_tick.send_site_messages(client, 1, rows, sleep=sleeps.append, interval=1.05)

        self.assertEqual(sleeps, [1.05, 1.05])  # no sleep after the last message
        self.assertEqual(len(client.messages), 3)

    def test_resume_does_not_repeat_delivered_messages(self) -> None:
        write_report(self.report, [
            make_row(domain="one.ie", name="One", score="90.0"),
            make_row(domain="two.ie", name="Two", score="50.0"),
            make_row(domain="three.ie", name="Three", score="30.0"),
        ])
        store.set_kv(self.con, "sending_job-1", "2")

        client = RecordingClient()
        sent = run_tick.send_report(client, self.con, self.job, self.counts,
                                    self.report, self.cfg)

        self.assertEqual(sent, 3)
        texts = [text for _, text in client.messages]
        self.assertFalse(any("Job job-1" in t for t in texts))  # no duplicate header
        self.assertTrue(any("Resuming" in t for t in texts))
        site_texts = [t for t in texts if t.startswith("#")]
        self.assertEqual(len(site_texts), 1)
        self.assertTrue(site_texts[0].startswith("#3 of 3"))
        self.assertTrue(texts[-1].startswith("TOP 3"))

    def test_digest_lists_top_twenty_and_says_how_many_remain(self) -> None:
        rows = [make_row(domain=f"site{i}.ie", name=f"Site {i}", score=str(90 - i))
                for i in range(25)]
        write_report(self.report, rows)
        client = RecordingClient()
        run_tick.send_report(client, self.con, self.job, self.counts,
                             self.report, self.cfg)

        texts = [text for _, text in client.messages]
        self.assertEqual(len(texts), 1 + 25 + 1)
        digest = texts[-1]
        self.assertTrue(digest.startswith("TOP 20"))
        self.assertIn("1. 90 · A · Site 0 — site0.ie", digest)
        self.assertIn("5 more in the site messages above.", digest)
        self.assertNotIn("site20.ie", digest)
        self.assertIn("info@claddaghmotors.ie", digest)

    def test_digest_can_be_disabled(self) -> None:
        write_report(self.report, [make_row()])
        with mock.patch.dict(os.environ, {"TOP_SUMMARY": "0"}):
            client = RecordingClient()
            run_tick.send_report(client, self.con, self.job, self.counts,
                                 self.report, self.cfg)
        self.assertTrue(client.messages[-1][1].startswith("Done —"))

    def test_short_job_digest_has_no_remainder_line(self) -> None:
        write_report(self.report, [make_row(domain="a.ie", name="A", score="55"),
                                   make_row(domain="b.ie", name="B", score="44")])
        client = RecordingClient()
        run_tick.send_report(client, self.con, self.job, self.counts,
                             self.report, self.cfg)
        digest = client.messages[-1][1]
        self.assertTrue(digest.startswith("TOP 2"))
        self.assertNotIn("more in the site messages", digest)

    def test_transient_socket_error_is_retried(self) -> None:
        class Flaky(RecordingClient):
            def __init__(self) -> None:
                super().__init__()
                self.fail = 1

            def send_message(self, chat_id: int | str, text: str) -> None:
                if self.fail:
                    self.fail -= 1
                    raise TelegramError("sendMessage: [Errno 104] Connection reset by peer")
                super().send_message(chat_id, text)

        sleeps: list[float] = []
        client = Flaky()
        sent = run_tick.send_site_messages(
            client, 1, [make_row()], sleep=sleeps.append, interval=0)

        self.assertEqual(sent, 1)
        self.assertEqual(len(client.messages), 1)
        self.assertEqual(sleeps, [2.0])

    def test_gives_up_when_rate_limit_never_clears(self) -> None:
        client = FlakyClient(failures=99, retry_after=1)
        with self.assertRaises(TelegramError):
            run_tick.send_site_messages(
                client, 1, [make_row()], sleep=lambda _seconds: None, interval=0)


if __name__ == "__main__":
    unittest.main()
