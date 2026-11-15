"""Barcode cache for Shopify, eBay lookups and the "one product - different prices" comparison."""

import csv
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from storewatch import compare as cmp
from storewatch import ebay, gtinfill
from storewatch.cli import main
from storewatch.http import HttpClient, HttpResponse
from storewatch.models import Variant
from storewatch.storage import Storage


def V(vid, price, gtin="", sku="", handle="h", title="Toy", available=True):
    return Variant(1, vid, title, "", handle, sku, "", "", price, None, available, gtin=gtin)


PAGE = """<script type="application/ld+json">{"@type":"Product","name":"Toy","offers":[
{"@type":"Offer","price":"20","sku":"A1","gtin13":"5901234123457"},
{"@type":"Offer","price":"25","sku":"B2","gtin13":"4006381333931"}]}</script>"""


class Match(unittest.TestCase):
    def rows(self):
        from storewatch.extract import products_from_html
        return products_from_html(PAGE, "https://s/products/h")

    def test_by_sku(self):
        got = gtinfill._match([V(1, 20, sku="a1"), V(2, 25, sku="B2")], self.rows())
        self.assertEqual(got, {1: "5901234123457", 2: "4006381333931"})

    def test_by_price_when_no_sku(self):
        got = gtinfill._match([V(1, 20), V(2, 25)], self.rows())
        self.assertEqual(got, {1: "5901234123457", 2: "4006381333931"})

    def test_single_needs_single_barcode(self):
        self.assertEqual(gtinfill._match([V(1, 99)], self.rows()), {})


class Compare(unittest.TestCase):
    def test_currency(self):
        self.assertEqual(cmp.store_currency("Shop (UK)", "https://shop.com"), "GBP")
        self.assertEqual(cmp.store_currency("Shop", "https://shop.co.uk"), "GBP")
        self.assertEqual(cmp.store_currency("Shop (DE)", "https://shop.de"), "EUR")

    def test_saving_and_guards(self):
        items = {
            "1": {"title": "A", "offers": {"X": (10.0, "EUR"), "Y": (8.0, "EUR")}},
            "2": {"title": "B", "offers": {"X": (10.0, "EUR"), "Y": (40.0, "EUR")}},   # >3x: ignored
            "3": {"title": "C", "offers": {"X": (10.0, "EUR")}},                       # one place only
            "4": {"title": "D", "offers": {"X": (12.0, "EUR"), "UK": (8.0, "GBP")}},   # 8 GBP = 10 EUR
            "5": {"title": "E", "offers": {"X": (12.0, "EUR"), "US": (8.0, "USD")}},   # no rate: dropped
        }
        rows = cmp.compare(items, {"EUR": 1.0, "GBP": 0.8})
        self.assertEqual([r["gtin"] for r in rows], ["1", "4"])
        self.assertEqual(rows[0]["saving_pct"], 20.0)
        self.assertEqual(rows[1]["min_seller"], "UK")
        md = cmp.to_markdown(rows, today="2026-11-15")
        self.assertIn("с разной ценой: **2**", md)
        self.assertIn("| A | Y 8.00 € | X 10.00 € | 20% (2.00 €) |", md)

    def test_empty(self):
        self.assertIn("Нет товаров", cmp.to_markdown([], today="2026-11-15"))


class Ebay(unittest.TestCase):
    def test_parse_cheapest(self):
        p = {"itemSummaries": [{"price": {"value": "19.90", "currency": "EUR"}},
                               {"price": {"value": "17.50", "currency": "EUR"}}, {"price": {}}]}
        self.assertEqual(ebay.parse_search(p), (17.5, "EUR"))
        self.assertEqual(ebay.parse_search({}), (None, ""))

    def test_refresh_respects_limit_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            db = Storage(os.path.join(d, "x.db"))
            calls = []
            fake = lambda g, m: (calls.append((g, m)) or (9.99 if g == "1" else None, "EUR"))
            self.assertEqual(ebay.refresh(db, ["1", "2"], max_calls=3, markets=["EBAY_DE", "EBAY_GB"], search=fake), 3)
            self.assertEqual(ebay.refresh(db, ["1", "2"], max_calls=9, markets=["EBAY_DE", "EBAY_GB"], search=fake), 1)
            self.assertEqual(len(calls), 4)
            self.assertEqual(ebay.offers(db), {"1": [("EBAY_DE", 9.99, "EUR"), ("EBAY_GB", 9.99, "EUR")]})
            db.close()


class EndToEnd(unittest.TestCase):
    def test_fill_export_compare(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.db")
            db = Storage(path)
            db.add_store("https://a.example", "A (DE)", platform="shopify", market="m")
            db.add_store("https://b.example", "B (NL)", platform="structured", market="m")
            db.save_snapshot("https://a.example", [V(1, 20, sku="A1", handle="h")])
            db.save_snapshot("https://b.example", [V(7, 16, gtin="5901234123457")])

            def http_get(url, headers, timeout):
                if url.endswith("/robots.txt"):
                    return HttpResponse(404, "", {})
                return HttpResponse(200, PAGE, {})
            client = HttpClient(http_get, delay=0, sleep=lambda s: None)
            self.assertEqual(gtinfill.fill(db, client, "m", 10), [("A (DE)", 1, 1)])
            self.assertEqual(gtinfill.fill(db, client, "m", 10), [("A (DE)", 0, 0)])     # cached: no re-read
            db.close()

            os.environ["STOREWATCH_DB"] = path
            try:
                out = os.path.join(d, "e.csv")
                with redirect_stdout(io.StringIO()):
                    main(["export", "--market", "m", "--gtin", "--csv", out])
                with open(out, encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual({r["store"]: r["gtin"] for r in rows},
                                 {"A (DE)": "5901234123457", "B (NL)": "5901234123457"})
                db = Storage(path)
                rows = cmp.compare(cmp.collect(db, "m"), {"EUR": 1.0})
                db.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["min_seller"], rows[0]["saving_pct"]), ("B (NL)", 20.0))
            finally:
                del os.environ["STOREWATCH_DB"]


if __name__ == "__main__":
    unittest.main()
