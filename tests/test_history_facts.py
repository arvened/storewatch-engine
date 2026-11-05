"""Facts kept for the price history: weight, price per kg, tags, launch date."""

import os
import tempfile
import unittest

from storewatch.models import Variant, parse_grams
from storewatch.platforms import shopify, woocommerce
from storewatch.storage import Storage


class WeightTest(unittest.TestCase):
    def test_parse(self):
        cases = {"1 kilo / Bonen": 1000, "250g": 250, "2 x 250g": 500, "0,5 kg": 500,
                 "Sitio — 0.5kg": 500, "5kg": 5000, "Size 02": None, "": None, "1 lb": 454}
        for text, grams in cases.items():
            self.assertEqual(parse_grams(text), grams, text)

    def test_price_per_kg_prefers_stated_grams(self):
        v = Variant(1, 1, "Peru 250 g", "", "h", "", "", "", 10.0, None, True, grams=1000)
        self.assertEqual((v.weight_g, v.price_per_kg), (1000, 10.0))
        v2 = Variant(1, 1, "Peru", "250 g", "h", "", "", "", 10.0, None, True)
        self.assertEqual(v2.price_per_kg, 40.0)


class PlatformFactsTest(unittest.TestCase):
    def test_shopify_tags_grams_date(self):
        vs = shopify.parse_products({"products": [{
            "id": 1, "title": "Peru", "handle": "peru", "vendor": "", "product_type": "Coffee",
            "tags": ["Peru", "Washed", "Bio"], "published_at": "2026-10-01T10:00:00+01:00",
            "variants": [{"id": 2, "title": "1kg", "price": "20.00", "grams": 1000, "available": True}]}]})
        self.assertEqual((vs[0].tags, vs[0].grams, vs[0].created_at),
                         ("Peru, Washed, Bio", 1000, "2026-10-01T10:00:00+01:00"))

    def test_woocommerce_tags_and_attributes(self):
        v = woocommerce.parse_product({
            "id": 5, "name": "Kenia AA", "prices": {"price": "1490", "regular_price": "1490"},
            "tags": [{"name": "Bio"}],
            "attributes": [{"name": "Herkunft", "terms": [{"name": "Kenia"}]}]})
        self.assertEqual(v.tags, "Bio, Herkunft: Kenia")


class StorageRoundTripTest(unittest.TestCase):
    def test_new_fields_saved_and_old_db_migrated(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        p = os.path.join(tmp.name, "db")
        Storage(p).close()
        con = sqlite3.connect(p)                       # simulate a pre-v0.8 variants table
        con.executescript("DROP TABLE variants; CREATE TABLE variants (snapshot_id INTEGER, product_id INTEGER,"
                          " variant_id INTEGER, product_title TEXT, variant_title TEXT, handle TEXT, sku TEXT,"
                          " vendor TEXT, product_type TEXT, price REAL, compare_at_price REAL,"
                          " available INTEGER, PRIMARY KEY (snapshot_id, variant_id));")
        con.close()
        db = Storage(p)
        db.add_store("https://a.de", "A")
        sid = db.save_snapshot("https://a.de", [Variant(1, 1, "Peru", "1kg", "h", "", "", "", 20.0, None, True,
                                                        tags="Bio", grams=1000, created_at="2026-10-01")])
        v = db.variants(sid)[1]
        self.assertEqual((v.tags, v.grams, v.created_at), ("Bio", 1000, "2026-10-01"))
        db.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
