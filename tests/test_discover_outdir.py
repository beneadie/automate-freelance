from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DiscoverOutDirTests(unittest.TestCase):
    def test_writes_into_out_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "directory.csv")
            with open(source, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Business Name", "Website", "Town", "Category"])
                writer.writerow(["Alpha Garage", "https://www.alpha.example", "Galway", "Garage"])
                writer.writerow(["Beta Cafe", "beta.example/menu", "Sligo", "Cafe"])
                writer.writerow(["Social Only", "https://facebook.com/something", "Galway", "Cafe"])
                writer.writerow(["Duplicate", "alpha.example/contact", "Galway", "Garage"])

            out = os.path.join(tmp, "job")
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "discover.py"),
                 source, "--out-dir", out, "--no-shuffle"],
                cwd=BASE_DIR, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            with open(os.path.join(out, "domains.txt"), encoding="utf-8") as fh:
                domains = fh.read().split()
            self.assertEqual(sorted(domains), ["alpha.example", "beta.example"])

            with open(os.path.join(out, "candidates.json"), encoding="utf-8") as fh:
                candidates = json.load(fh)
            self.assertEqual(candidates["alpha.example"]["name"], "Alpha Garage")
            self.assertEqual(candidates["beta.example"]["town"], "Sligo")


if __name__ == "__main__":
    unittest.main()
