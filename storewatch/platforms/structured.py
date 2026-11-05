"""Universal adapter for any platform (Shoper, IdoSell, Shoptet, Shopware,
PrestaShop, Magento, custom shops, WooCommerce without Store API...).

1. Collect page URLs from all sitemaps (from robots.txt, else /sitemap.xml,
   /sitemap_index.xml); gzipped sitemaps are supported.
2. Group URLs by "shape" (which sitemap, path depth, first path segment) and
   probe a few pages spread across each group. Shops mix products with
   categories, blog posts and info pages, often in one flat sitemap, so the
   first pages of a sitemap say little.
3. Read every page of the groups where products were found, up to
   ``max_pages`` requests in total, and extract products from the markup
   shops publish for Google (see extract.py).

If the budget runs out before all candidate pages are read, the result is
marked as truncated (``client.notes["truncated"]``) so the engine does not
report "new" or "removed" products that were simply not read this time.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from urllib.parse import urlparse

from ..extract import products_from_html
from ..http import FetchError, HttpClient, NotThisPlatform
from ..models import Variant

NAME = "structured"
LANG_ROOT = re.compile(r"[a-z]{2}(-[a-z]{2})?")
MAX_SITEMAPS = 40
MAX_URLS = 20000
PROBE_LIMIT = 40            # pages probed to find which URL groups hold products
COLD_EXPLORE = 20           # per run: unknown pages from URL groups without products
PRODUCT_HINTS = ("product", "produkt", "produit", "produkte", "artikel", "towar", "zbozi", "detail")
NON_PRODUCT = ("blog", "news", "aktuality", "artykul", "article", "contact", "kontakt", "about", "o-nas",
               "o-nás", "uber-uns", "a-propos", "faq", "privacy", "prywatnosc", "datenschutz", "gdpr",
               "regulamin", "terms", "agb", "impressum", "policies", "polityka", "cookies", "kariera",
               "career", "jobs", "zamestnani", "press", "presse", "event", "evenement", "login", "account",
               "konto", "cart", "koszyk", "kosik", "warenkorb", "panier", "search", "tag/", "author")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (child sitemap URLs, page URLs)."""
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
    except ET.ParseError:
        return [], []
    children, pages = [], []
    kind = _local(root.tag)
    for el in root:
        loc = next((c.text.strip() for c in el if _local(c.tag) == "loc" and c.text), None)
        if not loc:
            continue
        (children if kind == "sitemapindex" else pages).append(loc)
    return children, pages


def _is_producty(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in PRODUCT_HINTS)


def _is_non_product(url: str, store: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    segs = [s for s in path.split("/") if s]
    if not segs:
        return True                                   # home page
    if len(segs) == 1 and LANG_ROOT.fullmatch(segs[0]):  # /en, /fr, /pt-br ...
        return True
    return any(f"/{w}" in path for w in NON_PRODUCT)


def collect_urls(client: HttpClient, store: str) -> list[tuple[str, str]]:
    """All page URLs as (url, sitemap it came from), product-hinted sitemaps first."""
    queue = client.sitemaps(store) or [store + "/sitemap.xml", store + "/sitemap_index.xml"]
    seen_maps: set[str] = set()
    out: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    found_any = False
    while queue and len(seen_maps) < MAX_SITEMAPS and len(out) < MAX_URLS:
        sm = queue.pop(0)
        if sm in seen_maps or not client.allowed(store, sm):
            continue
        seen_maps.add(sm)
        resp = client.get(sm, accept="application/xml,text/xml")
        if resp.status != 200:
            continue
        children, pages = _parse_sitemap(resp.body)
        if children or pages:
            found_any = True
        if children:
            producty = [c for c in children if _is_producty(c)]
            queue = producty + queue + [c for c in children if c not in producty]
        for u in pages:
            if u not in seen_urls:
                seen_urls.add(u)
                out.append((u, sm))
    if not found_any:
        raise NotThisPlatform("no sitemap found")
    return out[:MAX_URLS]


def _signature(url: str, source: str) -> tuple:
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    first = segs[0].lower() if len(segs) > 1 else ""
    return (source, len(segs), first, p.path.endswith("/"))


def _spread(items: list, k: int) -> list:
    """k items spread evenly across the list (not the first k)."""
    if len(items) <= k:
        return list(items)
    step = len(items) / k
    return [items[int(step * i + step / 2)] for i in range(k)]


def fetch(client: HttpClient, store: str, max_pages: int = 300) -> list[Variant]:
    """Read products. Uses page memory from previous runs when available
    (client.notes["memory"] = {"product": set(urls), "other": set(urls)}) and
    reports what it learned in client.notes["learned"]."""
    mem = client.notes.get("memory") or {}
    known_prod: set[str] = set(mem.get("product", ()))
    known_other: set[str] = set(mem.get("other", ()))
    learned: dict[str, set[str]] = {"product": set(), "other": set()}
    client.notes["learned"] = learned

    pairs = [(u, s) for u, s in collect_urls(client, store) if not _is_non_product(u, store)]
    pairs = [(u, s) for u, s in pairs if client.allowed(store, u)]
    only = client.notes.get("only")
    if only:                                      # brand filter: read only pages naming a brand
        pairs = [(u, s) for u, s in pairs if only(urlparse(u).path)]
        if not pairs:
            raise NotThisPlatform("no sitemap pages name the selected brands")
    if not pairs:
        raise NotThisPlatform("sitemap has no candidate pages")
    client.notes["candidates"] = len(pairs)

    groups: "OrderedDict[tuple, list[str]]" = OrderedDict()
    for u, s in pairs:
        groups.setdefault(_signature(u, s), []).append(u)

    out: list[Variant] = []
    seen_ids: set[int] = set()
    fetched: set[str] = set()
    errors = 0
    budget = max_pages

    def read(url: str) -> bool:
        nonlocal errors, budget
        budget -= 1
        fetched.add(url)
        try:
            resp = client.get(url, accept="text/html")
        except FetchError:
            errors += 1
            return False
        if resp.status != 200:
            errors += 1
            return False
        found = products_from_html(resp.body, url)
        learned["product" if found else "other"].add(url)
        for v in found:
            if v.variant_id not in seen_ids:
                seen_ids.add(v.variant_id)
                out.append(v)
        return bool(found)

    in_sitemap = {u for u, _ in pairs}
    remembered = known_prod & in_sitemap

    if remembered:
        # Memory mode: known products first, then pages never classified,
        # starting with the URL groups that hold products. Known non-products are skipped.
        product_groups = [g for g in groups if any(u in known_prod for u in groups[g])]
        unknown_hot = [u for g in product_groups for u in groups[g]
                       if u not in known_prod and u not in known_other]
        unknown_cold = [u for g in groups if g not in product_groups for u in groups[g]
                        if u not in known_prod and u not in known_other]
        # Pages in groups without products are rarely products: classify only a few per run,
        # so a large blog or category tree never eats the daily budget.
        for u in [u for u, _ in pairs if u in remembered] + unknown_hot + unknown_cold[:COLD_EXPLORE]:
            if budget <= 0:
                break
            read(u)
        unread_hot = [u for u in unknown_hot if u not in fetched]
        unread_known = [u for u in remembered if u not in fetched]
        if unread_hot or unread_known:
            client.notes["truncated"] = True
        if not out:
            raise NotThisPlatform("no product markup found")
        return out

    # First run (or memory lost): probe URL groups, then read the groups with products.
    order = sorted(groups, key=lambda g: (not (_is_producty(g[0]) or any(_is_producty(u) for u in groups[g][:20])),
                                          -len(groups[g])))
    product_groups = []
    probes = 0
    for g in order:
        if probes >= PROBE_LIMIT or budget <= 0:
            break
        k = 4 if len(groups[g]) > 50 else 2
        hit = False
        for u in _spread(groups[g], k):
            if probes >= PROBE_LIMIT or budget <= 0:
                break
            probes += 1
            hit = read(u) or hit
        if hit:
            product_groups.append(g)
    if not product_groups:
        if errors and errors >= probes // 2:
            raise FetchError(f"{errors} of {probes} probed pages failed to load")
        raise NotThisPlatform(f"no product markup on {probes} probed pages")

    remaining = [u for g in product_groups for u in groups[g] if u not in fetched]
    for u in remaining:
        if budget <= 0:
            client.notes["truncated"] = True
            break
        read(u)
    if not out:
        raise NotThisPlatform("no product markup found")
    return out
