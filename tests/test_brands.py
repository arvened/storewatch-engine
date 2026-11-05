"""Brand filter: read only pages and products of selected brands."""

import os
import tempfile
import unittest

from storewatch import cli
from storewatch.fetcher import brand_matcher
from storewatch.storage import Storage


class BrandMatcherTest(unittest.TestCase):
    def test_whole_words_accents_and_separators(self):
        m = brand_matcher(["Caffè Borbone", "illy"])
        self.assertTrue(m("/fr/cafe-en-grains-caffe-borbone-blu-1kg-p-123.html"))
        self.assertTrue(m("ILLY Classico 250g"))
        self.assertFalse(m("/brilly-mug"))              # 'illy' inside another word
        self.assertFalse(m("/lavazza-oro"))

    def test_no_words_means_no_filter(self):
        self.assertIsNone(brand_matcher([]))
        self.assertIsNone(brand_matcher([" ", "--"]))


class StoreFileBrandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_parse_and_sync(self):
        self.write("reseller.example | Reseller | brands: brand one, brand two\nshop.pl | Shop\n")
        rows = cli.read_store_file(self.path, with_brands=True)
        self.assertEqual(rows, [("https://reseller.example", "Reseller", ["brand one", "brand two"]),
                                ("https://shop.pl", "Shop", [])])
        self.assertEqual(cli.read_store_file(self.path)[0], ("https://reseller.example", "Reseller"))
        db = Storage(os.path.join(self.tmp.name, "db"))
        db.sync_stores(rows, "brands")
        filt = {s["url"]: s["url_filter"] for s in db.stores()}
        self.assertEqual(filt, {"https://reseller.example": "brand one,brand two", "https://shop.pl": None})
        db.sync_stores([("https://reseller.example", "Reseller", [])], "brands")   # filter removed
        self.assertIsNone(db.stores()[0]["url_filter"])
        db.close()

    def test_bad_third_field(self):
        self.write("a.pl | A | brand one\n")
        with self.assertRaises(ValueError):
            cli.read_store_file(self.path, with_brands=True)


class EnginePassesBrandsTest(unittest.TestCase):
    def test_only_reaches_fetcher(self):
        from storewatch.engine import run_cycle
        from storewatch.models import Variant
        seen = {}

        class F:
            def fetch(self, url, platform=None, **kw):
                seen[url] = kw.get("only")
                return "shopify", [Variant(1, 1, "Kimbo", "", "brand one", "", "", "", 5.0, None, True)]
        tmp = tempfile.TemporaryDirectory()
        db = Storage(os.path.join(tmp.name, "db"))
        db.sync_stores([("https://a.fr", "A", ["brand one"]), ("https://b.fr", "B", [])], "brands")
        run_cycle(db, F(), market="brands")
        self.assertEqual(seen, {"https://a.fr": ["brand one"], "https://b.fr": None})
        db.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class ExportTest(unittest.TestCase):
    def test_export_latest_prices(self):
        import csv
        from storewatch.models import Variant
        tmp = tempfile.TemporaryDirectory()
        dbp, out = os.path.join(tmp.name, "db"), os.path.join(tmp.name, "o.csv")
        db = Storage(dbp)
        db.add_store("https://a.pl", "A", market="pl")
        db.save_snapshot("https://a.pl", [Variant(1, 1, "Etiopia", "250 g", "etiopia", "", "", "", 49.0, None, True)])
        db.save_snapshot("https://a.pl", [Variant(1, 1, "Etiopia", "250 g", "etiopia", "", "", "", 52.0, 55.0, True)])
        db.close()
        os.environ["STOREWATCH_DB"] = dbp
        try:
            self.assertEqual(cli.main(["export", "--csv", out, "--market", "pl"]), 0)
        finally:
            del os.environ["STOREWATCH_DB"]
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        self.assertEqual([(r["store"], r["price"], r["compare_at_price"]) for r in rows], [("A", "52.0", "55.0")])
        gz = os.path.join(tmp.name, "h.csv.gz")
        os.environ["STOREWATCH_DB"] = dbp
        try:
            self.assertEqual(cli.main(["export", "--csv", gz, "--market", "pl", "--all"]), 0)
        finally:
            del os.environ["STOREWATCH_DB"]
        import gzip
        rows = list(csv.DictReader(gzip.open(gz, "rt", encoding="utf-8")))
        self.assertEqual([r["price"] for r in rows], ["49.0", "52.0"])     # oldest first
        tmp.cleanup()


class CategoryFilterTest(unittest.TestCase):
    def test_filter_word_matches_product_category(self):
        from storewatch import fetcher as fm
        from storewatch.models import Variant

        def v(i, title, cat):
            return Variant(i, i, title, "", f"p{i}", "", "", cat, 10.0, None, True)

        class Fake:
            @staticmethod
            def fetch(client, store, max_pages):
                return [v(1, "Brazil Cerrado NY2 Specialty", "Rohkaffee"),
                        v(2, "Vase Gitte", "Wohnen")]

        old = fm.ADAPTERS.get("fake")
        fm.ADAPTERS["fake"] = Fake
        try:
            f = fm.CatalogFetcher(http_get=lambda u, h, t: None, sleep=lambda s: None, delay=0)
            f._only = ["Rohkaffee"]
            got = f._run("fake", "https://x.de")
        finally:
            if old is None:
                fm.ADAPTERS.pop("fake")
            else:
                fm.ADAPTERS["fake"] = old
        self.assertEqual([x.product_title for x in got], ["Brazil Cerrado NY2 Specialty"])
