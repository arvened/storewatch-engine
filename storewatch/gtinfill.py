"""Barcodes for shops whose catalogue API hides them (Shopify's public /products.json has no barcode).

The product page itself usually carries schema.org markup with gtin13/gtin. This module reads a
limited number of such pages per store per run and remembers the result in `gtin_cache`, so the
cache fills up over a few days and every page is read at most once a month.

Nothing in the snapshots changes: export and compare look the barcode up in the cache when the
variant has none of its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .extract import products_from_html
from .http import FetchError, HttpClient
from .storage import Storage, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS gtin_cache (
    store_url  TEXT NOT NULL,
    variant_id INTEGER NOT NULL,
    gtin       TEXT NOT NULL,          -- '' = page read, no barcode found
    checked_at TEXT NOT NULL,
    PRIMARY KEY (store_url, variant_id)
);
"""

RETRY_EMPTY_DAYS = 30


def ensure(db: Storage) -> None:
    db.db.executescript(SCHEMA)


def cached(db: Storage, store_url: str) -> dict[int, str]:
    """variant_id -> barcode found on the product page (only non-empty ones)."""
    ensure(db)
    return {r["variant_id"]: r["gtin"] for r in db.db.execute(
        "SELECT variant_id, gtin FROM gtin_cache WHERE store_url=? AND gtin<>''", (store_url,))}


def _match(variants: list, page_rows: list) -> dict[int, str]:
    """Shopify variants of one product -> barcode, from the page's offers.
    By SKU first; then a single variant takes the page's single barcode."""
    out: dict[int, str] = {}
    by_sku = {r.sku.strip().lower(): r.gtin for r in page_rows if r.sku and r.gtin}
    for v in variants:
        g = by_sku.get((v.sku or "").strip().lower())
        if g:
            out[v.variant_id] = g
    if not out:
        gtins = {r.gtin for r in page_rows if r.gtin}
        if len(variants) == 1 and len(gtins) == 1:
            out[variants[0].variant_id] = gtins.pop()
        elif len(variants) > 1:
            # same price on page and in the catalogue identifies the variant when prices differ
            by_price: dict[float, set[str]] = {}
            for r in page_rows:
                if r.gtin:
                    by_price.setdefault(round(r.price, 2), set()).add(r.gtin)
            for v in variants:
                g = by_price.get(round(v.price, 2), set())
                if len(g) == 1:
                    out[v.variant_id] = next(iter(g))
    return out


def fill_store(db: Storage, client: HttpClient, store_url: str, per_store: int) -> tuple[int, int]:
    """Read up to `per_store` product pages without a known barcode. Returns (pages read, barcodes found)."""
    ensure(db)
    snaps = db.last_ok_snapshots(store_url, 1)
    if not snaps:
        return 0, 0
    variants = list(db.variants(snaps[0]["id"]).values())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETRY_EMPTY_DAYS)).replace(microsecond=0).isoformat()
    done = {r["variant_id"] for r in db.db.execute(
        "SELECT variant_id FROM gtin_cache WHERE store_url=? AND (gtin<>'' OR checked_at>=?)",
        (store_url, cutoff))}
    todo: dict[str, list] = {}
    for v in variants:
        if not v.gtin and v.variant_id not in done and v.handle:
            todo.setdefault(v.handle, []).append(v)
    pages = found = 0
    for handle, group in todo.items():
        if pages >= per_store:
            break
        path = f"/products/{handle}"
        if not client.allowed(store_url, path):
            continue
        try:
            resp = client.get(store_url + path, accept="text/html")
        except FetchError:
            continue
        pages += 1
        if resp.status in (401, 403, 429):
            break                      # the shop does not want this: stop for today
        if resp.status != 200:
            continue
        got = _match(group, products_from_html(resp.body, store_url + path))
        t = now_iso()
        for v in group:
            g = got.get(v.variant_id, "")
            found += bool(g)
            db.db.execute("INSERT OR REPLACE INTO gtin_cache VALUES (?,?,?,?)", (store_url, v.variant_id, g, t))
        db.db.commit()
    return pages, found


def fill(db: Storage, client: HttpClient, market: str | None, per_store: int) -> list[tuple[str, int, int]]:
    out = []
    for s in db.stores(market):
        if (s["platform"] or "") == "shopify":
            p, f = fill_store(db, client, s["url"], per_store)
            out.append((s["name"], p, f))
    return out
