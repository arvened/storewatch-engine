import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_shop import FakeShop, product  # noqa: E402

from storewatch import cli  # noqa: E402
from storewatch.diff import DiffConfig, catalog_summary, diff_snapshots  # noqa: E402
from storewatch.engine import run_cycle  # noqa: E402
from storewatch.fetcher import (FetchError, ShopifyFetcher,  # noqa: E402
                                normalize_store_url, parse_products)
from storewatch.report import to_json, to_markdown, to_telegram  # noqa: E402
from storewatch.storage import Storage  # noqa: E402

NO_SLEEP = lambda s: None  # noqa: E731


def fetcher(shop):
    return ShopifyFetcher(http_get=shop, sleep=NO_SLEEP)


def base_catalog():
    return [
        product(1, "Runner", [(11, "40", "100.00", None, True), (12, "41", "100.00", None, True)]),
        product(2, "Walker", [(21, "Default Title", "50.00", None, True)]),
        product(3, "Hiker", [(31, "Default Title", "200.00", "250.00", True)]),
        product(4, "Sandal", [(41, "Default Title", "30.00", None, False)]),
    ]


def changed_catalog():
    return [
        # Runner 40: -15% (high); Runner 41: +0.5% (noise, ignored)
        product(1, "Runner", [(11, "40", "85.00", None, True), (12, "41", "100.50", None, True)]),
        # Walker: new discount (high) and goes out of stock (high)
        product(2, "Walker", [(21, "Default Title", "40.00", "50.00", False)]),
        # Hiker: discount removed, price back to 250 (+25%, medium)
        product(3, "Hiker", [(31, "Default Title", "250.00", None, True)]),
        # Sandal removed; Boot new
        product(5, "Boot", [(51, "Default Title", "120.00", None, True)]),
    ]


class TestFetcher(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_store_url("Shop.Example.com/collections/x"), "https://shop.example.com")
        self.assertEqual(normalize_store_url("http://a.com/"), "http://a.com")
        for bad in ("  ", "not a url ::", "localhost", "ftp://a.pl", "a..pl", "a.pl:99999"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_store_url(bad)
        self.assertEqual(normalize_store_url("127.0.0.1:8801"), "https://127.0.0.1:8801")

    def test_parse_skips_malformed(self):
        payload = {"products": [
            {"id": 1, "title": "A", "variants": [{"id": 2, "price": "9.99"}, {"id": 3, "price": "x"}, {"price": "1"}]},
            {"title": "no id"},
            "garbage",
        ]}
        vs = parse_products(payload)
        self.assertEqual([v.variant_id for v in vs], [2])
        self.assertTrue(vs[0].available)  # missing flag -> available

    def test_pagination(self):
        products = [product(i, f"P{i}", [(i * 10, "Default Title", "10.00", None, True)]) for i in range(1, 601)]
        shop = FakeShop(products)
        vs = fetcher(shop).fetch_catalog("shop.test")
        self.assertEqual(len(vs), 600)
        pages = [c for c in shop.calls if "products.json" in c]
        self.assertEqual(len(pages), 3)  # 250 + 250 + 100

    def test_store_ignoring_page_param_does_not_loop(self):
        products = [product(i, f"P{i}", [(i * 10, "Default Title", "10.00", None, True)]) for i in range(1, 300)]
        shop = FakeShop(products, ignore_page=True)
        vs = fetcher(shop).fetch_catalog("shop.test")
        self.assertEqual(len(vs), 250)
        self.assertEqual(len([c for c in shop.calls if "products.json" in c]), 2)

    def test_robots_disallow(self):
        shop = FakeShop(base_catalog(), robots="User-agent: *\nDisallow: /products.json\n")
        with self.assertRaises(FetchError):
            fetcher(shop).fetch_catalog("shop.test")

    def test_robots_with_unrelated_disallow_is_allowed(self):
        # openosint-pro's original check would wrongly block this store.
        shop = FakeShop(base_catalog(), robots="User-agent: *\nDisallow: /checkout\nDisallow: /admin\n")
        self.assertEqual(len(fetcher(shop).fetch_catalog("shop.test")), 5)

    def test_missing_robots_is_allowed(self):
        shop = FakeShop(base_catalog(), robots=None)
        self.assertEqual(len(fetcher(shop).fetch_catalog("shop.test")), 5)

    def test_not_shopify(self):
        with self.assertRaisesRegex(FetchError, "не найден ни одним способом"):
            fetcher(FakeShop([], status=404)).fetch_catalog("shop.test")

    def test_password_protected(self):
        with self.assertRaisesRegex(FetchError, "access denied"):
            fetcher(FakeShop([], status=401)).fetch_catalog("shop.test")

    def test_retries_on_503(self):
        shop = FakeShop(base_catalog(), fail_first=2)
        self.assertEqual(len(fetcher(shop).fetch_catalog("shop.test")), 5)

    def test_gives_up_after_retries(self):
        shop = FakeShop(base_catalog(), fail_first=10)
        with self.assertRaises(FetchError):
            fetcher(shop).fetch_catalog("shop.test")


class TestDiff(unittest.TestCase):
    def setUp(self):
        f = ShopifyFetcher(http_get=FakeShop(base_catalog()), sleep=NO_SLEEP)
        self.before = {v.variant_id: v for v in f.fetch_catalog("s.test")}
        f = ShopifyFetcher(http_get=FakeShop(changed_catalog()), sleep=NO_SLEEP)
        self.after = {v.variant_id: v for v in f.fetch_catalog("s.test")}
        self.res = diff_snapshots("https://s.test", self.before, self.after)
        self.kinds = {(c.kind, c.variant_id): c for c in self.res.changes}

    def test_big_price_drop_is_high(self):
        c = self.kinds[("price_down", 11)]
        self.assertEqual((c.severity, c.old, c.new, c.pct), ("high", 100.0, 85.0, -15.0))

    def test_noise_ignored(self):
        self.assertNotIn(("price_up", 12), self.kinds)

    def test_new_discount_and_stockout(self):
        self.assertEqual(self.kinds[("discount_new", 21)].severity, "high")
        self.assertEqual(self.kinds[("out_of_stock", 21)].severity, "high")

    def test_sale_drop_reported_once(self):
        # Walker 50 -> 40 with compare-at 50: one "discount_new", no separate "price_down"
        self.assertNotIn(("price_down", 21), self.kinds)
        self.assertEqual(self.kinds[("discount_new", 21)].detail, "-20.0% от 50.00")

    def test_discount_end_and_price_up(self):
        self.assertIn(("discount_end", 31), self.kinds)
        c = self.kinds[("price_up", 31)]
        self.assertEqual((c.severity, c.pct), ("medium", 25.0))

    def test_assortment(self):
        self.assertIn(("product_new", 51), self.kinds)
        self.assertIn(("product_removed", 41), self.kinds)

    def test_sorted_high_first(self):
        sev = [c.severity for c in self.res.changes]
        self.assertEqual(sev, sorted(sev, key={"high": 0, "medium": 1, "low": 2}.get))
        self.assertEqual(self.res.counts, {"high": 3, "medium": 3, "low": 1})

    def test_thresholds_configurable(self):
        res = diff_snapshots("x", self.before, self.after, DiffConfig(big_move_pct=20))
        c = [c for c in res.changes if c.kind == "price_down"][0]
        self.assertEqual(c.severity, "medium")

    def test_no_changes(self):
        self.assertEqual(diff_snapshots("x", self.before, self.before).changes, [])

    def test_summary(self):
        s = catalog_summary(self.before)
        self.assertEqual((s["products"], s["variants"], s["in_stock_pct"], s["on_sale_pct"]), (4, 5, 80.0, 20.0))
        self.assertEqual(s["avg_discount_pct"], 20.0)
        self.assertEqual(catalog_summary({}), {"products": 0, "variants": 0})


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_cycles_and_reports(self):
        db = Storage(self.db_path)
        db.add_store("https://a.test", "Конкурент А")
        db.add_store("https://broken.test", "Сломанный")
        shops = {"a.test": FakeShop(base_catalog()), "broken.test": FakeShop([], status=404)}

        def router(url, headers, timeout):
            host = url.split("/")[2]
            return shops[host](url, headers, timeout)

        f = ShopifyFetcher(http_get=router, sleep=NO_SLEEP)
        first = run_cycle(db, f)
        self.assertIsNone(first[0].diff)            # baseline
        self.assertIsNotNone(first[1].error)
        self.assertIn("Первый снимок", to_markdown(first))

        shops["a.test"] = FakeShop(changed_catalog())
        second = run_cycle(db, f)
        self.assertEqual(second[0].diff.counts["high"], 3)

        md = to_markdown(second)
        self.assertIn("Снижение цены: **Runner — 40** 100.00 → 85.00 (-15.0%)", md)
        self.assertIn("⚠️ Не удалось прочитать магазин", md)

        data = json.loads(to_json(second))
        self.assertEqual(data[0]["counts"]["high"], 3)
        self.assertFalse(data[0]["baseline"])

        tg = to_telegram(second)
        self.assertIn("🔴 Конкурент А — 3 важн.", tg)
        self.assertNotIn("**", tg)
        self.assertLessEqual(len(tg), 4000)

        # error snapshot must not become the comparison base
        self.assertEqual(len(db.last_ok_snapshots("https://a.test", 5)), 2)
        self.assertEqual(db.price_history("https://a.test", 11), [(db.last_ok_snapshots("https://a.test", 5)[1]["taken_at"], 100.0),
                                                                  (db.last_ok_snapshots("https://a.test", 5)[0]["taken_at"], 85.0)])
        db.close()

    def test_telegram_silent_when_nothing_important(self):
        db = Storage(self.db_path)
        db.add_store("https://a.test")
        f = ShopifyFetcher(http_get=FakeShop(base_catalog()), sleep=NO_SLEEP)
        run_cycle(db, f)
        self.assertIsNone(to_telegram(run_cycle(db, f)))
        db.close()

    def test_cli(self):
        os.environ["STOREWATCH_DB"] = self.db_path
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["add", "a.test/collections/all", "--name", "A"]), 0)
                self.assertEqual(cli.main(["add", "https://a.test"]), 0)
                cli.main(["list"])
                md_path = os.path.join(self.tmp.name, "r.md")
                code = cli.main(["run", "--md", md_path],
                                fetcher=ShopifyFetcher(http_get=FakeShop(base_catalog()), sleep=NO_SLEEP))
            text = out.getvalue()
            self.assertIn("Добавлен: https://a.test", text)
            self.assertIn("Уже отслеживается: https://a.test", text)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(md_path))
        finally:
            del os.environ["STOREWATCH_DB"]


class TestNotify(unittest.TestCase):
    def test_send_payload(self):
        from storewatch.notify import send_telegram
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def opener(req, timeout):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
            return Resp(b'{"ok": true}')

        self.assertTrue(send_telegram("TOKEN", "42", "hi", opener=opener))
        self.assertIn("/botTOKEN/sendMessage", seen["url"])
        self.assertEqual(seen["body"]["chat_id"], "42")

        def failing(req, timeout):
            raise OSError("down")
        self.assertFalse(send_telegram("T", "1", "x", opener=failing))


if __name__ == "__main__":
    unittest.main()
