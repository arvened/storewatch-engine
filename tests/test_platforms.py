"""Tests for WooCommerce, the universal structured-data reader and auto-detection."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storewatch import cli  # noqa: E402
from storewatch.engine import run_cycle  # noqa: E402
from storewatch.extract import products_from_html  # noqa: E402
from storewatch.fetcher import CatalogFetcher, FetchError  # noqa: E402
from storewatch.http import HttpResponse  # noqa: E402
from storewatch.models import parse_price  # noqa: E402
from storewatch.storage import Storage  # noqa: E402

NO_SLEEP = lambda s: None  # noqa: E731


class FakeSite:
    """Routes 'path' or 'path?query' to (status, body[, headers]). Unknown -> 404."""

    def __init__(self, routes, robots="User-agent: *\nDisallow: /admin\n"):
        self.routes = dict(routes)
        if robots is not None:
            self.routes.setdefault("/robots.txt", (200, robots))
        self.calls = []

    def __call__(self, url, headers, timeout):
        assert "StoreWatch" in headers["User-Agent"]
        self.calls.append(url)
        u = urlparse(url)
        key_q = u.path + ("?" + u.query if u.query else "")
        for key in (key_q, u.path):
            if key in self.routes:
                r = self.routes[key]
                r = r(u) if callable(r) else r
                return HttpResponse(r[0], r[1], r[2] if len(r) > 2 else {})
        return HttpResponse(404, "not found")


def fetcher(site, **kw):
    return CatalogFetcher(http_get=site, sleep=NO_SLEEP, delay=0, **kw)


# --- WooCommerce -----------------------------------------------------------

def wc_product(pid, name, price, regular=None, in_stock=True, rng=None, cat="Kawa"):
    prices = {"price": str(price), "regular_price": str(regular or price), "sale_price": str(price),
              "currency_code": "PLN", "currency_minor_unit": 2, "price_range": rng}
    return {"id": pid, "name": name, "slug": name.lower().replace(" ", "-"), "sku": f"S{pid}",
            "prices": prices, "is_in_stock": in_stock, "is_purchasable": True,
            "categories": [{"name": cat}], "type": "variable" if rng else "simple"}


def woo_site(products, per_page=100, robots="User-agent: *\nDisallow: /wp-admin/\n"):
    def handler(u):
        q = parse_qs(u.query)
        page = int(q.get("page", ["1"])[0])
        chunk = products[(page - 1) * per_page: page * per_page]
        if page > 1 and not chunk:
            return (400, json.dumps({"code": "rest_post_invalid_page_number"}))
        total = max(1, -(-len(products) // per_page))
        return (200, json.dumps(chunk), {"X-WP-TotalPages": str(total)})
    return FakeSite({"/wp-json/wc/store/v1/products": handler}, robots=robots)


class TestWooCommerce(unittest.TestCase):
    def test_simple_sale_and_stock(self):
        site = woo_site([
            wc_product(1, "Etiopia", 5900, regular=6900),
            wc_product(2, "Brazylia", 4500, in_stock=False),
        ])
        platform, vs = fetcher(site).fetch("roaster.pl")
        self.assertEqual(platform, "woocommerce")
        e = vs[0]
        self.assertEqual((e.price, e.compare_at_price, e.discount_pct, e.product_type), (59.0, 69.0, 14.5, "Kawa"))
        self.assertFalse(vs[1].available)

    def test_variable_product_uses_from_price(self):
        site = woo_site([wc_product(3, "Kolumbia", 0, rng={"min_amount": "4900", "max_amount": "15900"})])
        _, vs = fetcher(site).fetch("roaster.pl")
        self.assertEqual((vs[0].price, vs[0].variant_title, vs[0].compare_at_price), (49.0, "от", None))

    def test_pagination(self):
        site = woo_site([wc_product(i, f"P{i}", 1000) for i in range(1, 251)])
        _, vs = fetcher(site).fetch("roaster.pl")
        self.assertEqual(len(vs), 250)
        self.assertEqual(len([c for c in site.calls if "wc/store" in c]), 3)

    def test_html_entities_in_names(self):
        p = wc_product(1, "Sample &#8211; Guji &#038; Sidama", 200, cat="Kawa &amp; herbata")
        _, vs = fetcher(woo_site([p])).fetch("roaster.pl")
        self.assertEqual((vs[0].product_title, vs[0].product_type), ("Sample – Guji & Sidama", "Kawa & herbata"))

    def test_minor_unit_zero(self):
        p = wc_product(1, "X", 120)
        p["prices"]["currency_minor_unit"] = 0
        _, vs = fetcher(woo_site([p])).fetch("roaster.pl")
        self.assertEqual(vs[0].price, 120.0)

    def test_robots_blocking_wp_json_falls_through(self):
        site = woo_site([wc_product(1, "X", 100)], robots="User-agent: *\nDisallow: /wp-json/\n")
        with self.assertRaises(FetchError):
            fetcher(site).fetch("roaster.pl")
        self.assertFalse(any("wc/store" in c for c in site.calls))


# --- structured data -------------------------------------------------------

JSONLD_PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}</script>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"Product","name":"Etiopia Yirgacheffe","sku":"ETH-250",
 "brand":{"@type":"Brand","name":"Palarnia X"},"category":"Kawa ziarnista",
 "offers":{"@type":"Offer","price":"59,00","priceCurrency":"PLN",
   "availability":"https://schema.org/InStock",
   "priceSpecification":[{"@type":"UnitPriceSpecification","priceType":"https://schema.org/StrikethroughPrice","price":"69.00"}]}}
</script></head><body>...</body></html>"""

GRAPH_MULTI_OFFER = """<script type='application/ld+json'>
{"@graph":[{"@type":"WebPage"},{"@type":["Product"],"name":"Brazylia","offers":[
 {"@type":"Offer","name":"250 g","sku":"B250","price":45,"availability":"InStock"},
 {"@type":"Offer","name":"1 kg","sku":"B1K","price":"149.90","availability":"http://schema.org/OutOfStock"}]}]}
</script>"""

PRODUCT_GROUP = """<script type="application/ld+json">
{"@type":"ProductGroup","name":"Kenia AA","hasVariant":[
 {"@type":"Product","name":"Kenia AA 250g","sku":"K250","offers":{"@type":"Offer","price":"55.00"}},
 {"@type":"Product","name":"Kenia AA 1kg","sku":"K1","offers":{"@type":"Offer","price":"180.00","availability":"SoldOut"}}]}
</script>"""

AGGREGATE = """<script type="application/ld+json">
{"@type":"Product","name":"Gwatemala","offers":{"@type":"AggregateOffer","lowPrice":"39.99","highPrice":"129.99"}}
</script>"""

MICRODATA = """<div itemscope itemtype="http://schema.org/Product">
  <h1 itemprop="name">Kawa Rwanda</h1>
  <span itemprop="price" content="64.90">64,90 zł</span>
  <link itemprop="availability" href="https://schema.org/OutOfStock"/>
</div>"""

OG_META = """<head><meta property="og:title" content="Kawa Peru 250g">
<meta property="product:price:amount" content="52,00"><meta property="product:price:currency" content="PLN">
<meta property="product:original_price:amount" content="60,00">
<meta property="product:availability" content="instock"></head>"""


class TestExtract(unittest.TestCase):
    def test_jsonld_with_strikethrough(self):
        [v] = products_from_html(JSONLD_PAGE, "https://x.pl/p/1")
        self.assertEqual((v.product_title, v.price, v.compare_at_price, v.available, v.vendor, v.sku),
                         ("Etiopia Yirgacheffe", 59.0, 69.0, True, "Palarnia X", "ETH-250"))

    def test_graph_and_offer_list(self):
        vs = products_from_html(GRAPH_MULTI_OFFER, "https://x.pl/brazylia")
        self.assertEqual([(v.label, v.price, v.available) for v in vs],
                         [("Brazylia — 250 g", 45.0, True), ("Brazylia — 1 kg", 149.9, False)])
        self.assertEqual(len({v.product_id for v in vs}), 1)
        self.assertEqual(len({v.variant_id for v in vs}), 2)

    def test_product_group(self):
        vs = products_from_html(PRODUCT_GROUP, "https://x.pl/kenia")
        self.assertEqual([(v.product_title, v.variant_title, v.price, v.available) for v in vs],
                         [("Kenia AA", "Kenia AA 250g", 55.0, True), ("Kenia AA", "Kenia AA 1kg", 180.0, False)])

    def test_aggregate_offer(self):
        [v] = products_from_html(AGGREGATE, "https://x.pl/g")
        self.assertEqual(v.price, 39.99)

    def test_microdata(self):
        [v] = products_from_html(MICRODATA, "https://x.pl/r")
        self.assertEqual((v.product_title, v.price, v.available), ("Kawa Rwanda", 64.9, False))

    def test_og_meta(self):
        [v] = products_from_html(OG_META, "https://x.pl/peru")
        self.assertEqual((v.product_title, v.price, v.compare_at_price, v.available),
                         ("Kawa Peru 250g", 52.0, 60.0, True))

    def test_ids_stable_between_runs(self):
        a = products_from_html(JSONLD_PAGE, "https://x.pl/p/1")[0]
        b = products_from_html(JSONLD_PAGE.replace("59,00", "49,00"), "https://x.pl/p/1")[0]
        self.assertEqual(a.variant_id, b.variant_id)

    def test_no_product(self):
        self.assertEqual(products_from_html("<html><p>Kontakt</p></html>", "https://x.pl/kontakt"), [])
        self.assertEqual(products_from_html('<script type="application/ld+json">{broken</script>', "u"), [])

    def test_parse_price(self):
        cases = {"69.00": 69.0, "69,00": 69.0, "1 299,00 zł": 1299.0, "1.299,00": 1299.0,
                 "1,299.00": 1299.0, "12999": 12999.0, "1,299": 1299.0, 45: 45.0, "abc": None,
                 None: None, "-5": None, "1 299,50": 1299.5}
        for raw, want in cases.items():
            self.assertEqual(parse_price(raw), want, raw)


SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.pl/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://shop.pl/sitemap-products.xml</loc></sitemap>
</sitemapindex>"""


def urlset(urls):
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'


def structured_site(pages=None, robots_extra=""):
    product_urls = ["https://shop.pl/kawa-etiopia", "https://shop.pl/kawa-brazylia", "https://shop.pl/blocked"]
    routes = {
        "/sitemap.xml": (200, SITEMAP_INDEX),
        "/sitemap-products.xml": (200, urlset(product_urls)),
        "/sitemap-pages.xml": (200, urlset(["https://shop.pl/kontakt"])),
        "/kawa-etiopia": (200, JSONLD_PAGE),
        "/kawa-brazylia": (200, GRAPH_MULTI_OFFER),
        "/blocked": (200, MICRODATA),
        "/kontakt": (200, "<p>kontakt</p>"),
    }
    routes.update(pages or {})
    robots = "User-agent: *\nDisallow: /blocked\nSitemap: https://shop.pl/sitemap.xml\n" + robots_extra
    return FakeSite(routes, robots=robots)


class TestStructured(unittest.TestCase):
    def test_detect_and_read(self):
        site = structured_site()
        platform, vs = fetcher(site).fetch("shop.pl")
        self.assertEqual(platform, "structured")
        self.assertEqual(sorted(v.label for v in vs),
                         ["Brazylia — 1 kg", "Brazylia — 250 g", "Etiopia Yirgacheffe"])
        # robots.txt honoured; info pages like /kontakt are never fetched
        self.assertFalse(any(c.endswith("/blocked") for c in site.calls))
        self.assertFalse(any(c.endswith("/kontakt") for c in site.calls))

    def test_brand_filter_reads_only_matching_pages(self):
        site = structured_site()
        platform, vs = fetcher(site).fetch("shop.pl", only=["etiopia"])
        self.assertEqual([v.label for v in vs], ["Etiopia Yirgacheffe"])
        self.assertFalse(any(c.endswith("/kawa-brazylia") for c in site.calls))   # not even opened

    def test_brand_filter_without_matches_fails_clearly(self):
        with self.assertRaisesRegex(FetchError, "selected brands"):
            fetcher(structured_site()).fetch("shop.pl", platform="structured", only=["kimbo"])

    def test_falls_back_to_default_sitemap_path(self):
        site = FakeSite({"/sitemap.xml": (200, urlset(["https://shop.pl/p1"])), "/p1": (200, OG_META)})
        platform, vs = fetcher(site).fetch("shop.pl")
        self.assertEqual((platform, vs[0].price), ("structured", 52.0))

    def test_gives_up_without_markup(self):
        urls = [f"https://shop.pl/page{i}" for i in range(40)]
        routes = {"/sitemap.xml": (200, urlset(urls))}
        routes.update({f"/page{i}": (200, "<p>no product</p>") for i in range(40)})
        site = FakeSite(routes)
        with self.assertRaisesRegex(FetchError, "no product markup"):
            fetcher(site).fetch("shop.pl")
        self.assertLessEqual(len([c for c in site.calls if "/page" in c]), 25)

    def test_max_pages_cap(self):
        urls = [f"https://shop.pl/produkt-{i}" for i in range(50)]
        routes = {"/sitemap.xml": (200, urlset(urls))}
        routes.update({f"/produkt-{i}": (200, OG_META.replace("Peru", f"Peru {i}")) for i in range(50)})
        _, vs = fetcher(FakeSite(routes), max_pages=10).fetch("shop.pl")
        self.assertEqual(len(vs), 10)


# --- detection, persistence, CLI -------------------------------------------

class TestDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_shopify_403_is_reported_not_masked(self):
        site = FakeSite({"/products.json": (403, "blocked")})
        with self.assertRaisesRegex(FetchError, "access denied"):
            fetcher(site).fetch("s.pl")

    def test_platform_remembered_then_redetected_on_change(self):
        db = Storage(self.db_path)
        db.add_store("https://shop.pl", "Shop")
        sites = {"s": structured_site()}
        f = CatalogFetcher(http_get=lambda u, h, t: sites["s"](u, h, t), sleep=NO_SLEEP, delay=0)
        run_cycle(db, f)
        self.assertEqual(db.stores()[0]["platform"], "structured")

        # second run goes straight to the stored platform: no Shopify/Woo probes
        sites["s"] = structured_site()
        run_cycle(db, f)
        self.assertFalse(any("products.json" in c or "wc/store" in c for c in sites["s"].calls))

        # the shop migrates to WooCommerce: detected again, platform updated
        sites["s"] = woo_site([wc_product(1, "Etiopia Yirgacheffe", 5900)])
        reports = run_cycle(db, f)
        self.assertIsNone(reports[0].error)
        self.assertEqual(db.stores()[0]["platform"], "woocommerce")
        db.close()

    def test_migrates_v01_database(self):
        con = sqlite3.connect(self.db_path)
        con.executescript("CREATE TABLE stores (url TEXT PRIMARY KEY, name TEXT NOT NULL, "
                          "added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);"
                          "INSERT INTO stores VALUES ('https://old.pl','Old','2026-10-31',1);")
        con.commit(); con.close()
        db = Storage(self.db_path)
        self.assertIsNone(db.stores()[0]["platform"])
        db.close()

    def test_cli_check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["check", "shop.pl"], fetcher=fetcher(structured_site()))
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Платформа: универсальный", text)
        self.assertIn("Etiopia Yirgacheffe: 59.00 (было 69.00)", text)

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["check", "nothing.pl"], fetcher=fetcher(FakeSite({})))
        self.assertEqual(code, 4)
        self.assertIn("Не подходит", out.getvalue())

    def test_cli_add_with_platform_and_list(self):
        os.environ["STOREWATCH_DB"] = self.db_path
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["add", "shop.pl", "--platform", "woocommerce"])
                cli.main(["list"])
            self.assertIn("WooCommerce", out.getvalue())
        finally:
            del os.environ["STOREWATCH_DB"]


if __name__ == "__main__":
    unittest.main()


class TestStructuredV2(unittest.TestCase):
    """Flat sitemaps (Shoptet-style), probing, truncation, gzip, Shopify 5xx."""

    def flat_site(self, n_products=30, n_info=40, reverse=False):
        info = [f"https://shop.cz/{slug}/" for slug in
                ["doprava-platba", "kontakt", "gdpr-newsletter", "zamestnani_barista", "mnozstevni-sleva",
                 "darkove-poukazy", "technik"] + [f"clanek-{i}" for i in range(n_info - 7)]]
        prods = [f"https://shop.cz/kava-{i}/" for i in range(n_products)]
        listed = info + prods
        routes = {"/sitemap.xml": (200, urlset(listed[::-1] if reverse else listed))}
        for i in range(n_products):
            routes[f"/kava-{i}/"] = (200, MICRODATA.replace("Kawa Rwanda", f"Káva {i}")
                                     .replace('content="64.90"', f'content="{300 + i}"'))
        for u in info:
            routes[urlparse(u).path] = (200, "<p>info</p>")
        return FakeSite(routes)

    def test_products_after_many_info_pages(self):
        site = self.flat_site()
        f = fetcher(site)
        platform, vs = f.fetch("shop.cz")
        self.assertEqual((platform, len(vs)), ("structured", 30))
        self.assertFalse(f.truncated)
        self.assertFalse(any(c.endswith("/kontakt/") for c in site.calls))

    def test_truncated_flag_and_no_false_assortment_alerts(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Storage(os.path.join(tmp.name, "t.db"))
            db.add_store("https://shop.cz", "S")
            site = self.flat_site(n_products=60)
            f = CatalogFetcher(http_get=site, sleep=NO_SLEEP, delay=0, max_pages=25)
            r1 = run_cycle(db, f)
            self.assertTrue(r1[0].partial)
            self.assertTrue(f.truncated)
            # same shop, but the sitemap lists products in another order -> other subset read
            site2 = self.flat_site(n_products=60, reverse=True)
            f2 = CatalogFetcher(http_get=site2, sleep=NO_SLEEP, delay=0, max_pages=25)
            r2 = run_cycle(db, f2)
            kinds = {c.kind for c in r2[0].diff.changes}
            self.assertFalse(kinds & {"product_new", "product_removed"})
            self.assertIn("частично", to_md([*r2]))
            db.close()
        finally:
            tmp.cleanup()

    def test_shopify_500_falls_through_to_other_readers(self):
        site = structured_site({"/products.json": (500, '{"status":500}')})
        platform, _ = fetcher(site).fetch("shop.pl")
        self.assertEqual(platform, "structured")

    def test_gzip_body_is_decompressed(self):
        import gzip as gz
        from unittest import mock
        from storewatch import http as h

        class Resp:
            status = 200
            headers = h.urllib.request.urlopen.__class__  # placeholder, replaced below

            def __init__(self):
                import email.message
                self.headers = email.message.Message()
                self.headers["Content-Type"] = "application/x-gzip"

            def read(self, n=-1):
                return gz.compress(urlset(["https://x.pl/p"]).encode())

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(h.urllib.request, "urlopen", lambda req, timeout: Resp()):
            r = h.urllib_get("https://x.pl/sitemap.xml.gz", {}, 5)
        self.assertIn("<urlset", r.body)


def to_md(reports):
    from storewatch.report import to_markdown
    return to_markdown(reports)


class TestPageMemory(unittest.TestCase):
    """Second and later runs read remembered product pages and skip known non-products."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(os.path.join(self.tmp.name, "m.db"))
        self.db.add_store("https://shop.cz", "S")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def site(self, n_products=40, n_other=60, extra=None):
        others = [f"https://shop.cz/clanek-{i}/" for i in range(n_other)]
        prods = [f"https://shop.cz/kava-{i}/" for i in range(n_products)] + (extra or [])
        routes = {"/sitemap.xml": (200, urlset(others + prods))}
        for u in prods:
            p = urlparse(u).path
            routes[p] = (200, MICRODATA.replace("Kawa Rwanda", p.strip("/")))
        for u in others:
            routes[urlparse(u).path] = (200, "<p>clanek</p>")
        return FakeSite(routes)

    def run_once(self, site, max_pages=300):
        f = CatalogFetcher(http_get=site, sleep=NO_SLEEP, delay=0, max_pages=max_pages)
        return run_cycle(self.db, f)[0], site

    def page_calls(self, site):
        return [c for c in site.calls if "/kava-" in c or "/clanek-" in c]

    def test_second_run_skips_known_non_products(self):
        r1, s1 = self.run_once(self.site())
        self.assertEqual(r1.summary["products"], 40)
        mem = self.db.page_memory("https://shop.cz")
        self.assertEqual(len(mem["product"]), 40)

        r2, s2 = self.run_once(self.site())
        calls = self.page_calls(s2)
        self.assertEqual(len([c for c in calls if "/kava-" in c]), 40)       # all products re-read
        # non-products seen on run 1 are skipped; never-seen ones get classified once
        self.assertLess(len([c for c in calls if "/clanek-" in c]), 60)
        r3, s3 = self.run_once(self.site())
        self.assertEqual(len([c for c in self.page_calls(s3) if "/clanek-" in c]), 0)
        self.assertFalse(r3.partial)
        self.assertGreater(r1.requests, r3.requests)

    def test_new_product_found_and_reported(self):
        self.run_once(self.site())
        self.run_once(self.site())
        r, _ = self.run_once(self.site(extra=["https://shop.cz/kava-nova/"]))
        self.assertIn("product_new", {c.kind for c in r.diff.changes})

    def test_cold_pages_explored_slowly(self):
        # a big tree of category pages in its own URL group never yields products
        cats = [f"https://shop.cz/kolekce/k-{i}/" for i in range(200)]

        def site():
            st = self.site()
            body = st.routes["/sitemap.xml"][1].replace(
                "</urlset>", "".join(f"<url><loc>{u}</loc></url>" for u in cats) + "</urlset>")
            st.routes["/sitemap.xml"] = (200, body)
            for u in cats:
                st.routes[urlparse(u).path] = (200, "<p>kategorie</p>")
            return st
        self.run_once(site())
        r, s = self.run_once(site())
        self.assertEqual(r.summary["products"], 40)
        self.assertLessEqual(len([c for c in s.calls if "/kolekce/" in c]), 20)
        self.assertFalse(r.partial)

    def test_budget_reads_known_products_first(self):
        self.run_once(self.site(n_products=30))
        r, s = self.run_once(self.site(n_products=30), max_pages=35)
        self.assertEqual(r.summary["products"], 30)


class TestCrawlDelay(unittest.TestCase):
    def test_robots_crawl_delay_is_respected(self):
        waits = []
        site = structured_site(robots_extra="Crawl-delay: 3\n")
        f = CatalogFetcher(http_get=site, sleep=waits.append, delay=0.5)
        f.fetch("shop.pl")
        self.assertTrue(waits)
        self.assertGreater(max(waits), 2.0)
        self.assertLessEqual(max(waits), 3.0)


class TestParallel(unittest.TestCase):
    def test_parallel_matches_sequential_and_is_faster(self):
        import threading
        import time as _t

        def slow(site):
            def get(url, headers, timeout):
                _t.sleep(0.01)
                return site(url, headers, timeout)
            return get

        def build(tmp, name):
            db = Storage(os.path.join(tmp, name))
            for i in range(4):
                db.add_store(f"https://s{i}.pl", f"S{i}")
            return db

        sites = {f"s{i}.pl": FakeSite({"/products.json": (200, json.dumps({"products": [
            {"id": 1, "title": "Kawa", "variants": [{"id": 10 + i, "price": "50.00"}]}]}))})
            for i in range(4)}

        def router(url, headers, timeout):
            return sites[url.split("/")[2]](url, headers, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            seq_db, par_db = build(tmp, "a.db"), build(tmp, "b.db")
            t0 = _t.monotonic()
            seq = run_cycle(seq_db, CatalogFetcher(http_get=slow(router), delay=0.2), workers=1)
            t_seq = _t.monotonic() - t0
            threads = set()

            def spy(url, headers, timeout):
                threads.add(threading.get_ident())
                return slow(router)(url, headers, timeout)
            t0 = _t.monotonic()
            par = run_cycle(par_db, CatalogFetcher(http_get=spy, delay=0.2), workers=4)
            t_par = _t.monotonic() - t0
            self.assertEqual([r.name for r in seq], [r.name for r in par])
            self.assertEqual([r.summary for r in seq], [r.summary for r in par])
            self.assertEqual(sum(r.requests for r in par), sum(r.requests for r in seq))
            self.assertGreater(len(threads), 1)
            self.assertLess(t_par, t_seq)
            seq_db.close(); par_db.close()


def woo_site_with_variations(products, variations):
    def handler(u):
        q = parse_qs(u.query)
        page = int(q.get("page", ["1"])[0])
        items = variations if q.get("type") == ["variation"] else products
        chunk = items[(page - 1) * 100: page * 100]
        if page > 1 and not chunk:
            return (400, json.dumps({"code": "rest_post_invalid_page_number"}))
        return (200, json.dumps(chunk), {"X-WP-TotalPages": "1"})
    return FakeSite({"/wp-json/wc/store/v1/products": handler})


class TestWooVariations(unittest.TestCase):
    def _var(self, vid, parent, text, price, regular=None, stock=True):
        return {"id": vid, "parent": parent, "type": "variation", "name": "Kolumbia", "variation": text,
                "is_in_stock": stock, "prices": {"price": str(price), "regular_price": str(regular or price),
                                                 "currency_minor_unit": 2}}

    def test_variations_replace_from_row(self):
        p = wc_product(3, "Kolumbia", 0, rng={"min_amount": "4900", "max_amount": "15900"})
        p["type"] = "variable"
        site = woo_site_with_variations([p, wc_product(4, "Brazylia", 4500)], [
            self._var(31, 3, "Waga: 250 g", 4900),
            self._var(32, 3, "Waga: 1 kg", 15900, regular=17900, stock=False),
            self._var(99, 777, "orphan", 100),
        ])
        _, vs = fetcher(site).fetch("roaster.pl")
        rows = {v.variant_id: v for v in vs}
        self.assertEqual(sorted(rows), [4, 31, 32])
        self.assertEqual((rows[31].product_id, rows[31].weight_g, rows[31].price_per_kg), (3, 250, 196.0))
        self.assertEqual((rows[32].weight_g, rows[32].compare_at_price, rows[32].available), (1000, 179.0, False))

    def test_shop_ignoring_type_keeps_from_row(self):
        p = wc_product(3, "Kolumbia", 0, rng={"min_amount": "4900", "max_amount": "15900"})
        p["type"] = "variable"
        _, vs = fetcher(woo_site([p])).fetch("roaster.pl")
        self.assertEqual((len(vs), vs[0].variant_title), (1, "от"))
