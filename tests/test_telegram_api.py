from __future__ import annotations

import os
import unittest
from unittest import mock

import telegram_api


class TelegramHelpersTests(unittest.TestCase):
    def test_chunks_respect_limit(self) -> None:
        text = "\n".join(f"line {i}" for i in range(2000))
        chunks = telegram_api._chunks(text, 4096)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_multipart_contains_file(self) -> None:
        boundary, body = telegram_api._multipart(
            {"chat_id": "1", "caption": "hello"}, "document", "a.csv", b"x,y\n", "text/csv")
        self.assertIn(boundary.encode(), body)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b'filename="a.csv"', body)
        self.assertIn(b"text/csv", body)
        self.assertIn(b"x,y\n", body)

    def test_safe_filename(self) -> None:
        self.assertEqual(telegram_api._safe_filename('bad"name\\n.csv'), "bad_name_n.csv")

    def test_missing_token(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(telegram_api.TelegramError):
                telegram_api.TelegramClient()


if __name__ == "__main__":
    unittest.main()
