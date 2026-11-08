"""Recall check: barcode matching against a Safety Gate-like XML, without network."""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "niche"))
import recall_check as rc  # noqa: E402

LIST = b"""<weeklyReports>
  <weeklyReport><reference>Report-2026-43</reference><url>https://ec.europa.eu/x/report/2026/43/xml</url></weeklyReport>
  <weeklyReport><reference>Report-2026-44</reference><url>https://ec.europa.eu/x/report/2026/44/xml</url></weeklyReport>
</weeklyReports>"""

REPORT = b"""<report><notifications>
  <notification>
    <reference>SR/02708/26</reference>
    <product><name>Plastic doll</name><brand>Gina</brand>
      <barcodes><barcode>5906826473019</barcode></barcodes></product>
  </notification>
  <notification>
    <reference>SR/02712/26</reference>
    <product><name>Plastic ball</name><brand>Unknown</brand><barcode>Unknown</barcode></product>
  </notification>
</notifications></report>"""


class RecallCheck(unittest.TestCase):
    def test_report_urls_newest_first(self):
        self.assertEqual(rc.report_urls(LIST, 1), ["https://ec.europa.eu/x/report/2026/44/xml"])

    def test_alerts_only_with_barcode(self):
        alerts = rc.alerts_from_report(REPORT)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reference"], "SR/02708/26")
        self.assertEqual(alerts[0]["brand"], "Gina")
        self.assertIn("5906826473019", alerts[0]["barcodes"])

    def test_match(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cat.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["store", "product_title", "handle", "gtin"])
            w.writerow(["Shop A", "Lalka", "https://a/lalka", "5906826473019"])
            w.writerow(["Shop B", "Piłka", "https://b/pilka", ""])
            w.writerow(["Shop C", "Klocki", "https://c/k", "5907414303886"])
        rows = rc.catalogue(path)
        self.assertEqual(len(rows), 2)
        hits = rc.match(rows, rc.alerts_from_report(REPORT))
        self.assertEqual([(r["store"], a["reference"]) for r, a in hits], [("Shop A", "SR/02708/26")])


if __name__ == "__main__":
    unittest.main()
