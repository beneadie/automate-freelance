from __future__ import annotations

import csv
import os
import tempfile
import unittest
from unittest import mock

import run_tick
import store


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.cfg = run_tick.Config(
            data_dir=self.data_dir,
            db_path=os.path.join(self.data_dir, "leads.db"),
            upload_dir=os.path.join(self.data_dir, "uploads"),
            allowed_chats={1},
            dry_run=True,
        )
        os.makedirs(self.cfg.upload_dir, exist_ok=True)
        self.con = store.connect(self.cfg.db_path)
        self.client = run_tick.LoggingClient()
        self.csv_path = os.path.join(self.tmp.name, "source.csv")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Name", "Website"])
            writer.writerow(["Alpha", "alpha.example"])

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_ingest_creates_job_and_dedupes(self) -> None:
        job_id = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                      chat_id=1, original_name="source.csv")
        self.assertIsNotNone(job_id)
        job = store.get_job(self.con, job_id)
        self.assertEqual(job["status"], "received")
        self.assertEqual(job["file_name"], "source.csv")
        self.assertTrue(os.path.isfile(
            os.path.join(self.cfg.upload_dir, job_id, "source.csv")))

        again = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                     chat_id=1, original_name="source.csv")
        self.assertEqual(again, job_id)
        self.assertEqual(len(store.list_jobs(self.con)), 1)

    def test_rejects_non_csv_and_oversize(self) -> None:
        text = os.path.join(self.tmp.name, "source.txt")
        with open(text, "w", encoding="utf-8") as fh:
            fh.write("nope")
        self.assertIsNone(run_tick.ingest_file(
            self.client, self.con, self.cfg, text, chat_id=1, original_name="source.txt"))
        self.assertEqual(len(store.list_jobs(self.con)), 0)

    def test_accepts_csv_misnamed_as_xlsx(self) -> None:
        # What Android/Telegram actually does: real CSV bytes, .csv.xlsx name.
        mangled = "Galway_x5f_Chamber.csv.xlsx"
        job_id = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                      chat_id=1, original_name=mangled)
        self.assertIsNotNone(job_id)
        self.assertEqual(store.get_job(self.con, job_id)["file_name"],
                         "Galway_x5f_Chamber.csv")

    def test_rejects_real_excel_workbook(self) -> None:
        book = os.path.join(self.tmp.name, "sheet.xlsx")
        with open(book, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 64)
        self.assertIsNone(run_tick.ingest_file(
            self.client, self.con, self.cfg, book, chat_id=1, original_name="sheet.xlsx"))
        self.assertEqual(len(store.list_jobs(self.con)), 0)

    def test_sniff_kind_classifies_content(self) -> None:
        self.assertEqual(run_tick.sniff_kind(self.csv_path), "csv")
        semicolon = os.path.join(self.tmp.name, "semi.csv")
        with open(semicolon, "w", encoding="utf-8") as fh:
            fh.write("Name;Website\nAlpha;alpha.example\n")
        self.assertEqual(run_tick.sniff_kind(semicolon), "csv")
        text = os.path.join(self.tmp.name, "plain.txt")
        with open(text, "w", encoding="utf-8") as fh:
            fh.write("just some prose without delimiters\n")
        self.assertEqual(run_tick.sniff_kind(text), "unknown")

    def test_status_reports_live_progress_for_running_jobs(self) -> None:
        job_id = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                      chat_id=1, original_name="source.csv")
        store.add_job_domains(self.con, job_id, {"alpha.example", "beta.example"})
        store.mark_seen(self.con, "alpha.example", "scored", "A", 70.0, True)
        # The stored counter is still zero, exactly like a live crawl.
        self.assertEqual(store.get_job(self.con, job_id)["checked"], 0)

        status = run_tick.command_reply(self.con, self.cfg, "/status")
        self.assertIn("1/2 checked", status)
        self.assertIn("1 WP", status)

    def test_rescore_command_queues_a_forced_recrawl(self) -> None:
        job_id = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                      chat_id=1, original_name="source.csv")
        store.set_job_status(self.con, job_id, "done", finished=True)
        reply = run_tick.command_reply(self.con, self.cfg, f"/rescore {job_id}")

        self.assertIn("queued for re-scoring", reply)
        self.assertEqual(store.get_job(self.con, job_id)["status"], "received")
        self.assertEqual(store.get_kv(self.con, f"rescore_{job_id}"), "1")
        self.assertEqual(store.get_kv(self.con, f"sending_{job_id}"), "0")

    def test_rescore_command_needs_a_known_job(self) -> None:
        self.assertIn("Usage", run_tick.command_reply(self.con, self.cfg, "/rescore"))
        self.assertIn("No job", run_tick.command_reply(self.con, self.cfg, "/rescore nope"))

    def test_command_status_and_retry(self) -> None:
        job_id = run_tick.ingest_file(self.client, self.con, self.cfg, self.csv_path,
                                      chat_id=1, original_name="source.csv")
        status = run_tick.command_reply(self.con, self.cfg, "/status")
        self.assertIn(job_id, status)
        self.assertIn("received", status)

        store.set_job_status(self.con, job_id, "failed", error="boom", finished=True)
        reply = run_tick.command_reply(self.con, self.cfg, f"/retry {job_id}")
        self.assertIn("queued again", reply)
        self.assertEqual(store.get_job(self.con, job_id)["status"], "received")


class DotenvTests(unittest.TestCase):
    def _load(self, body: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            run_tick.load_dotenv(path)

    def test_parses_plain_exported_quoted_and_skips_junk(self) -> None:
        keys = ["RUNTICK_TEST_PLAIN", "RUNTICK_TEST_EXPORTED",
                "RUNTICK_TEST_QUOTED", "RUNTICK_TEST_SINGLE"]
        with mock.patch.dict(os.environ):
            for key in keys:
                os.environ.pop(key, None)
            self._load(
                "# a comment\n"
                "\n"
                "RUNTICK_TEST_PLAIN=hello\n"
                "export RUNTICK_TEST_EXPORTED=yes\n"
                'RUNTICK_TEST_QUOTED="two words"\n'
                "RUNTICK_TEST_SINGLE='single'\n"
                "malformed line without equals\n"
            )
            self.assertEqual(os.environ["RUNTICK_TEST_PLAIN"], "hello")
            self.assertEqual(os.environ["RUNTICK_TEST_EXPORTED"], "yes")
            self.assertEqual(os.environ["RUNTICK_TEST_QUOTED"], "two words")
            self.assertEqual(os.environ["RUNTICK_TEST_SINGLE"], "single")

    def test_real_environment_is_not_overridden(self) -> None:
        with mock.patch.dict(os.environ, {"RUNTICK_TEST_KEEP": "from-env"}):
            self._load("RUNTICK_TEST_KEEP=from-file\n")
            self.assertEqual(os.environ["RUNTICK_TEST_KEEP"], "from-env")

    def test_missing_file_is_ignored(self) -> None:
        run_tick.load_dotenv(os.path.join(tempfile.gettempdir(), "nope-not-here.env"))


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    def send_message(self, chat_id: int | str, text: str) -> None:
        self.sent.append((chat_id, text))


class _PollClient(_Recorder):
    def __init__(self, updates: list[dict]) -> None:
        super().__init__()
        self.updates = updates

    def get_updates(self, offset: int | None = None, limit: int = 100,
                    timeout: int = 0) -> list[dict]:
        return self.updates


class UnknownChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = run_tick.Config(
            data_dir=self.tmp.name,
            db_path=os.path.join(self.tmp.name, "leads.db"),
            upload_dir=os.path.join(self.tmp.name, "uploads"),
            allowed_chats={1},
            dry_run=True,
        )
        os.makedirs(self.cfg.upload_dir, exist_ok=True)
        self.con = store.connect(self.cfg.db_path)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_announces_chat_id_once(self) -> None:
        client = _Recorder()
        run_tick.announce_chat(client, self.con, 777)
        self.assertEqual(len(client.sent), 1)
        self.assertIn("777", client.sent[0][1])
        self.assertEqual(store.get_kv(self.con, "notified_chat_777"), "1")

        run_tick.announce_chat(client, self.con, 777)
        self.assertEqual(len(client.sent), 1)

    def test_unknown_chat_cannot_queue_a_document(self) -> None:
        client = _PollClient([{
            "update_id": 41,
            "message": {
                "message_id": 9,
                "chat": {"id": 999},
                "document": {"file_id": "abc", "file_name": "x.csv", "file_size": 10},
            },
        }])
        queued = run_tick.poll_telegram(client, self.con, self.cfg)

        self.assertEqual(queued, 0)
        self.assertEqual(store.list_jobs(self.con), [])
        self.assertEqual(len(client.sent), 1)
        self.assertIn("999", client.sent[0][1])
        # Offset still advances, so a stranger cannot block the queue.
        self.assertEqual(store.get_kv(self.con, "telegram_offset"), "42")


if __name__ == "__main__":
    unittest.main()
