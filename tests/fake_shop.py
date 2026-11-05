"""A fake Shopify storefront for tests: serves robots.txt and products.json."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from storewatch.fetcher import HttpResponse


def product(pid, title, variants, handle=None, ptype="Shoes", vendor="Acme"):
    return {
        "id": pid, "title": title, "handle": handle or title.lower().replace(" ", "-"),
        "vendor": vendor, "product_type": ptype,
        "variants": [
            {"id": vid, "title": vt, "sku": f"SKU{vid}", "price": price,
             "compare_at_price": cap, "available": avail}
            for (vid, vt, price, cap, avail) in variants
        ],
    }


class FakeShop:
    def __init__(self, products, robots="User-agent: *\nDisallow: /admin\nDisallow: /cart\n",
                 status=200, ignore_page=False, fail_first=0):
        self.products = products
        self.robots = robots
        self.status = status
        self.ignore_page = ignore_page
        self.fail_first = fail_first
        self.calls: list[str] = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        assert "StoreWatch" in headers["User-Agent"]
        u = urlparse(url)
        if u.path == "/robots.txt":
            if self.robots is None:
                return HttpResponse(404, "")
            return HttpResponse(200, self.robots)
        if u.path == "/products.json":
            if self.fail_first > 0:
                self.fail_first -= 1
                return HttpResponse(503, "busy")
            if self.status != 200:
                return HttpResponse(self.status, "<html>nope</html>")
            q = parse_qs(u.query)
            limit = int(q.get("limit", ["30"])[0])
            page = 1 if self.ignore_page else int(q.get("page", ["1"])[0])
            chunk = self.products[(page - 1) * limit: page * limit]
            return HttpResponse(200, json.dumps({"products": chunk}))
        return HttpResponse(404, "")
