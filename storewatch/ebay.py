"""eBay prices by barcode, through eBay's official Browse API (free developer account).

Needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (an eBay developer "Production" keyset).
Only new, fixed-price items are taken; the cheapest one per marketplace is kept.
Results are cached in `ebay_offers` for EBAY_CACHE_DAYS days so the daily call limit is respected.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .storage import Storage, now_iso

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_MARKETS = "EBAY_DE,EBAY_GB,EBAY_FR,EBAY_IT,EBAY_ES"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ebay_offers (
    gtin       TEXT NOT NULL,
    market     TEXT NOT NULL,          -- EBAY_DE, EBAY_GB, ...
    price      REAL,                   -- NULL = searched, nothing found
    currency   TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (gtin, market)
);
"""


class EbayError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))


def _token(timeout: float = 20.0) -> str:
    cred = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    req = urllib.request.Request(
        TOKEN_URL, method="POST",
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(cred).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["access_token"]
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise EbayError(f"eBay token: {e}") from e


def parse_search(payload: dict) -> tuple[float | None, str]:
    """Cheapest item price in one search answer -> (price, currency); (None, '') if none."""
    best: tuple[float | None, str] = (None, "")
    for it in payload.get("itemSummaries") or []:
        p = (it or {}).get("price") or {}
        try:
            v = float(p.get("value"))
        except (TypeError, ValueError):
            continue
        if v > 0 and (best[0] is None or v < best[0]):
            best = (v, str(p.get("currency") or ""))
    return best


def _search(token: str, gtin: str, market: str, timeout: float = 20.0) -> tuple[float | None, str]:
    q = urllib.parse.urlencode({"gtin": gtin, "filter": "buyingOptions:{FIXED_PRICE},conditionIds:{1000}",
                                "sort": "price", "limit": "5"})
    req = urllib.request.Request(f"{SEARCH_URL}?{q}", headers={
        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": market})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_search(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""
        raise EbayError(f"eBay search HTTP {e.code}") from e
    except (urllib.error.URLError, ValueError) as e:
        raise EbayError(f"eBay search: {e}") from e


def refresh(db: Storage, gtins: list[str], max_calls: int | None = None,
            markets: list[str] | None = None, cache_days: int | None = None, search=None) -> int:
    """Look up barcodes not checked recently, most important first. Returns API calls made."""
    db.db.executescript(SCHEMA)
    markets = markets or [m.strip() for m in os.environ.get("EBAY_MARKETS", DEFAULT_MARKETS).split(",") if m.strip()]
    max_calls = max_calls if max_calls is not None else int(os.environ.get("EBAY_MAX_CALLS", "1500"))
    cache_days = cache_days if cache_days is not None else int(os.environ.get("EBAY_CACHE_DAYS", "3"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cache_days)).replace(microsecond=0).isoformat()
    fresh = {(r["gtin"], r["market"]) for r in db.db.execute(
        "SELECT gtin, market FROM ebay_offers WHERE checked_at>=?", (cutoff,))}
    if search is None:
        token = _token()
        search = lambda g, m: _search(token, g, m)
    calls = 0
    for g in gtins:
        for m in markets:
            if (g, m) in fresh:
                continue
            if calls >= max_calls:
                db.db.commit()
                return calls
            price, cur = search(g, m)
            calls += 1
            db.db.execute("INSERT OR REPLACE INTO ebay_offers VALUES (?,?,?,?,?)", (g, m, price, cur, now_iso()))
    db.db.commit()
    return calls


def offers(db: Storage) -> dict[str, list[tuple[str, float, str]]]:
    """gtin -> [(market, price, currency)] for items found."""
    db.db.executescript(SCHEMA)
    out: dict[str, list] = {}
    for r in db.db.execute("SELECT gtin, market, price, currency FROM ebay_offers WHERE price IS NOT NULL"):
        out.setdefault(r["gtin"], []).append((r["market"], r["price"], r["currency"]))
    return out
