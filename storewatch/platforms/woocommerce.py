"""WooCommerce: public Store API at /wp-json/wc/store/v1/products.

The Store API is the unauthenticated endpoint WooCommerce's own storefront
blocks use. Prices come in minor units (6900 = 69.00).

Variable products: the list endpoint gives only a price range ("from" price).
Their variations (pack size, grind) are then read from the same endpoint with
`?type=variation` — about one extra request per 100 variations. If a shop does
not answer that query, the product stays as one "from" row.
"""

from __future__ import annotations

import html
import json

from ..http import FetchError, HttpClient, NotThisPlatform
from ..models import Variant

NAME = "woocommerce"
PER_PAGE = 100
ENDPOINTS = ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products")


def _amount(value, minor_unit: int) -> float | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    return round(int(s) / (10 ** minor_unit), 2)


def parse_product(p: dict) -> Variant | None:
    if not isinstance(p, dict) or "id" not in p:
        return None
    prices = p.get("prices") or {}
    try:
        mu = int(prices.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        mu = 2
    rng = prices.get("price_range") or None
    if rng and isinstance(rng, dict):
        price = _amount(rng.get("min_amount"), mu)
        compare = None
        variant_title = "от" if rng.get("min_amount") != rng.get("max_amount") else ""
    else:
        price = _amount(prices.get("price"), mu)
        regular = _amount(prices.get("regular_price"), mu)
        compare = regular if (regular and price is not None and regular > price) else None
        variant_title = ""
    if price is None or (price == 0 and not p.get("is_purchasable", True)):
        return None
    cats = p.get("categories") or []
    ptype = ", ".join(html.unescape(str(c.get("name", ""))) for c in cats
                      if isinstance(c, dict) and c.get("name"))
    brands = p.get("brands") or []
    vendor = html.unescape(brands[0].get("name", "")) if brands and isinstance(brands[0], dict) else ""
    facts = [html.unescape(str(t.get("name", ""))) for t in (p.get("tags") or [])
             if isinstance(t, dict) and t.get("name")]
    for a in p.get("attributes") or []:
        if isinstance(a, dict) and a.get("name"):
            vals = ", ".join(html.unescape(str(t.get("name", ""))) for t in (a.get("terms") or [])
                             if isinstance(t, dict) and t.get("name"))
            if vals:
                facts.append(f"{html.unescape(str(a['name']))}: {vals}")
    return Variant(
        product_id=int(p["id"]),
        variant_id=int(p["id"]),
        product_title=html.unescape(str(p.get("name") or "")).strip(),
        variant_title=variant_title,
        handle=str(p.get("slug") or p.get("permalink") or ""),
        sku=str(p.get("sku") or ""),
        vendor=str(vendor),
        product_type=str(ptype),
        price=price,
        compare_at_price=compare,
        available=bool(p.get("is_in_stock", True)),
        tags=", ".join(facts)[:1000],
    )


def _variation_text(v: dict) -> str:
    """Attribute text of a variation, e.g. "250 g, Ziarno"."""
    txt = v.get("variation")
    if isinstance(txt, str) and txt.strip():
        return html.unescape(txt).strip()
    vals = [html.unescape(str(a.get("value", ""))) for a in (v.get("attributes") or [])
            if isinstance(a, dict) and a.get("value")]
    return ", ".join(vals)


def parse_variation(v: dict, parent: Variant) -> Variant | None:
    """One variation row, attached to its parent product."""
    if not isinstance(v, dict) or "id" not in v:
        return None
    prices = v.get("prices") or {}
    try:
        mu = int(prices.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        mu = 2
    price = _amount(prices.get("price"), mu)
    if price is None or price <= 0:
        return None
    regular = _amount(prices.get("regular_price"), mu)
    title = _variation_text(v)
    if not title:
        name = html.unescape(str(v.get("name") or "")).strip()
        if name.startswith(parent.product_title):
            name = name[len(parent.product_title):].lstrip(" -–—:,")
        title = name
    return Variant(
        product_id=parent.product_id,
        variant_id=int(v["id"]),
        product_title=parent.product_title,
        variant_title=title,
        handle=parent.handle,
        sku=str(v.get("sku") or parent.sku),
        vendor=parent.vendor,
        product_type=parent.product_type,
        price=price,
        compare_at_price=regular if (regular and regular > price) else None,
        available=bool(v.get("is_in_stock", True)),
        tags=parent.tags,
    )


def _expand_variations(client: HttpClient, store: str, base: str, out: list[Variant],
                       variable: set[int], max_pages: int) -> list[Variant]:
    """Replace "from" rows of variable products with their variations, when the shop allows."""
    if not variable:
        return out
    parents = {v.product_id: v for v in out if v.product_id in variable}
    rows: dict[int, list[Variant]] = {}
    page = 1
    while page <= max_pages:
        try:
            resp = client.get(f"{store}{base}?type=variation&per_page={PER_PAGE}&page={page}")
        except FetchError:
            break
        if resp.status != 200:
            break
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError:
            break
        if not isinstance(data, list) or not data:
            break
        if page == 1 and not any(isinstance(d, dict) and d.get("type") == "variation" for d in data):
            break                      # the shop ignores type=variation: keep "from" rows
        for d in data:
            if not isinstance(d, dict):
                continue
            try:
                parent = parents.get(int(d.get("parent") or 0))
            except (TypeError, ValueError):
                parent = None
            if parent is None:
                continue
            row = parse_variation(d, parent)
            if row:
                rows.setdefault(parent.product_id, []).append(row)
        if len(data) < PER_PAGE:
            break
        page += 1
    if not rows:
        return out
    result: list[Variant] = []
    for v in out:
        if v.product_id in rows:
            if v.variant_id == v.product_id:   # the "from" row: replaced once by its variations
                result.extend(rows.pop(v.product_id))
        else:
            result.append(v)
    return result


def fetch(client: HttpClient, store: str, max_pages: int = 100) -> list[Variant]:
    base = None
    first = None
    for ep in ENDPOINTS:
        if not client.allowed(store, ep):
            continue
        resp = client.get(f"{store}{ep}?per_page={PER_PAGE}&page=1")
        if resp.status == 200:
            try:
                data = json.loads(resp.body)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                base, first = ep, (resp, data)
                break
    if base is None:
        raise NotThisPlatform("no WooCommerce Store API")

    resp, data = first
    total_pages = resp.header("X-WP-TotalPages")
    total_pages = int(total_pages) if total_pages and total_pages.isdigit() else None
    out: list[Variant] = []
    seen: set[int] = set()
    variable: set[int] = set()
    page = 1
    while True:
        variable.update(int(p["id"]) for p in data if isinstance(p, dict) and "id" in p
                        and (p.get("type") == "variable" or p.get("variations")))
        new = [v for v in (parse_product(p) for p in data) if v and v.variant_id not in seen]
        seen.update(v.variant_id for v in new)
        out.extend(new)
        if not data or len(data) < PER_PAGE or not new:
            break
        if total_pages is not None and page >= total_pages:
            break
        page += 1
        if page > max_pages:
            break
        resp = client.get(f"{store}{base}?per_page={PER_PAGE}&page={page}")
        if resp.status == 400:         # past the last page
            break
        if resp.status != 200:
            raise FetchError(f"HTTP {resp.status} on Store API page {page}")
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as e:
            raise FetchError(f"Store API page {page} is not JSON") from e
        if not isinstance(data, list):
            break
    return _expand_variations(client, store, base, out, variable, max_pages)
