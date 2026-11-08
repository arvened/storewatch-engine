"""Barcodes (EAN/GTIN) are read from shop pages and APIs and survive the database."""

import os
import sqlite3
import tempfile
import unittest

from storewatch.extract import products_from_html
from storewatch.models import Variant, clean_gtin
from storewatch.platforms.woocommerce import parse_product
from storewatch.storage import Storage


class CleanGtin(unittest.TestCase):
    def test_valid_lengths(self):
        self.assertEqual(clean_gtin("5906826473019"), "5906826473019")
        self.assertEqual(clean_gtin(" 0 12345 67890 5 "), "012345678905")
        self.assertEqual(clean_gtin("EAN: 5906826473019"), "5906826473019")
        self.assertEqual(clean_gtin(12345670), "12345670")

    def test_rejects_junk(self):
        for bad in (None, "", "abc", "123", "12345678901", True, {"x": 1}, [1]):
            self.assertEqual(clean_gtin(bad), "", bad)


class FromHtml(unittest.TestCase):
    def test_jsonld_product_level(self):
        html = """<script type="application/ld+json">{"@type":"Product","name":"Gra","gtin13":"5906826473019",
        "brand":{"name":"Pegaz Toys"},"offers":{"@type":"Offer","price":"19.99"}}</script>"""
        [v] = products_from_html(html, "https://shop.example/gra")
        self.assertEqual(v.gtin, "5906826473019")
        self.assertEqual(v.vendor, "Pegaz Toys")

    def test_jsonld_offer_level_wins(self):
        html = """<script type="application/ld+json">{"@type":"Product","name":"Skakanka","gtin":"5905523623451",
        "offers":[{"@type":"Offer","price":"5","sku":"a","gtin13":"5903246430707"},
                  {"@type":"Offer","price":"6","sku":"b"}]}</script>"""
        a, b = products_from_html(html, "https://shop.example/s")
        self.assertEqual(a.gtin, "5903246430707")
        self.assertEqual(b.gtin, "5905523623451")      # falls back to the product's barcode

    def test_microdata(self):
        html = """<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Lalka</span>
        <meta itemprop="gtin13" content="5907414303886"><span itemprop="price">29,99</span></div>"""
        [v] = products_from_html(html, "https://shop.example/l")
        self.assertEqual(v.gtin, "5907414303886")

    def test_no_barcode(self):
        html = """<script type="application/ld+json">{"@type":"Product","name":"X",
        "offers":{"@type":"Offer","price":"1"}}</script>"""
        [v] = products_from_html(html, "https://shop.example/x")
        self.assertEqual(v.gtin, "")


class FromWoo(unittest.TestCase):
    def test_global_unique_id(self):
        p = {"id": 7, "name": "Klocki", "prices": {"price": "4999", "currency_minor_unit": 2},
             "global_unique_id": "5907414303886"}
        self.assertEqual(parse_product(p).gtin, "5907414303886")


class Storage_(unittest.TestCase):
    def test_roundtrip_and_migration(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "old.db")
        # a database from before this column existed
        db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE variants (snapshot_id INTEGER, product_id INTEGER, variant_id INTEGER,
            product_title TEXT, variant_title TEXT, handle TEXT, sku TEXT, vendor TEXT, product_type TEXT,
            price REAL, compare_at_price REAL, available INTEGER, tags TEXT NOT NULL DEFAULT '', grams INTEGER,
            created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (snapshot_id, variant_id));""")
        db.close()
        s = Storage(path)
        cols = {r["name"] for r in s.db.execute("PRAGMA table_info(variants)")}
        self.assertIn("gtin", cols)
        s.close()

        s = Storage(os.path.join(d, "new.db"))
        s.add_store("https://shop.example")
        v = Variant(1, 2, "Gra", "", "gra", "", "Pegaz Toys", "", 19.99, None, True, gtin="5906826473019")
        sid = s.save_snapshot("https://shop.example", [v])
        self.assertEqual(s.variants(sid)[2].gtin, "5906826473019")


if __name__ == "__main__":
    unittest.main()
