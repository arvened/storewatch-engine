"""Shopify: public catalogue at /products.json (paginated, up to 250 per page)."""

from __future__ import annotations

import json

from ..http import FetchError, HttpClient, NotThisPlatform
from ..models import Variant, parse_price

NAME = "shopify"
PAGE_LIMIT = 250


def _tags(raw) -> str:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    return ", ".join(t.strip() for t in items if str(t).strip())[:1000]


def _int(raw) -> int | None:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_products(payload: dict) -> list[Variant]:
    """Turn one products.json page into Variant rows. Skips malformed items."""
    out: list[Variant] = []
    for p in payload.get("products", []) or []:
        if not isinstance(p, dict) or "id" not in p:
            continue
        for v in p.get("variants", []) or []:
            if not isinstance(v, dict) or "id" not in v:
                continue
            price = parse_price(v.get("price"))
            if price is None:
                continue
            out.append(Variant(
                product_id=int(p["id"]),
                variant_id=int(v["id"]),
                product_title=str(p.get("title") or "").strip(),
                variant_title=str(v.get("title") or "").strip(),
                handle=str(p.get("handle") or ""),
                sku=str(v.get("sku") or ""),
                vendor=str(p.get("vendor") or ""),
                product_type=str(p.get("product_type") or ""),
                price=price,
                compare_at_price=parse_price(v.get("compare_at_price")),
                available=bool(v.get("available", True)),
                tags=_tags(p.get("tags")),
                grams=_int(v.get("grams")),
                created_at=str(p.get("published_at") or p.get("created_at") or ""),
            ))
    return out


def fetch(client: HttpClient, store: str, max_pages: int = 100) -> list[Variant]:
    if not client.allowed(store, "/products.json"):
        raise NotThisPlatform("robots.txt disallows /products.json")
    variants: list[Variant] = []
    seen: set[int] = set()
    for page in range(1, max_pages + 1):
        resp = client.get(f"{store}/products.json?limit={PAGE_LIMIT}&page={page}")
        if page == 1 and resp.status in (400, 404, 405, 406, 410):
            raise NotThisPlatform(f"/products.json not found (HTTP {resp.status})")
        if page == 1 and resp.status >= 500:
            # Non-Shopify apps often answer unknown paths with 500. A real Shopify outage
            # also lands here: detection then tries the other readers, Shopify first next time.
            raise NotThisPlatform(f"/products.json answered HTTP {resp.status}")
        if resp.status in (401, 403):
            raise FetchError(f"access denied (HTTP {resp.status}) — password-protected or blocks bots")
        if resp.status != 200:
            raise FetchError(f"HTTP {resp.status} on /products.json page {page}")
        try:
            payload = json.loads(resp.body)
        except json.JSONDecodeError:
            if page == 1:
                raise NotThisPlatform("/products.json is not JSON")
            raise FetchError(f"/products.json page {page} is not JSON")
        if not isinstance(payload, dict) or "products" not in payload:
            if page == 1:
                raise NotThisPlatform("/products.json has no products key")
            break
        if not payload["products"]:
            break
        new = [v for v in parse_products(payload) if v.variant_id not in seen]
        if not new:              # store ignores ?page= and repeats page 1
            break
        seen.update(v.variant_id for v in new)
        variants.extend(new)
        if len(payload["products"]) < PAGE_LIMIT:
            break
    return variants
