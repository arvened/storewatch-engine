"""Ceneo XML feed: well-formed, prices converted and marked up, barcodes and stock kept."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from storewatch import feed
from storewatch.cli import main
from storewatch.models import Variant
from storewatch.storage import Storage


def v(vid, title, price, available=True, gtin="", sku="", vendor="Kanlux"):
    return Variant(product_id=vid, variant_id=vid, product_title=title, variant_title="Default Title",
                   handle=f"p-{vid}", sku=sku, vendor=vendor, product_type="Oświetlenie",
                   price=price, compare_at_price=None, available=available, gtin=gtin)


class Feed(unittest.TestCase):
    def test_price_formula(self):
        self.assertEqual(feed.shop_price(10.0, 4.25, 20, 5), 56.0)   # 10 × 4.25 × 1.2 + 5

    def test_xml_is_valid_and_escaped(self):
        items = [("https://hurt.example.pl", "woocommerce",
                  v(1, 'Lampa "LED" & <klosz> ]]> test', 100.0, gtin="5905339123456", sku="K-1")),
                 ("https://hurt.example.pl", "woocommerce", v(2, "Brak", 50.0, available=False))]
        root = ET.fromstring(feed.ceneo_xml(items, rate=1.0, markup_pct=10))
        offers = root.findall("./group/o")
        self.assertEqual(len(offers), 2)
        o = offers[0]
        self.assertEqual(o.get("price"), "110.00")
        self.assertEqual(o.get("avail"), "1")
        self.assertEqual(o.get("url"), "https://hurt.example.pl/product/p-1/")
        self.assertEqual(o.find("name").text, 'Lampa "LED" & <klosz> ]]> test')
        attrs = {a.get("name"): a.text for a in o.find("attrs")}
        self.assertEqual(attrs, {"Producent": "Kanlux", "EAN": "5905339123456", "Kod_producenta": "K-1"})
        self.assertEqual((offers[1].get("avail"), offers[1].get("stock")), ("99", "0"))

    def test_cli_writes_latest_snapshot_of_market(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = os.path.join(d, "s.db")
            db = Storage(dbp)
            db.add_store("https://hurt.example.pl", "Hurt", "woocommerce", market="c-x")
            db.add_store("https://other.example.pl", "Other", "woocommerce", market="other")
            db.save_snapshot("https://hurt.example.pl", [v(1, "Stara", 10.0)])
            db.save_snapshot("https://hurt.example.pl", [v(1, "Nowa", 12.0), v(3, "Zero", 0.0)])
            db.save_snapshot("https://other.example.pl", [v(9, "Obca", 1.0)])
            db.close()
            out = os.path.join(d, "feed.xml")
            os.environ["STOREWATCH_DB"] = dbp
            try:
                rc = main(["feed", "--market", "c-x", "--xml", out, "--rate", "2", "--markup", "50"])
            finally:
                del os.environ["STOREWATCH_DB"]
            self.assertEqual(rc or 0, 0)
            offers = ET.parse(out).getroot().findall("./group/o")
            self.assertEqual([(o.find("name").text, o.get("price")) for o in offers], [("Nowa", "36.00")])


if __name__ == "__main__":
    unittest.main()
